#!/usr/bin/env python3
"""Remove the consumer accounts the submission script created.

Usage:
    python challenge_submission_cleanup.py challenge_submission_cleanup_config.json
    python challenge_submission_cleanup.py challenge_submission_cleanup_config.json --dry-run

The submission script makes one submission per account and, when asked for
more accounts than were listed, creates the rest in Keycloak. This script
reads the handoff file it wrote (`target.input_file`) and deletes exactly
those accounts — the ones flagged `created_user: true` — through the Keycloak
Admin API, then removes the handoff file. Accounts that were listed in the
submission config are never touched unless `delete.configured_accounts` is
true.

The submissions themselves stay: the community layer has no delete route for
a submission, and its own `users` row for the account is not tied to
Keycloak. Both go when the challenge is deleted — through the deletion script
while it is DRAFT or SCHEDULED, or in the database once it is PUBLISHED.

Everything the script touches comes from the JSON config. Values may
reference the environment as ${VAR} or ${VAR:-fallback}.
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
except ModuleNotFoundError:  # Allows --help and config validation before install.
    requests = None  # type: ignore[assignment]


LOG = logging.getLogger("challenge_submission_cleanup")

DEFAULT_CONFIG: dict[str, Any] = {
    "keycloak": {
        "url": "",
        "realm": "",
        # A confidential client with realm user management.
        "admin_client_id": "",
        "admin_client_secret": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    "target": {
        "input_file": "../submission/submissions_created.json",
        "require_input_file": True,
    },
    "delete": {
        # Accounts flagged created_user in the handoff file.
        "created_accounts": True,
        # Accounts that came from consumers.accounts in the submission config.
        "configured_accounts": False,
        # An account that is already gone counts as deleted.
        "missing_ok": True,
        # Confirm each id answers 404 afterwards.
        "verify_after": True,
        # Remove the handoff file once every account it names is gone.
        "remove_input_file": True,
        "pause_seconds": 0,
    },
    "endpoints": {
        "kc_token": "/realms/{realm}/protocol/openid-connect/token",
        "kc_users": "/admin/realms/{realm}/users",
        "kc_user": "/admin/realms/{realm}/users/{user_id}",
    },
    "logging": {
        "level": "INFO",
        "print_requests": True,
        "print_responses": True,
        "response_preview_chars": 2000,
        "mask_secrets": False,
    },
}


class ConfigError(ValueError):
    """A required configuration value is absent or invalid."""


class ApiError(RuntimeError):
    """Keycloak rejected a request."""


# --------------------------------------------------------------------- config

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
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


def require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"'{field_name}' must be a non-empty string")
    return value.strip()


def require_requests() -> None:
    if requests is None:
        raise ConfigError(
            "missing Python dependency 'requests'; run "
            "'python -m pip install -r ../requirements.txt'"
        )


# ------------------------------------------------------------------- output

OUTPUT = {
    "print_responses": True,
    "print_requests": True,
    "preview_chars": 2000,
    "mask_secrets": False,
}


def configure_output(config: dict[str, Any]) -> None:
    settings = config.get("logging") or {}
    OUTPUT["print_responses"] = bool(settings.get("print_responses", True))
    OUTPUT["print_requests"] = bool(settings.get("print_requests", True))
    OUTPUT["preview_chars"] = int(settings.get("response_preview_chars") or 2000)
    OUTPUT["mask_secrets"] = bool(settings.get("mask_secrets", False))


_SECRET_KEYS = ("password", "secret", "token", "credentials", "authorization")
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")


def scrub(value: Any, _depth: int = 0) -> Any:
    if not OUTPUT["mask_secrets"]:
        return value
    if _depth > 6:
        return "..."
    if isinstance(value, dict):
        return {
            key: "***" if any(hint in str(key).lower() for hint in _SECRET_KEYS)
            else scrub(item, _depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub(item, _depth + 1) for item in value[:25]]
    if isinstance(value, str):
        return _JWT.sub("***jwt***", value)
    return value


def preview(payload: Any) -> str:
    if payload is None or payload == "":
        return "(no content)"
    try:
        text = json.dumps(scrub(payload), default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(payload)
    limit = OUTPUT["preview_chars"]
    return text if limit <= 0 or len(text) <= limit else text[:limit] + "…"


def log_request(label: str, method: str, path: str, body: Any = None,
                params: dict[str, Any] | None = None) -> None:
    if not OUTPUT["print_requests"]:
        return
    detail = ""
    if params:
        detail += f" params={preview(params)}"
    if body is not None:
        detail += f" body={preview(body)}"
    LOG.info("→ %s: %s %s%s", label, method, path, detail)


def log_response(label: str, method: str, path: str, status: int, payload: Any) -> None:
    if not OUTPUT["print_responses"]:
        return
    LOG.info("← %s: %s %s → %s | %s", label, method, path, status, preview(payload))


def log_block(title: str, fields: dict[str, Any]) -> None:
    shown = scrub(dict(fields))
    LOG.info("%s", title)
    width = max((len(str(k)) for k in shown), default=0)
    for key, value in shown.items():
        LOG.info("    %-*s  %s", width, key, "" if value is None else value)


# ------------------------------------------------------------------- keycloak


class Keycloak:
    """The Keycloak Admin API with a client-credentials token."""

    def __init__(self, config: dict[str, Any], endpoints: dict[str, Any]):
        self.base_url = require_string(config.get("url"), "keycloak.url").rstrip("/")
        self.realm = require_string(config.get("realm"), "keycloak.realm")
        self.admin_client_id = require_string(
            config.get("admin_client_id"), "keycloak.admin_client_id"
        )
        self.admin_client_secret = str(config.get("admin_client_secret") or "")
        self.timeout = float(config.get("timeout_seconds") or 30)
        self.endpoints = endpoints
        self.session = requests.Session()
        self.session.verify = bool(config.get("verify_tls", True))
        self._admin_token = ""
        self._admin_expiry = 0.0

    def path(self, name: str, **fields: str) -> str:
        template = self.endpoints.get(name)
        if not isinstance(template, str) or not template:
            raise ConfigError(f"'endpoints.{name}' must be a non-empty string")
        return template.format(realm=self.realm, **fields)

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def admin_token(self) -> str:
        if self._admin_token and time.monotonic() < self._admin_expiry:
            return self._admin_token
        path = self.path("kc_token")
        form = {
            "grant_type": "client_credentials",
            "client_id": self.admin_client_id,
            "client_secret": self.admin_client_secret,
        }
        log_request("keycloak admin token", "POST", path, body=form)
        response = self.session.post(
            self.url(path), data=form, timeout=self.timeout,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            log_response("keycloak admin token", "POST", path, response.status_code, response.text[:400])
            raise ApiError(f"keycloak admin token failed ({response.status_code}): {response.text[:400]}")
        payload = response.json()
        log_response("keycloak admin token", "POST", path, response.status_code, payload)
        self._admin_token = payload["access_token"]
        self._admin_expiry = time.monotonic() + max(payload.get("expires_in", 60) - 30, 10)
        return self._admin_token

    def request(self, method: str, path: str, label: str,
                expect: tuple[int, ...] = (200,)) -> tuple[int, Any]:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.admin_token()}"}
        log_request(label, method, path)
        response = self.session.request(method, self.url(path), headers=headers, timeout=self.timeout)
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        log_response(label, method, path, response.status_code, payload)
        if response.status_code not in expect:
            raise ApiError(
                f"{label}: {method} {path} returned {response.status_code}, "
                f"expected {' or '.join(str(code) for code in expect)}\n{response.text[:400]}"
            )
        return response.status_code, payload


# --------------------------------------------------------------------- run


def resolve_path(name: str, config_path: Path) -> Path:
    candidate = Path(name)
    return candidate if candidate.is_absolute() else config_path.resolve().parent / candidate


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        LOG.warning("Ignoring unreadable file %s", path)
        return {}
    return record if isinstance(record, dict) else {}


def planned_deletions(config: dict[str, Any], record: dict[str, Any]) -> list[dict[str, Any]]:
    delete = config["delete"]
    plan = []
    for entry in record.get("submissions") or []:
        if not isinstance(entry, dict):
            continue
        created = bool(entry.get("created_user"))
        if created and not delete.get("created_accounts", True):
            continue
        if not created and not delete.get("configured_accounts", False):
            continue
        plan.append(entry)
    return plan


def delete_account(config: dict[str, Any], kc: Keycloak, entry: dict[str, Any]) -> str:
    """Delete one account; returns 'deleted', 'missing' or raises."""
    delete = config["delete"]
    user_id = str(entry.get("keycloak_user_id") or "").strip()
    username = str(entry.get("username") or "").strip()
    label = f"consumer #{entry.get('index')} {username or user_id}"
    if not user_id and username:
        _, matches = kc.request(
            "GET", kc.path("kc_users") + f"?username={username}&exact=true&max=2",
            f"find {label}",
        )
        if matches:
            user_id = str(matches[0].get("id") or "")
    if not user_id:
        if delete.get("missing_ok", True):
            LOG.info("%s: not in Keycloak — nothing to delete", label)
            return "missing"
        raise ApiError(f"{label}: not found in Keycloak")
    status, _ = kc.request(
        "DELETE", kc.path("kc_user", user_id=user_id), f"delete {label}",
        expect=(204, 404) if delete.get("missing_ok", True) else (204,),
    )
    if status == 404:
        LOG.info("%s: already gone", label)
        return "missing"
    if delete.get("verify_after", True):
        kc.request("GET", kc.path("kc_user", user_id=user_id), f"confirm {label} is gone",
                   expect=(404,))
    LOG.info("Deleted %s (%s)", label, user_id)
    return "deleted"


def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(
        str(args.log_level or config["logging"].get("level") or "INFO").upper()
    )
    configure_output(config)

    input_file = resolve_path(require_string(config["target"].get("input_file"),
                                             "target.input_file"), config_path)
    record = read_json(input_file)
    if not record:
        if config["target"].get("require_input_file", True):
            raise ConfigError(f"target.input_file {input_file} is missing or unreadable")
        LOG.info("No handoff file at %s — nothing to do", input_file)
        return 0
    LOG.info("Read %s", input_file)

    plan = planned_deletions(config, record)
    log_block("=== accounts to delete ===", {
        "challenge": record.get("competition_id"),
        "title": record.get("title"),
        "listed in file": len(record.get("submissions") or []),
        "to delete": ", ".join(str(e.get("username") or e.get("keycloak_user_id")) for e in plan) or "none",
        "keycloak": f"{config['keycloak'].get('url')} realm {config['keycloak'].get('realm')}",
    })

    if args.dry_run:
        LOG.info("Dry run — no calls will be made")
        for entry in plan:
            LOG.info("Would DELETE %s", entry.get("username") or entry.get("keycloak_user_id"))
        if config["delete"].get("remove_input_file", True):
            LOG.info("Would remove %s", input_file)
        return 0

    outcomes: dict[str, str] = {}
    if plan:
        require_requests()
        kc = Keycloak(config["keycloak"], config["endpoints"])
        pause = float(config["delete"].get("pause_seconds") or 0)
        for entry in plan:
            outcomes[str(entry.get("username") or entry.get("keycloak_user_id"))] = \
                delete_account(config, kc, entry)
            if pause > 0 and entry is not plan[-1]:
                time.sleep(pause)

    if config["delete"].get("remove_input_file", True):
        input_file.unlink()
        LOG.info("Removed %s", input_file)

    log_block("=== summary ===", {
        "deleted": sum(1 for o in outcomes.values() if o == "deleted"),
        "already gone": sum(1 for o in outcomes.values() if o == "missing"),
        "kept (configured accounts)": len(record.get("submissions") or []) - len(plan),
    })
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("challenge_submission_cleanup_config.json"),
        help="JSON config path (default: challenge_submission_cleanup_config.json beside this script)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would happen, change nothing")
    parser.add_argument("--log-level", help="override logging.level from the config")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        return run(args.config, args)
    except (ConfigError, ApiError, OSError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
