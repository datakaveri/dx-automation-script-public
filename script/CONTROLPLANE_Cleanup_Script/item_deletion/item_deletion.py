#!/usr/bin/env python3
"""Delete catalogue items from ControlPlane — policies first, then the item.

Usage:
    python item_deletion.py                                   # item_deletion_config.json beside this script
    python item_deletion.py my_config.json
    python item_deletion.py --item-id <uuid> [--item-id <uuid> ...]
    python item_deletion.py --dry-run

What it deletes comes from the config, in three ways that can be combined:

    target.item_ids       ids pasted in, deleted as `owner`
    target.name_prefix    every item `owner` has whose name starts with this,
                          found through /cat/search/myassets
    sweep.enabled         every Keycloak account whose username starts with
                          sweep.username_prefix is signed in as (with
                          sweep.password) and its prefixed items are deleted —
                          the same pass the complete-test harness runs after a
                          collection, for items whose owner is not `owner`

The order per item is mandatory: `DELETE /cat/item` refuses with 409 while any
active policy exists, so the item's policies are deactivated first through the
ACL server. A policy that is already inactive is fine — the API answers 400
"policy is not ACTIVE", and inactive is what is wanted.

Exit codes:
    0  every item is gone (or was already)
    1  at least one item or policy could not be removed
    2  configuration error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # allows --help and --dry-run before install
    requests = None  # type: ignore[assignment]

LOG = logging.getLogger("item_deletion")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

DEFAULT_CONFIG: dict[str, Any] = {
    "control_plane": {
        "base_url": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    "acl": {
        # The policy server. Empty means the same host as control_plane.
        "base_url": "",
    },
    "keycloak": {
        "url": "",
        "realm": "",
        # Public client for password-grant tokens (the owner, and each swept account).
        "user_client_id": "postman-client",
        "user_client_secret": "",
        # Only needed with sweep.enabled — listing users is an Admin API call.
        "admin_client_id": "",
        "admin_client_secret": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    # The provider that owns target.item_ids. A ready token skips the sign-in.
    "owner": {
        "username": "",
        "password": "",
        "token": "",
    },
    "target": {
        "item_ids": [],
        "name_prefix": "",
    },
    "sweep": {
        "enabled": False,
        "username_prefix": "",
        # The password every swept account shares (run.user_password in the harness).
        "password": "",
        # Accounts never signed in as, whatever the prefix says.
        "protected_usernames": [],
        # Only providers can own items; skip accounts without this realm role
        # quietly instead of reporting a failed sign-in for them.
        "provider_role": "provider",
    },
    "delete": {
        "deactivate_policies": True,
        # Re-read each item afterwards and fail if it is still there.
        "verify": True,
    },
    "endpoints": {
        "kc_token": "/realms/{realm}/protocol/openid-connect/token",
        "kc_users": "/admin/realms/{realm}/users",
        "kc_role_mappings": "/admin/realms/{realm}/users/{user_id}/role-mappings/realm",
        "cp_item": "/iudx/v2/cat/item",
        "cp_my_assets": "/iudx/v2/cat/search/myassets",
        "acl_provider_policies": "/iudx/acl/apd/v2/policy/provider",
        "acl_policy": "/iudx/acl/apd/v2/policy",
    },
    "logging": {
        "level": "INFO",
    },
}


class ConfigError(ValueError):
    """A required configuration value is absent or invalid."""


class ApiError(RuntimeError):
    """Keycloak or ControlPlane rejected a request."""


# --------------------------------------------------------------------- config

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Resolve ${VAR} / ${VAR:-fallback} anywhere in the config tree."""
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    if isinstance(value, str):
        return _ENV_REF.sub(
            lambda m: os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else ""),
            value,
        )
    return value


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return deep_merge(DEFAULT_CONFIG, expand_env(raw))


def require(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} is required")
    return value.strip()


def validate(config: dict[str, Any], item_ids: list[str]) -> None:
    require(config["control_plane"]["base_url"], "control_plane.base_url")
    target = config["target"]
    sweep = config["sweep"]
    if not (item_ids or target.get("name_prefix") or sweep.get("enabled")):
        raise ConfigError(
            "nothing to delete: set target.item_ids, target.name_prefix, "
            "sweep.enabled, or pass --item-id"
        )
    owner = config["owner"]
    if item_ids or target.get("name_prefix"):
        if not owner.get("token"):
            require(owner.get("username"), "owner.username")
            require(owner.get("password"), "owner.password")
            require(config["keycloak"]["url"], "keycloak.url")
            require(config["keycloak"]["realm"], "keycloak.realm")
    if sweep.get("enabled"):
        require(sweep.get("username_prefix"), "sweep.username_prefix")
        require(sweep.get("password"), "sweep.password")
        require(config["keycloak"]["url"], "keycloak.url")
        require(config["keycloak"]["realm"], "keycloak.realm")
        require(config["keycloak"]["admin_client_id"], "keycloak.admin_client_id")
        require(config["keycloak"]["admin_client_secret"], "keycloak.admin_client_secret")


# ---------------------------------------------------------------- http helpers

def field(row: Any, *names: str, default: Any = None) -> Any:
    """Read a field from an API row, flat or nested one level down."""
    if not isinstance(row, dict):
        return default
    for name in names:
        if row.get(name) is not None:
            return row[name]
    for value in row.values():
        if isinstance(value, dict):
            for name in names:
                if value.get(name) is not None:
                    return value[name]
    return default


def rows_of(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "result", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _short(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


class Http:
    """One host, one session. Unwraps the {type,title,result} envelope."""

    def __init__(self, base_url: str, timeout: int, verify_tls: bool):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_tls

    def request(self, method: str, path: str, label: str, token: str | None = None,
                params: dict[str, Any] | None = None, form: dict[str, str] | None = None,
                expect: tuple[int, ...] = (200,)) -> tuple[int, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self.session.request(
                method, url, params=params, data=form, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise ApiError(f"{label}: {method} {url} failed: {exc}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        LOG.debug("%s -> %s %s", label, response.status_code, _short(str(payload), 200))
        if response.status_code not in expect:
            raise ApiError(
                f"{label}: {method} {url} returned {response.status_code}: "
                f"{_short(json.dumps(payload) if not isinstance(payload, str) else payload)}"
            )
        if isinstance(payload, dict) and "result" in payload:
            return response.status_code, payload["result"]
        return response.status_code, payload


class Keycloak:
    def __init__(self, config: dict[str, Any], endpoints: dict[str, str]):
        self.config = config
        self.endpoints = endpoints
        self.http = Http(config["url"], config["timeout_seconds"], config["verify_tls"])
        self._admin_token: str | None = None

    def path(self, name: str, **fields: str) -> str:
        return self.endpoints[name].format(realm=self.config["realm"], **fields)

    def user_token(self, username: str, password: str) -> str:
        form = {
            "grant_type": "password",
            "client_id": self.config["user_client_id"],
            "username": username,
            "password": password,
        }
        if self.config.get("user_client_secret"):
            form["client_secret"] = self.config["user_client_secret"]
        _, payload = self.http.request("POST", self.path("kc_token"), f"token for {username}", form=form)
        return payload["access_token"]

    def admin_token(self) -> str:
        if self._admin_token:
            return self._admin_token
        _, payload = self.http.request(
            "POST", self.path("kc_token"), "keycloak admin token",
            form={
                "grant_type": "client_credentials",
                "client_id": self.config["admin_client_id"],
                "client_secret": self.config["admin_client_secret"],
            },
        )
        self._admin_token = payload["access_token"]
        return self._admin_token

    def find_users_by_prefix(self, prefix: str) -> list[dict[str, Any]]:
        _, users = self.http.request(
            "GET", self.path("kc_users"), f"find users like {prefix}",
            token=self.admin_token(), params={"username": prefix, "max": 500},
        )
        return [u for u in users if str(u.get("username", "")).startswith(prefix)]

    def realm_roles(self, user_id: str) -> list[str]:
        _, mapped = self.http.request(
            "GET", self.path("kc_role_mappings", user_id=user_id), "read realm roles",
            token=self.admin_token(),
        )
        return [role["name"] for role in mapped]


# -------------------------------------------------------------------- deletion

class ItemDeleter:
    def __init__(self, config: dict[str, Any], dry_run: bool):
        self.config = config
        self.dry_run = dry_run
        self.endpoints = config["endpoints"]
        cp = config["control_plane"]
        self.cp = Http(cp["base_url"], cp["timeout_seconds"], cp["verify_tls"])
        acl_url = config["acl"].get("base_url") or cp["base_url"]
        self.acl = Http(acl_url, cp["timeout_seconds"], cp["verify_tls"])
        self.problems: list[str] = []
        self.deleted: list[str] = []

    # -- policies

    def policy_ids(self, token: str, item_id: str) -> list[str]:
        """Every policy on this item, from the owner's provider policy list.

        The endpoint returns more than this provider's own rows on some
        deployments, so the item id is what is matched on — never the list.
        """
        _, payload = self.acl.request(
            "GET", self.endpoints["acl_provider_policies"], f"list policies for {item_id}", token=token
        )
        ids = []
        for row in rows_of(payload):
            if str(field(row, "itemId")) != str(item_id):
                continue
            pid = field(row, "policyId", "id", "_id")
            if pid and pid not in ids:
                ids.append(str(pid))
        return ids

    def deactivate_policy(self, token: str, policy_id: str, item_id: str) -> None:
        if self.dry_run:
            LOG.info("DRY RUN — would deactivate policy %s on %s", policy_id, item_id)
            return
        try:
            self.acl.request(
                "PUT", self.endpoints["acl_policy"], f"deactivate policy {policy_id}",
                token=token, params={"id": policy_id},
            )
            LOG.info("deactivated policy %s on %s", policy_id, item_id)
        except ApiError as exc:
            if "not ACTIVE" in str(exc):
                LOG.info("policy %s was already inactive", policy_id)
                return
            raise

    # -- items

    def delete_item(self, token: str, item_id: str, name: str = "") -> None:
        label = f"{item_id}" + (f" ({name})" if name else "")
        if self.config["delete"]["deactivate_policies"]:
            try:
                for pid in self.policy_ids(token, item_id):
                    try:
                        self.deactivate_policy(token, pid, item_id)
                    except ApiError as exc:
                        self.problems.append(f"policy {pid} on {label}: {exc}")
            except ApiError as exc:
                self.problems.append(f"could not list policies on {label}: {exc}")

        if self.dry_run:
            LOG.info("DRY RUN — would delete item %s", label)
            return
        try:
            status, _ = self.cp.request(
                "DELETE", self.endpoints["cp_item"], f"delete item {label}",
                token=token, params={"id": item_id}, expect=(200, 404),
            )
        except ApiError as exc:
            self.problems.append(f"delete item {label}: {exc}")
            return
        if status == 404:
            LOG.info("item %s was already gone", label)
        else:
            LOG.info("deleted item %s", label)
        self.deleted.append(item_id)

        if self.config["delete"]["verify"]:
            self.verify_gone(token, item_id, label)

    def verify_gone(self, token: str, item_id: str, label: str) -> None:
        try:
            status, payload = self.cp.request(
                "GET", self.endpoints["cp_item"], f"verify {label} is gone",
                token=token, params={"id": item_id}, expect=(200, 404),
            )
        except ApiError as exc:
            self.problems.append(f"could not verify {label}: {exc}")
            return
        rows = rows_of(payload) if status == 200 else []
        if any(rows):
            self.problems.append(f"item {label} still exists after delete")

    def my_assets(self, token: str, who: str) -> list[dict[str, Any]]:
        _, payload = self.cp.request(
            "GET", self.endpoints["cp_my_assets"], f"list assets of {who}",
            token=token, params={"page": 1, "size": 100},
        )
        return rows_of(payload)

    def delete_prefixed_assets(self, token: str, who: str, prefix: str) -> int:
        count = 0
        for row in self.my_assets(token, who):
            item_id = field(row, "id", "itemId")
            name = str(field(row, "name", default=""))
            if not item_id or not name.startswith(prefix):
                continue
            self.delete_item(token, str(item_id), name)
            count += 1
        if not count:
            LOG.info("%s owns no item named %s*", who, prefix)
        return count

    # -- the sweep across accounts

    def sweep(self, kc: Keycloak) -> None:
        sweep = self.config["sweep"]
        prefix = sweep["username_prefix"]
        name_prefix = self.config["target"].get("name_prefix") or prefix
        protected = {u.lower() for u in sweep.get("protected_usernames", []) if u}

        users = kc.find_users_by_prefix(prefix)
        LOG.info("sweep: %d account(s) under %s*", len(users), prefix)
        for user in users:
            username = str(user.get("username", ""))
            if username.lower() in protected:
                LOG.info("protected account, assets left alone: %s", username)
                continue
            try:
                token = kc.user_token(username, sweep["password"])
            except ApiError:
                # Only a provider can own an item. For anyone else a failed
                # sign-in means nothing is being missed.
                try:
                    roles = kc.realm_roles(user["id"])
                except ApiError:
                    roles = [sweep["provider_role"]]
                if sweep["provider_role"] not in roles:
                    LOG.info("cannot sign in as %s; not a provider, nothing to sweep", username)
                    continue
                self.problems.append(
                    f"could not sign in as {username} — it is a provider, so any item it owns is left behind"
                )
                continue
            try:
                self.delete_prefixed_assets(token, username, name_prefix)
            except ApiError as exc:
                self.problems.append(f"could not list assets for {username}: {exc}")


def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(str(args.log_level or config["logging"]["level"]).upper())

    item_ids = [str(i) for i in (config["target"].get("item_ids") or []) if i]
    item_ids += list(args.item_id or [])
    validate(config, item_ids)

    if requests is None:
        raise ConfigError("the 'requests' package is not installed: pip install -r ../requirements.txt")

    deleter = ItemDeleter(config, args.dry_run)
    kc = Keycloak(config["keycloak"], config["endpoints"]) if config["keycloak"].get("url") else None

    owner = config["owner"]
    prefix = config["target"].get("name_prefix")
    if item_ids or prefix:
        token = owner.get("token")
        if not token:
            assert kc is not None
            token = kc.user_token(owner["username"], owner["password"])
        for item_id in item_ids:
            deleter.delete_item(token, item_id)
        if prefix:
            deleter.delete_prefixed_assets(token, owner.get("username") or "owner", prefix)

    if config["sweep"]["enabled"]:
        assert kc is not None
        deleter.sweep(kc)

    LOG.info("%d item(s) %s", len(deleter.deleted), "would be deleted" if args.dry_run else "deleted")
    if deleter.problems:
        for problem in deleter.problems:
            LOG.error("%s", problem)
        return EXIT_FAILED
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config", nargs="?", type=Path,
        default=Path(__file__).with_name("item_deletion_config.json"),
        help="JSON config path (default: item_deletion_config.json beside this script)",
    )
    parser.add_argument("--item-id", action="append", help="an item to delete, in addition to target.item_ids")
    parser.add_argument("--dry-run", action="store_true", help="list what would be deleted, delete nothing")
    parser.add_argument("--log-level", help="override logging.level from the config")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        return run(args.config, args)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return EXIT_CONFIG
    except (ApiError, OSError) as exc:
        LOG.error("%s", exc)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
