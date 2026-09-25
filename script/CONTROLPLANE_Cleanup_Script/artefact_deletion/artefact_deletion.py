#!/usr/bin/env python3
"""Delete ControlPlane artefacts by id through the platform's own DELETE APIs.

Usage:
    python artefact_deletion.py                               # artefact_deletion_config.json beside this script
    python artefact_deletion.py my_config.json
    python artefact_deletion.py --type subscription --id <uuid> [--type app --id <uuid> ...]
    python artefact_deletion.py --dry-run

These are the deletions the Postman collection makes in its own teardown
folders. Each one is a single request with an id in it and needs the right
account: what a consumer created, a consumer deletes; a delegation or an app,
the provider; a resource server, the cos_admin. When a run dies before its
teardown, or something was created by hand, this is the script to paste the
ids into.

    type                endpoint                                          actor
    ----                --------                                          -----
    resource_server     DELETE /iudx/v2/resource_servers/{id}             cos_admin
    credit_request      DELETE /iudx/v2/auth/user/credit/request/{id}     consumer
    compute_request     DELETE /iudx/v2/auth/user/compute/requests/{id}   consumer
    delegation          DELETE /iudx/v2/auth/delegation/{id}              provider
    asset_request       DELETE /iudx/v2/auth/asset/request/{id}           provider
    subscription        DELETE /iudx/v2/subscriptions/{id}                consumer
    app                 DELETE /iudx/v2/auth/app/{id}                     provider
    user_feedback       DELETE /iudx/v2/user/feedback/{id}                consumer
    provider_feedback   DELETE /iudx/v2/provider/feedback?id={id}         provider
    policy              PUT    /iudx/acl/apd/v2/policy?id={id}  (ACL)     provider
    item                DELETE /iudx/v2/cat/item?id={id}                  provider

`item` is here for completeness; item_deletion.py is the fuller tool for it,
since it deactivates the item's policies first. Every actor is a login (or a
ready token) in `actors.*`; `types.<type>.actor` picks which one a type uses,
so a type created under a different role than the table above can be pointed
at any of the three.

A 404 is treated as done — the artefact is gone, which is the outcome wanted.
Rows that outlive their API delete (the platform soft-deletes several of
these) are database_sweep.py's job.

Exit codes:
    0  every artefact gone, or already gone
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
from pathlib import Path
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # allows --help before install
    requests = None  # type: ignore[assignment]

LOG = logging.getLogger("artefact_deletion")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

# type -> (method, path template, where the id goes, default actor, server)
# "path" puts the id in the URL, "query" as ?id=.
TYPES: dict[str, dict[str, str]] = {
    "resource_server":   {"method": "DELETE", "path": "/iudx/v2/resource_servers/{id}", "id_in": "path", "actor": "cos_admin", "server": "control_plane", "ok": "204"},
    "credit_request":    {"method": "DELETE", "path": "/iudx/v2/auth/user/credit/request/{id}", "id_in": "path", "actor": "consumer", "server": "control_plane", "ok": "200"},
    "compute_request":   {"method": "DELETE", "path": "/iudx/v2/auth/user/compute/requests/{id}", "id_in": "path", "actor": "consumer", "server": "control_plane", "ok": "200"},
    "delegation":        {"method": "DELETE", "path": "/iudx/v2/auth/delegation/{id}", "id_in": "path", "actor": "provider", "server": "control_plane", "ok": "200"},
    "asset_request":     {"method": "DELETE", "path": "/iudx/v2/auth/asset/request/{id}", "id_in": "path", "actor": "provider", "server": "control_plane", "ok": "200"},
    "subscription":      {"method": "DELETE", "path": "/iudx/v2/subscriptions/{id}", "id_in": "path", "actor": "consumer", "server": "control_plane", "ok": "200"},
    "app":               {"method": "DELETE", "path": "/iudx/v2/auth/app/{id}", "id_in": "path", "actor": "provider", "server": "control_plane", "ok": "200"},
    "user_feedback":     {"method": "DELETE", "path": "/iudx/v2/user/feedback/{id}", "id_in": "path", "actor": "consumer", "server": "control_plane", "ok": "200"},
    "provider_feedback": {"method": "DELETE", "path": "/iudx/v2/provider/feedback", "id_in": "query", "actor": "provider", "server": "control_plane", "ok": "200"},
    "policy":            {"method": "PUT", "path": "/iudx/acl/apd/v2/policy", "id_in": "query", "actor": "provider", "server": "acl", "ok": "200"},
    "item":              {"method": "DELETE", "path": "/iudx/v2/cat/item", "id_in": "query", "actor": "provider", "server": "control_plane", "ok": "200"},
}

DEFAULT_CONFIG: dict[str, Any] = {
    "control_plane": {
        "base_url": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    "acl": {
        # The policy server; empty means the same host as control_plane.
        "base_url": "",
    },
    "keycloak": {
        "url": "",
        "realm": "",
        "user_client_id": "postman-client",
        "user_client_secret": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    # Only the actors the targets need have to be filled in. A token skips the
    # sign-in.
    "actors": {
        "cos_admin": {"username": "", "password": "", "token": ""},
        "provider": {"username": "", "password": "", "token": ""},
        "consumer": {"username": "", "password": "", "token": ""},
    },
    # Ids to delete, per type. Every key of TYPES is accepted here.
    "target": {name: [] for name in TYPES},
    # Per-type overrides: {"app": {"actor": "consumer"}} when an app was created
    # by a consumer on this deployment, for example.
    "types": {},
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


def spec_for(config: dict[str, Any], kind: str) -> dict[str, str]:
    if kind not in TYPES:
        raise ConfigError(f"unknown type {kind!r}; one of: {', '.join(TYPES)}")
    return {**TYPES[kind], **(config["types"].get(kind) or {})}


def collect_targets(config: dict[str, Any], args: argparse.Namespace) -> list[tuple[str, str]]:
    """[(type, id)] from the config and the command line, in that order."""
    targets: list[tuple[str, str]] = []
    for kind, ids in (config["target"] or {}).items():
        if kind not in TYPES:
            raise ConfigError(f"target.{kind}: unknown type; one of: {', '.join(TYPES)}")
        for value in ids or []:
            if value:
                targets.append((kind, str(value)))
    kinds, ids = list(args.type or []), list(args.id or [])
    if len(kinds) != len(ids):
        raise ConfigError("--type and --id must be given in pairs")
    for kind, value in zip(kinds, ids):
        spec_for(config, kind)
        targets.append((kind, value))
    return targets


def validate(config: dict[str, Any], targets: list[tuple[str, str]]) -> None:
    require(config["control_plane"]["base_url"], "control_plane.base_url")
    if not targets:
        raise ConfigError("nothing to delete: fill target.<type> in the config or pass --type/--id")
    needed = {spec_for(config, kind)["actor"] for kind, _ in targets}
    for actor in sorted(needed):
        block = config["actors"].get(actor)
        if not isinstance(block, dict):
            raise ConfigError(f"actors.{actor} is not defined")
        if block.get("token"):
            continue
        require(block.get("username"), f"actors.{actor}.username")
        require(block.get("password"), f"actors.{actor}.password")
        require(config["keycloak"]["url"], "keycloak.url")
        require(config["keycloak"]["realm"], "keycloak.realm")


# ----------------------------------------------------------------------- http

class Http:
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
        if response.status_code not in expect:
            body = payload if isinstance(payload, str) else json.dumps(payload)
            raise ApiError(f"{label}: {method} {url} returned {response.status_code}: {body[:400]}")
        return response.status_code, payload


def user_token(config: dict[str, Any], actor: str) -> str:
    block = config["actors"][actor]
    if block.get("token"):
        return block["token"]
    kc = config["keycloak"]
    http = Http(kc["url"], kc["timeout_seconds"], kc["verify_tls"])
    form = {
        "grant_type": "password",
        "client_id": kc["user_client_id"],
        "username": block["username"],
        "password": block["password"],
    }
    if kc.get("user_client_secret"):
        form["client_secret"] = kc["user_client_secret"]
    _, payload = http.request(
        "POST", f"/realms/{kc['realm']}/protocol/openid-connect/token",
        f"token for {actor} ({block['username']})", form=form,
    )
    return payload["access_token"]


# ------------------------------------------------------------------- deletion

def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(str(args.log_level or config["logging"]["level"]).upper())
    targets = collect_targets(config, args)
    validate(config, targets)
    if requests is None:
        raise ConfigError("the 'requests' package is not installed: pip install -r ../requirements.txt")

    cp_conf = config["control_plane"]
    servers = {
        "control_plane": Http(cp_conf["base_url"], cp_conf["timeout_seconds"], cp_conf["verify_tls"]),
        "acl": Http(config["acl"].get("base_url") or cp_conf["base_url"], cp_conf["timeout_seconds"], cp_conf["verify_tls"]),
    }
    tokens: dict[str, str] = {}
    problems: list[str] = []
    done = 0

    for kind, value in targets:
        spec = spec_for(config, kind)
        label = f"{kind} {value}"
        method, actor = spec["method"], spec["actor"]
        if spec["id_in"] == "query":
            path, params = spec["path"], {"id": value}
        else:
            path, params = spec["path"].format(id=value), None
        if args.dry_run:
            LOG.info("DRY RUN — would %s %s%s as %s", method, path, f"?id={value}" if params else "", actor)
            done += 1
            continue
        try:
            if actor not in tokens:
                tokens[actor] = user_token(config, actor)
            ok = tuple(int(code) for code in str(spec["ok"]).split(",")) + (404,)
            status, _ = servers[spec["server"]].request(
                method, path, f"delete {label}", token=tokens[actor], params=params, expect=ok,
            )
        except ApiError as exc:
            if kind == "policy" and "not ACTIVE" in str(exc):
                LOG.info("%s was already inactive", label)
                done += 1
                continue
            problems.append(f"{label}: {exc}")
            LOG.error("%s", problems[-1])
            continue
        LOG.info("%s %s", label, "was already gone" if status == 404 else "deleted")
        done += 1

    LOG.info("%d of %d artefact(s) %s", done, len(targets), "would be handled" if args.dry_run else "handled")
    return EXIT_FAILED if problems else EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config", nargs="?", type=Path,
        default=Path(__file__).with_name("artefact_deletion_config.json"),
        help="JSON config path (default: artefact_deletion_config.json beside this script)",
    )
    parser.add_argument("--type", action="append", choices=sorted(TYPES), help="artefact type, paired with the next --id")
    parser.add_argument("--id", action="append", help="artefact id, paired with the preceding --type")
    parser.add_argument("--dry-run", action="store_true", help="show the requests that would be made, make none")
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
