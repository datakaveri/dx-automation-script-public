#!/usr/bin/env python3
"""Delete Keycloak users by username prefix, or by name — the accounts sweep.

Usage:
    python keycloak_user_sweep.py                          # keycloak_user_sweep_config.json beside this script
    python keycloak_user_sweep.py my_config.json
    python keycloak_user_sweep.py --username <name> [--username <name> ...]
    python keycloak_user_sweep.py --dry-run

The per-role deletion scripts in script/user_creation remove one account each,
and prefer ControlPlane's own self-delete. This one is the other half of a
teardown: everything left under a prefix once the flow is over — org admins the
platform refuses to self-delete, providers, accounts a crashed run never got
to. It goes straight to the Keycloak Admin API, which is the only route that
works for all of them.

Who goes:

    target.usernames          named accounts, exact match
    target.prefix             every account whose username starts with this
    target.protected_usernames  never deleted, whatever the prefix says — name
                              any borrowed platform account here
    target.older_than_hours   with a prefix, leave accounts younger than this
                              alone (0 = sweep all). A guard for a prefix that
                              several people share, so a run still in flight is
                              not swept from under them.

Keycloak rows are only half of an account: the platform's own rows for it live
in Postgres and are the database_sweep script's job. Run that afterwards, and
note that it resolves user ids from Keycloak — so run it before this if you
want the ids found for you, or paste them into its config.

Exit codes:
    0  every targeted account is gone (or was already)
    1  at least one could not be removed
    2  configuration error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # allows --help and --dry-run before install
    requests = None  # type: ignore[assignment]

LOG = logging.getLogger("keycloak_user_sweep")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

DEFAULT_CONFIG: dict[str, Any] = {
    "keycloak": {
        "url": "",
        "realm": "",
        "admin_client_id": "",
        "admin_client_secret": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    "target": {
        "usernames": [],
        "prefix": "",
        "protected_usernames": [],
        "older_than_hours": 0,
    },
    "delete": {
        # List the prefix again afterwards and fail if anything is still there.
        "verify": True,
        # Print the ids that were deleted, for pasting into database_sweep.
        "print_user_ids": True,
    },
    "endpoints": {
        "kc_token": "/realms/{realm}/protocol/openid-connect/token",
        "kc_users": "/admin/realms/{realm}/users",
        "kc_user": "/admin/realms/{realm}/users/{user_id}",
    },
    "logging": {
        "level": "INFO",
    },
}


class ConfigError(ValueError):
    """A required configuration value is absent or invalid."""


class ApiError(RuntimeError):
    """Keycloak rejected a request."""


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


def validate(config: dict[str, Any], usernames: list[str]) -> None:
    kc = config["keycloak"]
    for key in ("url", "realm", "admin_client_id", "admin_client_secret"):
        require(kc.get(key), f"keycloak.{key}")
    if not (usernames or config["target"].get("prefix")):
        raise ConfigError("nothing to delete: set target.usernames, target.prefix, or pass --username")
    prefix = config["target"].get("prefix")
    if prefix and len(prefix) < 3:
        # A one-letter prefix matches most of a realm. Refuse rather than ask.
        raise ConfigError(f"target.prefix {prefix!r} is too short to sweep on safely")


# ------------------------------------------------------------------- keycloak

class Keycloak:
    def __init__(self, config: dict[str, Any], endpoints: dict[str, str]):
        self.config = config
        self.endpoints = endpoints
        self.base_url = config["url"].rstrip("/")
        self.session = requests.Session()
        self.session.verify = config["verify_tls"]
        self.timeout = config["timeout_seconds"]
        self._admin_token: str | None = None

    def path(self, name: str, **fields: str) -> str:
        return self.endpoints[name].format(realm=self.config["realm"], **fields)

    def _request(self, method: str, path: str, label: str, token: str | None = None,
                 params: dict[str, Any] | None = None, form: dict[str, str] | None = None,
                 expect: tuple[int, ...] = (200,)) -> Any:
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
        if response.status_code not in expect:
            raise ApiError(f"{label}: {method} {url} returned {response.status_code}: {response.text[:400]}")
        if not response.text:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def admin_token(self) -> str:
        if self._admin_token:
            return self._admin_token
        payload = self._request(
            "POST", self.path("kc_token"), "keycloak admin token",
            form={
                "grant_type": "client_credentials",
                "client_id": self.config["admin_client_id"],
                "client_secret": self.config["admin_client_secret"],
            },
        )
        self._admin_token = payload["access_token"]
        return self._admin_token

    def find_user(self, username: str) -> dict[str, Any] | None:
        matches = self._request(
            "GET", self.path("kc_users"), f"find user {username}", token=self.admin_token(),
            params={"username": username, "exact": "true", "max": 2},
        )
        return matches[0] if matches else None

    def find_users_by_prefix(self, prefix: str) -> list[dict[str, Any]]:
        matches = self._request(
            "GET", self.path("kc_users"), f"find users like {prefix}", token=self.admin_token(),
            params={"username": prefix, "max": 500},
        )
        return [u for u in matches if str(u.get("username", "")).startswith(prefix)]

    def delete_user(self, user_id: str, username: str) -> None:
        self._request(
            "DELETE", self.path("kc_user", user_id=user_id), f"delete {username}",
            token=self.admin_token(), expect=(204, 404),
        )


# ---------------------------------------------------------------------- sweep

def select_targets(kc: Keycloak, config: dict[str, Any], usernames: list[str]) -> list[dict[str, Any]]:
    target = config["target"]
    protected = {u.lower() for u in target.get("protected_usernames", []) if u}
    chosen: dict[str, dict[str, Any]] = {}

    for name in usernames:
        if name.lower() in protected:
            LOG.info("protected account, never deleted: %s", name)
            continue
        found = kc.find_user(name)
        if not found:
            LOG.info("no such user, already gone: %s", name)
            continue
        chosen[found["id"]] = found

    prefix = target.get("prefix")
    if prefix:
        hours = float(target.get("older_than_hours") or 0)
        cutoff_ms = int((time.time() - hours * 3600) * 1000) if hours > 0 else None
        for user in kc.find_users_by_prefix(prefix):
            name = str(user.get("username", ""))
            if name.lower() in protected:
                LOG.info("protected account, never deleted: %s", name)
                continue
            created = user.get("createdTimestamp") or 0
            if cutoff_ms is not None and created > cutoff_ms:
                LOG.info("younger than %sh, left alone: %s", hours, name)
                continue
            chosen[user["id"]] = user

    return list(chosen.values())


def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(str(args.log_level or config["logging"]["level"]).upper())

    usernames = [str(u) for u in (config["target"].get("usernames") or []) if u]
    usernames += list(args.username or [])
    validate(config, usernames)

    if requests is None:
        raise ConfigError("the 'requests' package is not installed: pip install -r ../requirements.txt")

    kc = Keycloak(config["keycloak"], config["endpoints"])
    problems: list[str] = []
    targets = select_targets(kc, config, usernames)
    if not targets:
        LOG.info("nothing to delete")
        return EXIT_OK

    LOG.info("%d account(s) selected", len(targets))
    deleted_ids: list[str] = []
    for user in targets:
        name = str(user.get("username", ""))
        if args.dry_run:
            LOG.info("DRY RUN — would delete %s (%s)", name, user["id"])
            deleted_ids.append(user["id"])
            continue
        try:
            kc.delete_user(user["id"], name)
            LOG.info("deleted %s (%s)", name, user["id"])
            deleted_ids.append(user["id"])
        except ApiError as exc:
            problems.append(f"delete {name}: {exc}")

    if config["delete"]["verify"] and not args.dry_run and config["target"].get("prefix"):
        # Only the prefix is re-listed: a named account that was deleted has
        # already answered 204, and a protected one is meant to still be there.
        protected = {u.lower() for u in config["target"].get("protected_usernames", []) if u}
        survivors = [
            str(u.get("username")) for u in kc.find_users_by_prefix(config["target"]["prefix"])
            if str(u.get("username", "")).lower() not in protected and u["id"] in deleted_ids
        ]
        for name in survivors:
            problems.append(f"{name} still exists after delete")

    if config["delete"]["print_user_ids"] and deleted_ids:
        # These are the database's user ids too (user_table._id is the Keycloak
        # sub), which is what database_sweep needs once the accounts are gone.
        LOG.info("user ids, for database_sweep's target.user_ids:\n%s", json.dumps(deleted_ids, indent=2))

    if problems:
        for problem in problems:
            LOG.error("%s", problem)
        return EXIT_FAILED
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config", nargs="?", type=Path,
        default=Path(__file__).with_name("keycloak_user_sweep_config.json"),
        help="JSON config path (default: keycloak_user_sweep_config.json beside this script)",
    )
    parser.add_argument("--username", action="append", help="an account to delete, in addition to target.usernames")
    parser.add_argument("--dry-run", action="store_true", help="list who would be deleted, delete nobody")
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
