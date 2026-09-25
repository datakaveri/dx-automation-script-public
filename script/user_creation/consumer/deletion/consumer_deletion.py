#!/usr/bin/env python3
"""Delete one consumer user from the platform.

Usage:
    python consumer_deletion.py consumer_deletion_config.json
    python consumer_deletion.py consumer_deletion_config.json --username someone@example.invalid
    python consumer_deletion.py consumer_deletion_config.json --dry-run

Who to delete comes from `--username`, then `target.username`, then the handoff
file the creation script wrote — pointing `target.input_file` at that file is
what lets a teardown run with no ids typed out by hand.

`delete.mode` decides the route:

    auto      ControlPlane's own self-delete endpoint, which cascades into
              Keycloak and the database, falling back to the Keycloak Admin API
              if the platform refuses. (default)
    api       self-delete only; fail if the platform refuses.
    keycloak  the Keycloak Admin API only, leaving platform rows behind.

The platform refuses to self-delete org admins and platform admins; a plain
consumer is never one of those, so `auto` normally takes the cascading route.

Everything the script touches — URLs, credentials, endpoint paths, which route
to take, whether to verify — comes from the JSON config. Values may reference
the environment as ${VAR} or ${VAR:-fallback}.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # Allows --help and config validation before install.
    requests = None  # type: ignore[assignment]


LOG = logging.getLogger("consumer_deletion")

# Every key the script reads, with the value used when the config omits it. The
# shipped example config repeats these, so any of them can be pasted over.
DEFAULT_CONFIG: dict[str, Any] = {
    "control_plane": {
        "base_url": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    "keycloak": {
        "url": "",
        "realm": "",
        "admin_client_id": "",
        "admin_client_secret": "",
        "user_client_id": "",
        "user_client_secret": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    "target": {
        "username": "",
        "user_id": "",
        # Only the self-delete route needs this: it signs in as the account.
        "password": "",
        # The record the creation script wrote, in its own folder.
        "input_file": "../creation/consumer_created.json",
        "require_input_file": False,
        "remove_input_file": True,
    },
    "delete": {
        "mode": "auto",
        "pause_seconds": 0,
        "verify": True,
        "verify_timeout_seconds": 30,
        "verify_poll_seconds": 2,
    },
    "endpoints": {
        "kc_token": "/realms/{realm}/protocol/openid-connect/token",
        "kc_users": "/admin/realms/{realm}/users",
        "kc_user": "/admin/realms/{realm}/users/{user_id}",
        "kc_realm_role": "/admin/realms/{realm}/roles/{role}",
        "kc_role_mappings": "/admin/realms/{realm}/users/{user_id}/role-mappings/realm",
        "cp_user_delete": "/iudx/v2/auth/user/delete",
    },
    "logging": {
        "level": "INFO",
        # Print every request and every response as the run goes.
        "print_requests": True,
        "print_responses": True,
        "response_preview_chars": 2000,
        # Off means bodies print verbatim — passwords, client secrets and
        # access tokens included. Turn it on before sharing a log.
        "mask_secrets": False,
    },
}


class ConfigError(ValueError):
    """A required configuration value is absent or invalid."""


class ApiError(RuntimeError):
    """Keycloak or ControlPlane rejected a request."""


# --------------------------------------------------------------------- config

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Resolve ${VAR} / ${VAR:-fallback} anywhere in the config tree.

    Credentials can then live in the environment rather than in a file that gets
    copied around, without forcing that on anyone who would rather paste them in.
    """
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
    """Overlay the config onto the defaults, one nested level at a time."""
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
            "'python -m pip install -r ../../requirements.txt'"
        )


def cp_path(config: dict[str, Any], name: str, **fields: str) -> str:
    """One ControlPlane endpoint path, with its placeholders filled in."""
    template = config["endpoints"].get(name)
    if not isinstance(template, str) or not template:
        raise ConfigError(f"'endpoints.{name}' must be a non-empty string")
    try:
        return template.format(**fields)
    except KeyError as exc:
        raise ConfigError(f"'endpoints.{name}' uses an unknown placeholder {exc}") from exc


def field(row: Any, *names: str, default: Any = None) -> Any:
    """Read a field from an API row, whether it is flat or nested one level.

    The same record comes back camelCased from one endpoint and snake_cased from
    another, and sometimes grouped into a sub-object; callers should not have to
    track which shape each endpoint uses.
    """
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


# ------------------------------------------------------------------- output

# What every call returned, printed as the script goes. Set from the config by
# configure_output, so the classes below need no extra plumbing.
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


# Fields masked when logging.mask_secrets is on. It is off by default, so a run
# prints exactly what went over the wire — passwords, client secrets and access
# tokens included. That is what makes the output useful for checking a run by
# hand, and it is also why the output is not safe to paste into a ticket, a
# chat, or anywhere a log is kept: turn masking on for those.
_SECRET_KEYS = ("password", "secret", "token", "credentials", "authorization")

# A bare JWT echoed outside a named field — no key-based rule would catch it.
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")


def scrub(value: Any, _depth: int = 0) -> Any:
    """Copy a payload, masking credentials when the config asks for it."""
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
    """One line of a body, truncated to logging.response_preview_chars."""
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
    """Print what one call is about to send."""
    if not OUTPUT["print_requests"]:
        return
    detail = ""
    if params:
        detail += f" params={preview(params)}"
    if body is not None:
        detail += f" body={preview(body)}"
    LOG.info("→ %s: %s %s%s", label, method, path, detail)


def log_response(label: str, method: str, path: str, status: int, payload: Any) -> None:
    """Print what one call returned."""
    if not OUTPUT["print_responses"]:
        return
    LOG.info("← %s: %s %s → %s | %s", label, method, path, status, preview(payload))


def log_block(title: str, fields: dict[str, Any]) -> None:
    """Print a labelled block of details, one per line.

    Masked on the same switch as every other body, so turning masking on before
    sharing a log covers this block too rather than leaving the password in it.
    """
    shown = scrub(dict(fields))
    LOG.info("%s", title)
    width = max((len(str(k)) for k in shown), default=0)
    for key, value in shown.items():
        LOG.info("    %-*s  %s", width, key, "" if value is None else value)


def rows_of(payload: Any) -> list[Any]:
    """The list of records in a response, paginated or not."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "result", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
        # An endpoint holding one record returns it as a bare object rather
        # than a list of one — /organization/user/provider_requests does — so a
        # dict carrying an id is handed back as a single row.
        for key in ("id", "requestId", "request_id"):
            if payload.get(key):
                return [payload]
    return []


# ------------------------------------------------------------------- keycloak


class Keycloak:
    """The Keycloak Admin API, plus password-grant tokens for platform accounts.

    ControlPlane has no create-user API — users originate in Keycloak — so the
    Admin API is a hard dependency, and deletion then comes free with the same
    client.
    """

    def __init__(self, config: dict[str, Any], endpoints: dict[str, Any]):
        self.base_url = require_string(config.get("url"), "keycloak.url").rstrip("/")
        self.realm = require_string(config.get("realm"), "keycloak.realm")
        self.admin_client_id = require_string(
            config.get("admin_client_id"), "keycloak.admin_client_id"
        )
        self.admin_client_secret = str(config.get("admin_client_secret") or "")
        self.user_client_id = require_string(
            config.get("user_client_id"), "keycloak.user_client_id"
        )
        self.user_client_secret = str(config.get("user_client_secret") or "")
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

    # ------------------------------------------------------------- tokens

    def _form_post(self, path: str, label: str, form: dict[str, str]) -> dict[str, Any]:
        """Token endpoints take form encoding rather than JSON."""
        log_request(label, "POST", path, body=form)
        response = self.session.post(
            self.url(path),
            data=form,
            timeout=self.timeout,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            log_response(label, "POST", path, response.status_code, response.text[:400])
            raise ApiError(f"{label} failed ({response.status_code}): {response.text[:400]}")
        payload = response.json()
        log_response(label, "POST", path, response.status_code, payload)
        return payload

    def admin_token(self) -> str:
        """Client-credentials token for the Admin API, refreshed when stale."""
        if self._admin_token and time.monotonic() < self._admin_expiry:
            return self._admin_token
        payload = self._form_post(
            self.path("kc_token"),
            "keycloak admin token",
            {
                "grant_type": "client_credentials",
                "client_id": self.admin_client_id,
                "client_secret": self.admin_client_secret,
            },
        )
        self._admin_token = payload["access_token"]
        # Refresh early so a long step never runs on an expiring token.
        self._admin_expiry = time.monotonic() + max(payload.get("expires_in", 60) - 30, 10)
        return self._admin_token

    def user_token(self, username: str, password: str) -> str:
        """Password-grant token for one platform account.

        Roles and organisation attributes live in the token, so this has to be
        called again after any approval — a token minted earlier does not carry
        what the approval granted.
        """
        form = {
            "grant_type": "password",
            "client_id": self.user_client_id,
            "username": username,
            "password": password,
        }
        if self.user_client_secret:
            form["client_secret"] = self.user_client_secret
        payload = self._form_post(self.path("kc_token"), f"token for {username}", form)
        return payload["access_token"]

    # ---------------------------------------------------------- admin api

    def request(
        self,
        method: str,
        path: str,
        label: str,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        expect: tuple[int, ...] = (200,),
    ) -> Any:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.admin_token()}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        log_request(label, method, path, body=json_body, params=params)
        response = self.session.request(
            method,
            self.url(path),
            json=json_body,
            params=params,
            headers=headers,
            timeout=self.timeout,
        )
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
        return payload

    def create_user(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Create the account and return it as Keycloak actually stored it.

        The name asked for is not always the name kept: a realm with
        registrationEmailAsUsername set replaces the username with the email on
        create. Reading the account back by email as well as by username is what
        keeps a differing email from looking like a failed creation — and the
        caller needs the stored username, because that is what the password
        grant will accept.
        """
        self.request(
            "POST", self.path("kc_users"), f"create user {spec['username']}",
            json_body=spec, expect=(201,),
        )
        email = str(spec.get("email") or "")
        found = self.find_user(spec["username"])
        if not found and email:
            found = self.find_user(email) or self.find_by_email(email)
        if not found:
            raise ApiError(f"created user {spec['username']} but could not read it back")
        return found

    def find_user(self, username: str) -> dict[str, Any] | None:
        """Exact-username lookup, or None."""
        matches = self.request(
            "GET", self.path("kc_users"), f"find user {username}",
            params={"username": username, "exact": "true", "max": 2},
        )
        return matches[0] if matches else None

    def find_by_email(self, email: str) -> dict[str, Any] | None:
        """Exact-email lookup, or None — the other way an account can be found."""
        matches = self.request(
            "GET", self.path("kc_users"), f"find user by email {email}",
            params={"email": email, "exact": "true", "max": 2},
        )
        return matches[0] if matches else None

    def delete_user(self, user_id: str, label: str = "user") -> None:
        self.request(
            "DELETE", self.path("kc_user", user_id=user_id), f"delete {label}", expect=(204,)
        )

    def realm_roles(self, user_id: str) -> list[str]:
        mapped = self.request(
            "GET", self.path("kc_role_mappings", user_id=user_id), "read realm roles"
        )
        return sorted(role["name"] for role in mapped)

    def assign_realm_role(self, user_id: str, role_name: str) -> None:
        role = self.request(
            "GET", self.path("kc_realm_role", role=role_name), f"read role {role_name}"
        )
        self.request(
            "POST", self.path("kc_role_mappings", user_id=user_id), f"assign {role_name}",
            json_body=[{"id": role["id"], "name": role["name"]}], expect=(204,),
        )

    def attributes(self, user_id: str) -> dict[str, Any]:
        user = self.request("GET", self.path("kc_user", user_id=user_id), "read user attributes")
        return user.get("attributes") or {}

    def await_roles(
        self, user_id: str, expected: list[str], timeout: float, interval: float
    ) -> list[str]:
        """Wait for roles to appear — approval writes to Keycloak asynchronously."""
        wanted = set(expected)
        deadline = time.monotonic() + timeout
        seen: list[str] = []
        while True:
            seen = self.realm_roles(user_id)
            if wanted.issubset(set(seen)):
                return seen
            if time.monotonic() >= deadline:
                break
            time.sleep(interval)
        missing = ", ".join(sorted(wanted - set(seen)))
        raise ApiError(
            f"Keycloak did not gain role(s) {missing} within {timeout}s; has: {', '.join(seen)}"
        )


# -------------------------------------------------------------- controlplane


class ControlPlane:
    """The platform API, called as whichever account holds the token."""

    def __init__(self, config: dict[str, Any]):
        self.base_url = require_string(config.get("base_url"), "control_plane.base_url").rstrip("/")
        self.timeout = float(config.get("timeout_seconds") or 30)
        self.session = requests.Session()
        self.session.verify = bool(config.get("verify_tls", True))

    def request(
        self,
        method: str,
        path: str,
        label: str,
        token: str,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        expect: tuple[int, ...] = (200,),
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        log_request(label, method, path, body=json_body, params=params)
        response = self.session.request(
            method, url, json=json_body, params=params, headers=headers, timeout=self.timeout
        )
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        log_response(label, method, path, response.status_code, payload)
        if response.status_code not in expect:
            body = payload if isinstance(payload, str) else json.dumps(payload)[:400]
            raise ApiError(
                f"{label}: {method} {path} returned {response.status_code}, "
                f"expected {' or '.join(str(code) for code in expect)}\n{body}"
            )
        # Responses arrive in a {type,title,result} envelope; hand back the payload.
        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload


# --------------------------------------------------------------- handoff file


def resolve_path(name: str, config_path: Path) -> Path:
    """A configured file path, read relative to the config file that named it."""
    candidate = Path(name)
    return candidate if candidate.is_absolute() else config_path.resolve().parent / candidate


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    LOG.info("Wrote %s", path)


def read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        LOG.warning("Ignoring unreadable file %s", path)
        return {}
    return record if isinstance(record, dict) else {}


def actor_token(kc: Keycloak, block: dict[str, Any], label: str) -> str:
    """A ready-made token from the config, or one minted from credentials."""
    token = str(block.get("token") or "").strip()
    if token:
        LOG.info("Using the configured %s token", label)
        return token
    username = require_string(block.get("username"), f"{label}.username")
    password = require_string(block.get("password"), f"{label}.password")
    LOG.info("Signing in as %s %s", label, username)
    return kc.user_token(username, password)


# -------------------------------------------------------------------- target


def resolve_target(config: dict[str, Any], config_path: Path, override: str | None) -> dict[str, Any]:
    """Who to delete: the command line, then the config, then the handoff file.

    The creation script writes a small JSON record; pointing `target.input_file`
    at it is what makes a teardown need no ids typed out by hand.
    """
    target = config["target"]
    record: dict[str, Any] = {}
    input_file = str(target.get("input_file") or "").strip()
    if input_file:
        path = resolve_path(input_file, config_path)
        record = read_json(path)
        if record:
            LOG.info("Read %s", path)
        elif target.get("require_input_file", False):
            raise ConfigError(f"target.input_file {path} is missing or unreadable")

    # What the config and command line say outright. These win, and they also
    # decide whether the handoff record is about the same account at all.
    asked_username = (override or str(target.get("username") or "")).strip()
    asked_user_id = str(target.get("user_id") or "").strip()

    # The record describes one account. If either identifier given here names a
    # different one, the rest of that file — the other id, the password — belongs
    # to somebody else, and using it would delete or sign into the wrong account.
    # So the record is dropped whole rather than merged field by field.
    recorded_username = str(record.get("username") or "").strip()
    recorded_user_id = str(record.get("user_id") or "").strip()
    conflict = (
        (asked_username and recorded_username and asked_username != recorded_username)
        or (asked_user_id and recorded_user_id and asked_user_id != recorded_user_id)
    )
    if record and conflict:
        LOG.warning(
            "%s describes %s (%s), which is not the account asked for — ignoring "
            "that file and using only the config and command line",
            input_file, recorded_username or "?", recorded_user_id or "?",
        )
        record = {}

    username = asked_username or str(record.get("username") or "").strip()
    user_id = asked_user_id or str(record.get("user_id") or "").strip()
    if not username and not user_id:
        raise ConfigError(
            "no account to delete: pass --username, set 'target.username' or "
            "'target.user_id', or point 'target.input_file' at the file the "
            "creation script wrote"
        )

    resolved = {
        "username": username,
        "user_id": user_id,
        "password": str(target.get("password") or "").strip() or str(record.get("password") or "").strip(),
        "org_id": str(target.get("org_id") or "").strip() or str(record.get("org_id") or "").strip(),
        "org_name": str(record.get("org_name") or "").strip(),
    }
    log_block("=== account to delete ===", {
        **resolved,
        "keycloak_realm": config["keycloak"].get("realm"),
        "keycloak_url": config["keycloak"].get("url"),
        "control_plane": config["control_plane"].get("base_url"),
        "delete_mode": config["delete"].get("mode"),
    })
    return resolved


def ensure_username(kc: Keycloak, target: dict[str, Any]) -> None:
    """Fill in the username when only a Keycloak id was given.

    The sign-in and the after-the-fact "is it really gone" check are both by
    name, so an id-only target needs the name looked up once.
    """
    if target.get("username") or not target.get("user_id"):
        return
    found = kc.request(
        "GET", kc.path("kc_user", user_id=target["user_id"]),
        f"read user {target['user_id']}",
    )
    target["username"] = str(found.get("username") or "")
    if not target["username"]:
        raise ApiError(f"Keycloak id {target['user_id']} has no username")
    LOG.info("Keycloak id %s is %s", target["user_id"], target["username"])


def keycloak_delete(kc: Keycloak, target: dict[str, Any]) -> bool:
    """Remove the account through the Admin API. True when it is gone."""
    user_id = target.get("user_id") or ""
    if not user_id:
        found = kc.find_user(target["username"])
        if not found:
            LOG.info("User %s is already absent from Keycloak", target["username"])
            return True
        user_id = found["id"]
        target["user_id"] = user_id
    kc.delete_user(user_id, target["username"])
    LOG.info("Deleted %s through the Keycloak Admin API", target["username"])
    return True


def self_delete(
    config: dict[str, Any], kc: Keycloak, cp: ControlPlane, target: dict[str, Any]
) -> None:
    """Remove the account through ControlPlane, as the account itself.

    It unwinds the platform side — realm roles, organisation membership and
    database rows — and answers "User deleted successfully from Keycloak and
    DB". Do not take the Keycloak half of that at face value: on this
    deployment the account itself survives, so auto checks and finishes the job
    through the Admin API. It also refuses org admins and platform admins,
    which is the other reason callers have a fallback.
    """
    password = target.get("password") or str(config["target"].get("password") or "")
    if not password:
        raise ConfigError(
            "the ControlPlane self-delete signs in as the account, so it needs "
            "'target.password' (or a password in the handoff file)"
        )
    token = kc.user_token(target["username"], password)
    cp.request(
        "DELETE", cp_path(config, "cp_user_delete"),
        f"self-delete {target['username']}", token=token,
    )
    LOG.info(
        "Deleted %s through ControlPlane (cascades into Keycloak and the database)",
        target["username"],
    )


def verify_gone(config: dict[str, Any], kc: Keycloak, username: str) -> None:
    """Confirm the account really is gone before reporting success."""
    delete = config["delete"]
    if not delete.get("verify", True):
        return
    deadline = time.monotonic() + float(delete.get("verify_timeout_seconds") or 30)
    interval = float(delete.get("verify_poll_seconds") or 2)
    while True:
        if kc.find_user(username) is None:
            LOG.info("Verified: %s is gone from Keycloak", username)
            return
        if time.monotonic() >= deadline:
            raise ApiError(f"{username} is still present in Keycloak after deletion")
        time.sleep(interval)


def consume_input_file(config: dict[str, Any], config_path: Path, username: str) -> None:
    """Remove the handoff file once the account it described is gone.

    Only when it described *this* account: deleting one named on the command
    line must not throw away the record of a different, still-live one.
    """
    target = config["target"]
    if not target.get("remove_input_file", True):
        return
    name = str(target.get("input_file") or "").strip()
    if not name:
        return
    path = resolve_path(name, config_path)
    if not path.is_file():
        return
    recorded = str(read_json(path).get("username") or "").strip()
    if recorded and recorded != username:
        LOG.info("Leaving %s in place — it names %s, which is still there", path, recorded)
        return
    path.unlink()
    LOG.info("Removed %s", path)


# -------------------------------------------------------------------- steps


def delete_consumer(
    config: dict[str, Any], kc: Keycloak, cp: ControlPlane, target: dict[str, Any]
) -> bool:
    """Remove the account by whichever route the config asks for."""
    ensure_username(kc, target)
    delete = config["delete"]
    mode = str(delete.get("mode") or "auto").lower()
    if mode not in ("auto", "api", "keycloak"):
        raise ConfigError(f"unknown delete.mode '{mode}'; expected auto, api or keycloak")

    pause = float(delete.get("pause_seconds") or 0)
    if pause > 0:
        LOG.info("Waiting %.0fs before deleting", pause)
        time.sleep(pause)

    removed = False
    if mode in ("auto", "api"):
        try:
            self_delete(config, kc, cp, target)
            removed = True
        except (ApiError, ConfigError) as exc:
            if mode == "api":
                raise
            LOG.warning("Self-delete did not work (%s); using the Keycloak Admin API", exc)

    # This deployment's self-delete strips the platform identity — realm roles,
    # organisation attributes and database rows — but leaves the Keycloak
    # account standing, whatever its "deleted from Keycloak and DB" message
    # says. In auto the account still has to go, so finish through the Admin API.
    if removed and mode == "auto" and kc.find_user(target["username"]) is not None:
        LOG.info(
            "The platform unwound %s but its Keycloak account is still there — "
            "removing it through the Admin API",
            target["username"],
        )
        removed = keycloak_delete(kc, target)

    if not removed:
        removed = keycloak_delete(kc, target)

    verify_gone(config, kc, target["username"])
    return removed


# ---------------------------------------------------------------------- run


def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(
        str(args.log_level or config["logging"].get("level") or "INFO").upper()
    )
    configure_output(config)

    target = resolve_target(config, config_path, args.username)

    if args.dry_run:
        LOG.info("Dry run — no calls will be made")
        LOG.info("Keycloak realm %s at %s",
                 config["keycloak"].get("realm"), config["keycloak"].get("url"))
        LOG.info("ControlPlane at %s", config["control_plane"].get("base_url"))
        LOG.info("Would delete %s using mode '%s'",
                 target["username"], config["delete"].get("mode"))
        return 0

    require_requests()
    kc = Keycloak(config["keycloak"], config["endpoints"])
    cp = ControlPlane(config["control_plane"])

    delete_consumer(config, kc, cp, target)
    consume_input_file(config, config_path, target["username"])
    LOG.info("Deleted consumer %s", target["username"])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("consumer_deletion_config.json"),
        help="JSON config path (default: consumer_deletion_config.json beside this script)",
    )
    parser.add_argument("--username", help="the account to delete, overriding the config")
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
