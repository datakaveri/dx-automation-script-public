#!/usr/bin/env python3
"""Create one organisation admin on the platform.

Usage:
    python org_admin_creation.py org_admin_creation_config.json
    python org_admin_creation.py org_admin_creation_config.json --org-name my-org
    python org_admin_creation.py org_admin_creation_config.json --dry-run

An organisation admin is made, not assigned: the user asks for an organisation
of their own and the COS admin approves the request, and it is that approval
which creates the organisation and writes the `org_admin` and `provider` roles
into Keycloak. So this script needs a COS admin's credentials, or a ready-made
COS admin token, as well as the Keycloak Admin API.

What the account and its organisation became is written to the handoff file
named by `output.file`, which the deletion script beside this one reads — that
script needs the organisation id, and this is where it gets it.

Everything the script touches — URLs, credentials, endpoint paths, the whole
organisation payload, which roles to assert — comes from the JSON config. Values
may reference the environment as ${VAR} or ${VAR:-fallback}.
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


LOG = logging.getLogger("org_admin_creation")

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
    "user": {
        # Blank builds a name from prefix + username_template below.
        "username": "",
        "email": "",
        "password": "",
        "first_name": "Org",
        "last_name": "Admin",
        "prefix": "user-orgadmin",
        "email_domain": "example.invalid",
        # How a generated username is shaped. Placeholders: {prefix},
        # {timestamp}, {random}, {domain}. Set user.username instead to name
        # one account outright.
        "username_template": "{prefix}-{timestamp}-{random}@{domain}",
        "timestamp_format": "%Y%m%d%H%M%S",
        "random_hex_bytes": 2,
        "enabled": True,
        "email_verified": True,
        "temporary_password": False,
        # Keycloak required actions on the new account, e.g. ["VERIFY_EMAIL"].
        # An account carrying one cannot use the password grant until it is done.
        "required_actions": [],
        "attributes": {},
        "realm_roles": [],
        "reuse_existing": False,
    },
    # Whoever approves the organisation create request. A ready-made token is
    # used as-is; otherwise the script signs the account in.
    "cos_admin": {
        "username": "",
        "password": "",
        "token": "",
    },
    "organisation": {
        # Blank generates <name_prefix>-<timestamp>.
        "name": "",
        "name_prefix": "adm-org",
        # The OGC record table stores the organisation name inside a
        # varchar(100) JSON blob, which leaves 24 characters for it. A longer
        # name fails collection onboarding later with a message that never
        # mentions length, so it is trimmed here instead.
        "name_max_length": 24,
        "payload": {
            "entity_type": "private",
            "org_sector": "technology",
            "website_link": "https://example.invalid",
            "address": "Automation script, generated organisation",
            "certificate_path": "/automation/cert.pdf",
            "pancard_path": "/automation/pan.pdf",
            "emp_id": "ADM001",
            "job_title": "Automation",
            # Blank generates one per run: the platform rejects a request
            # carrying a manager email another request already used.
            "manager_email": "",
            "organisation_documents": "/automation/org.pdf",
        },
    },
    "approval": {
        "enabled": True,
        "status": "granted",
        "list_page_size": 100,
        "list_max_pages": 20,
    },
    "create": {
        # Sign in as the new account with the password grant. The org create
        # request is submitted as the user, so this journey cannot run without
        # it; the key exists so the sign-in can be refused deliberately rather
        # than failing halfway.
        "fetch_token": True,
        "touch_control_plane": True,
        "expect_roles": ["org_admin", "provider"],
        # none: create the account and mail nothing — the Admin API sends no
        # verification mail on its own. send: Keycloak mails a "confirm your
        # address" link. actions: mail an action-token link for email_actions.
        "verify_email": "none",
        "email_actions": ["VERIFY_EMAIL"],
        "email_link_client_id": "",
        "email_link_redirect_uri": "",
        "email_link_lifespan_seconds": 0,
        "role_timeout_seconds": 60,
        "role_poll_seconds": 2,
        "confirm_membership": True,
        "settle_seconds": 0,
    },
    # The compute role is orthogonal to consumer/provider/org_admin: a request
    # the account makes for itself, approved by the COS admin, granting the
    # `compute` realm role on top of whatever the account already has.
    "compute": {
        "enabled": False,
        # Sent as {"additionalInfo": {...}} when non-empty; the body is optional.
        "additional_info": {},
        "kyc": {
            "set_verified": True,
            "attribute": "kyc_verified",
            "value": "true",
            "refresh_token": True,
        },
        "approve": True,
        "approve_status": "granted",
        # self: read the user's own compute requests. cos_admin: page the COS
        # admin's pending list instead.
        "lookup": "self",
        "list_page_size": 100,
        "list_max_pages": 20,
        "settle_seconds": 2,
        "expect_role": "compute",
    },
    "endpoints": {
        "kc_token": "/realms/{realm}/protocol/openid-connect/token",
        "kc_users": "/admin/realms/{realm}/users",
        "kc_user": "/admin/realms/{realm}/users/{user_id}",
        "kc_realm_role": "/admin/realms/{realm}/roles/{role}",
        "kc_role_mappings": "/admin/realms/{realm}/users/{user_id}/role-mappings/realm",
        "kc_send_verify_email": "/admin/realms/{realm}/users/{user_id}/send-verify-email",
        "kc_execute_actions_email":
            "/admin/realms/{realm}/users/{user_id}/execute-actions-email",
        "cp_user_info": "/iudx/v2/auth/user",
        "cp_compute_requests": "/iudx/v2/auth/compute/requests",
        "cp_compute_request": "/iudx/v2/auth/compute/requests/{req_id}",
        "cp_own_compute_requests": "/iudx/v2/auth/user/compute/requests",
        "cp_org_create_requests": "/iudx/v2/auth/organisations/requests",
        "cp_org_create_approve": "/iudx/v2/auth/organisations/requests/approve",
        "cp_org_users": "/iudx/v2/auth/organisations/{org_id}/users",
    },
    "output": {
        "enabled": True,
        # Written beside this script; the deletion script reads it across.
        "file": "org_admin_created.json",
        # Kept for symmetry with the other scripts: the deletion script uses the
        # Keycloak Admin API, which does not sign in as the account.
        "include_password": True,
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


# ------------------------------------------------------------------ identity


def generated_username(config: dict[str, Any]) -> str:
    """Build a username from `user.prefix` and `user.username_template`.

    The template decides the shape and the prefix names the run, so a set of
    accounts can be made recognisable — and swept — by whatever naming a
    deployment already uses.
    """
    user = config["user"]
    template = str(user.get("username_template") or "{prefix}-{timestamp}-{random}@{domain}")
    fields = {
        "prefix": str(user.get("prefix") or "user"),
        "timestamp": datetime.now(timezone.utc).strftime(
            str(user.get("timestamp_format") or "%Y%m%d%H%M%S")
        ),
        "random": secrets.token_hex(max(int(user.get("random_hex_bytes") or 2), 1)),
        "domain": str(user.get("email_domain") or "example.invalid"),
    }
    try:
        name = template.format(**fields)
    except KeyError as exc:
        raise ConfigError(
            f"'user.username_template' uses an unknown placeholder {exc}; "
            f"available: {', '.join(sorted(fields))}"
        ) from exc
    if not name.strip():
        raise ConfigError("'user.username_template' produced an empty username")
    return name.strip()


def resolve_identity(config: dict[str, Any], override: str | None) -> tuple[str, str]:
    """The username and email to use, generating one when none is configured.

    `--username` wins, then a literal `user.username`, then a name built from
    `user.prefix` and `user.username_template`.

    Username and email then end up the same string. Realms with
    registrationEmailAsUsername set overwrite the username with the email on
    create, so a distinct username would be silently discarded — the user could
    then not be read back, and the password grant would reject the login.
    """
    user = config["user"]
    domain = str(user.get("email_domain") or "example.invalid")
    username = (override or str(user.get("username") or "")).strip()
    if not username:
        username = generated_username(config)
    email = str(user.get("email") or "").strip()
    if not email:
        email = username if "@" in username else f"{username}@{domain}"
    return username, email


def user_payload(config: dict[str, Any], username: str, email: str) -> dict[str, Any]:
    """The Keycloak representation of the account to create."""
    user = config["user"]
    password = require_string(user.get("password"), "user.password")
    payload: dict[str, Any] = {
        "username": username,
        "email": email,
        "firstName": str(user.get("first_name") or "Automation"),
        "lastName": str(user.get("last_name") or "User"),
        "enabled": bool(user.get("enabled", True)),
        "emailVerified": bool(user.get("email_verified", True)),
        "credentials": [
            {
                "type": "password",
                "value": password,
                "temporary": bool(user.get("temporary_password", False)),
            }
        ],
    }
    actions = [str(a) for a in (user.get("required_actions") or [])]
    if actions:
        # Keycloak will demand these of the account at its next browser login.
        # VERIFY_EMAIL here is what makes an account genuinely unverified.
        payload["requiredActions"] = actions
    attributes = user.get("attributes") or {}
    if attributes:
        if not isinstance(attributes, dict):
            raise ConfigError("'user.attributes' must be an object")
        # Keycloak stores attribute values as lists of strings.
        payload["attributes"] = {
            key: value if isinstance(value, list) else [str(value)]
            for key, value in attributes.items()
        }
    return payload


def ensure_user(
    config: dict[str, Any], kc: Keycloak, username: str, email: str
) -> tuple[str, str, str]:
    """Create the account, or adopt one already there when the config allows it.

    Returns the id plus the username and email Keycloak actually stored, which
    are not always the ones asked for — the realm may lowercase them, or replace
    the username with the email (see Keycloak.create_user). Everything
    afterwards uses the stored values: the sign-in, the handoff file, the
    printed block, and the teardown.
    """
    user = config["user"]
    existing = kc.find_user(username)
    if not existing and email and email != username:
        existing = kc.find_by_email(email)
    if existing:
        if not user.get("reuse_existing", False):
            raise ApiError(
                f"user {existing.get('username') or username} already exists "
                f"({existing['id']}); set user.reuse_existing to true to adopt it, "
                "or choose another username or email"
            )
        stored = str(existing.get("username") or username)
        LOG.info("Reusing existing user %s (%s)", stored, existing["id"])
        return existing["id"], stored, str(existing.get("email") or email)

    record = kc.create_user(user_payload(config, username, email))
    stored = str(record.get("username") or username)
    if stored != username:
        LOG.warning(
            "Keycloak stored this account as %s, not the %s that was asked for — "
            "this realm has registrationEmailAsUsername enabled, so the email "
            "became the username. Continuing as %s.",
            stored, username, stored,
        )
    LOG.info("Created user %s (%s)", stored, record["id"])
    return record["id"], stored, str(record.get("email") or email)


def request_email_verification(
    config: dict[str, Any], kc: Keycloak, user_id: str, username: str, email: str
) -> bool:
    """Have Keycloak mail the account, per `create.verify_email`.

    Creating a user through the Admin API never sends anything by itself, so
    verification has to be asked for. Two routes do it:

        send     PUT .../send-verify-email — the plain "confirm your address"
                 mail, and the closest thing to what a real sign-up sends.
        actions  PUT .../execute-actions-email with `create.email_actions` —
                 the same action-token link, but for any set of actions, so it
                 can carry UPDATE_PASSWORD or CONFIGURE_TOTP alongside
                 VERIFY_EMAIL.

    Both need SMTP configured on the realm, and both mail whatever address the
    account carries — so point them at an inbox you own.
    """
    create = config["create"]
    mode = str(create.get("verify_email") or "none").lower()
    if mode == "none":
        return False
    if mode not in ("send", "actions"):
        raise ConfigError(
            f"unknown create.verify_email '{mode}'; expected none, send or actions"
        )

    if config["user"].get("email_verified", True):
        LOG.warning(
            "create.verify_email is '%s' but user.email_verified is true, so the "
            "account is already verified and the mail asks for nothing. Set "
            "user.email_verified to false to exercise the real path.",
            mode,
        )

    params: dict[str, Any] = {}
    if str(create.get("email_link_client_id") or ""):
        params["client_id"] = str(create["email_link_client_id"])
    if str(create.get("email_link_redirect_uri") or ""):
        params["redirect_uri"] = str(create["email_link_redirect_uri"])
    lifespan = int(create.get("email_link_lifespan_seconds") or 0)
    if lifespan > 0:
        params["lifespan"] = lifespan

    if mode == "send":
        kc.request(
            "PUT", kc.path("kc_send_verify_email", user_id=user_id),
            f"send verification email to {email}",
            params=params or None, expect=(200, 204),
        )
    else:
        actions = [str(a) for a in create.get("email_actions") or ["VERIFY_EMAIL"]]
        kc.request(
            "PUT", kc.path("kc_execute_actions_email", user_id=user_id),
            f"email account actions {', '.join(actions)} to {email}",
            json_body=actions, params=params or None, expect=(200, 204),
        )

    LOG.info("Keycloak has mailed %s — the account stays unverified until that "
             "link is opened", email)
    if create.get("fetch_token", True) and not config["user"].get("email_verified", True):
        LOG.warning(
            "Signing in as %s is expected to fail while the address is "
            "unverified: the password grant refuses an account that still has a "
            "required action pending. Set create.fetch_token and "
            "create.touch_control_plane to false to stop before that step.",
            username,
        )
    return True


def assign_extra_roles(config: dict[str, Any], kc: Keycloak, user_id: str) -> None:
    """Realm roles the config asks for on top of whatever the flow grants."""
    for role in config["user"].get("realm_roles") or []:
        kc.assign_realm_role(user_id, str(role))
        LOG.info("Assigned realm role %s", role)


def report_roles(config: dict[str, Any], kc: Keycloak, user_id: str) -> list[str]:
    """Assert the roles the config expects, or just log what the account has."""
    create = config["create"]
    expected = [str(role) for role in create.get("expect_roles") or []]
    if expected:
        seen = kc.await_roles(
            user_id,
            expected,
            float(create.get("role_timeout_seconds") or 60),
            float(create.get("role_poll_seconds") or 2),
        )
    else:
        seen = kc.realm_roles(user_id)
    LOG.info("Keycloak roles: %s", ", ".join(seen) or "none")
    return seen


def log_account(config: dict[str, Any], record: dict[str, Any]) -> None:
    """Print everything about the account that was just created.

    The password is included: it is the point of the block — the account is
    meant to be signed into by hand afterwards.
    """
    user = config["user"]
    fields: dict[str, Any] = dict(record)
    fields["password"] = user.get("password")
    fields["keycloak_realm"] = config["keycloak"].get("realm")
    fields["keycloak_url"] = config["keycloak"].get("url")
    fields["control_plane"] = config["control_plane"].get("base_url")
    if isinstance(fields.get("roles"), list):
        fields["roles"] = ", ".join(fields["roles"]) or "none"
    log_block("=== account created ===", fields)


def write_output(config: dict[str, Any], config_path: Path, record: dict[str, Any]) -> None:
    """Hand the created account over to the deletion script.

    The deletion config points at this file, so a teardown needs no ids typed
    out by hand. The password is left out unless the config asks for it.
    """
    output = config["output"]
    if not output.get("enabled", True):
        return
    payload = dict(record)
    if output.get("include_password", False):
        payload["password"] = config["user"].get("password") or ""
    write_json(resolve_path(str(output.get("file") or "created_user.json"), config_path), payload)


# -------------------------------------------------------------------- steps


def organisation_name(config: dict[str, Any], override: str | None = None) -> str:
    """The organisation to ask for, generated and trimmed when not configured."""
    org = config["organisation"]
    name = (override or str(org.get("name") or "")).strip()
    if not name:
        stamp = datetime.now(timezone.utc).strftime("%m%d%H%M%S")
        name = f"{org.get('name_prefix') or 'adm-org'}-{stamp}"
    limit = int(org.get("name_max_length") or 0)
    return name[:limit] if limit > 0 else name


def find_org_create_request(
    config: dict[str, Any], cp: ControlPlane, cos_token: str, name: str
) -> str:
    """Locate our pending request in the COS admin's list, by its name."""
    approval = config["approval"]
    size = int(approval.get("list_page_size") or 100)
    max_pages = int(approval.get("list_max_pages") or 20)
    for page in range(1, max_pages + 1):
        payload = cp.request(
            "GET", cp_path(config, "cp_org_create_requests"),
            "list pending org requests", token=cos_token,
            params={"status": "pending", "page": page, "size": size},
        )
        rows = rows_of(payload)
        for row in rows:
            if str(field(row, "name") or "") == name:
                request_id = str(field(row, "id", "requestId", "request_id") or "")
                if request_id:
                    return request_id
        if len(rows) < size:
            break
    raise ApiError(f"pending org request {name} is not visible to the COS admin")


# ------------------------------------------------------------------- compute


def set_kyc_verified(config: dict[str, Any], kc: Keycloak, user_id: str) -> bool:
    """Mark the account KYC-verified in Keycloak, per `compute.kyc`.

    The compute request sits behind the platform's KYC gate. That gate is off
    wherever `kycRequired` is false — dev is one such deployment, which is why
    the join, org-create and provider requests all go through on an unverified
    account — but a deployment with it on refuses the request, and a real
    account carries the attribute anyway.

    Keycloak replaces the whole attribute map on write, so the attributes the
    account already has are read and merged rather than overwritten.
    """
    kyc = config["compute"].get("kyc") or {}
    if not kyc.get("set_verified", True):
        return False
    name = str(kyc.get("attribute") or "kyc_verified")
    value = kyc.get("value", "true")
    record = kc.request(
        "GET", kc.path("kc_user", user_id=user_id), "read the account to update"
    )
    attributes = dict(record.get("attributes") or {})
    attributes[name] = value if isinstance(value, list) else [str(value)]
    # Keycloak validates the whole representation on write, and this realm's
    # user profile makes email required — a body carrying only `attributes` is
    # rejected with error-user-attribute-required. So the fields the profile
    # needs are carried back unchanged alongside the merged attribute map, and
    # the server-managed ones (access, createdTimestamp, userProfileMetadata …)
    # are left out.
    body = {
        key: record[key]
        for key in ("username", "email", "firstName", "lastName",
                    "enabled", "emailVerified", "requiredActions")
        if key in record
    }
    body["attributes"] = attributes
    kc.request(
        "PUT", kc.path("kc_user", user_id=user_id),
        f"set {name} on the account",
        json_body=body, expect=(200, 204),
    )
    LOG.info("Marked the account %s=%s in Keycloak", name, value)
    return True


def find_compute_request(
    config: dict[str, Any], cp: ControlPlane, user_token: str, cos_token: str, user_id: str
) -> str:
    """The id of the pending compute role request, from either listing."""
    compute = config["compute"]
    lookup = str(compute.get("lookup") or "self").lower()
    if lookup == "self":
        payload = cp.request(
            "GET", cp_path(config, "cp_own_compute_requests"),
            "read own compute requests", token=user_token,
        )
        for row in rows_of(payload):
            if str(field(row, "status") or "").lower() == "pending":
                request_id = str(field(row, "id") or "")
                if request_id:
                    return request_id
        raise ApiError("the user has no pending compute role request")

    size = int(compute.get("list_page_size") or 100)
    max_pages = int(compute.get("list_max_pages") or 20)
    for page in range(1, max_pages + 1):
        payload = cp.request(
            "GET", cp_path(config, "cp_compute_requests"),
            "list compute requests", token=cos_token,
            params={"status": "pending", "page": page, "size": size},
        )
        rows = rows_of(payload)
        for row in rows:
            if str(field(row, "user_id", "userId") or "") == user_id:
                request_id = str(field(row, "id") or "")
                if request_id:
                    return request_id
        if len(rows) < size:
            break
    raise ApiError(f"no pending compute role request for {user_id} is visible to the COS admin")


def add_compute_role(
    config: dict[str, Any], kc: Keycloak, cp: ControlPlane, user_token: str,
    username: str, password: str, user_id: str,
) -> dict[str, Any]:
    """Ask for the compute role and have the COS admin approve it.

    The compute role is orthogonal to consumer, provider and org_admin: it is a
    request the account makes for itself, approved by the COS admin — never the
    org admin — and granting it adds the `compute` realm role on top of whatever
    the account already has. So this runs as the last step of any of the three
    journeys.

    Note `compute_role.user_id` is UNIQUE: an account gets one request, ever. A
    rejected one is re-activated to pending rather than a second row appearing.
    """
    compute = config["compute"]
    result: dict[str, Any] = {"compute_request_id": None, "kyc_verified": False}

    if not user_token:
        # create.fetch_token was off; the request is made as the account.
        user_token = kc.user_token(username, password)

    if set_kyc_verified(config, kc, user_id):
        result["kyc_verified"] = True
        if (compute.get("kyc") or {}).get("refresh_token", True):
            # The attribute is a token claim; the existing token predates it.
            user_token = kc.user_token(username, password)

    body: dict[str, Any] = {}
    info = compute.get("additional_info")
    if info:
        body["additionalInfo"] = info
    cp.request(
        "POST", cp_path(config, "cp_compute_requests"),
        "request compute role", token=user_token, json_body=body,
    )
    LOG.info("Requested the compute role")

    if not compute.get("approve", True):
        LOG.info("compute.approve is false — leaving the request pending, so the "
                 "account does not have the compute role yet")
        return result

    cos_token = actor_token(kc, config["cos_admin"], "cos_admin")
    request_id = find_compute_request(config, cp, user_token, cos_token, user_id)
    result["compute_request_id"] = request_id
    LOG.info("Compute role request is %s", request_id)

    cp.request(
        "PUT", cp_path(config, "cp_compute_request", req_id=request_id),
        "approve compute role request", token=cos_token,
        json_body={"status": str(compute.get("approve_status") or "granted")},
    )
    LOG.info("Compute role request approved")

    # The grant just added a role; assert it alongside the ones already expected.
    expected = config["create"].get("expect_roles")
    role = str(compute.get("expect_role") or "compute")
    if expected and role not in expected:
        expected.append(role)

    settle = float(compute.get("settle_seconds") or 0)
    if settle > 0:
        time.sleep(settle)
    return result


def create_org_admin(
    config: dict[str, Any], kc: Keycloak, cp: ControlPlane, username: str, email: str, name: str
) -> dict[str, Any]:
    """Create the account, get its organisation approved, and confirm the roles."""
    create = config["create"]
    approval = config["approval"]
    password = require_string(config["user"].get("password"), "user.password")

    user_id, username, email = ensure_user(config, kc, username, email)
    assign_extra_roles(config, kc, user_id)
    request_email_verification(config, kc, user_id, username, email)

    user_token = kc.user_token(username, password)
    LOG.info("Signed in as %s", username)

    if create.get("touch_control_plane", True):
        cp.request("GET", cp_path(config, "cp_user_info"), "read own user info", token=user_token)

    payload = dict(config["organisation"].get("payload") or {})
    payload["name"] = name
    if not str(payload.get("manager_email") or "").strip():
        local, _, domain = email.partition("@")
        payload["manager_email"] = f"{local}-manager@{domain or 'example.invalid'}"

    cp.request(
        "POST", cp_path(config, "cp_org_create_requests"),
        "submit org create request", token=user_token, json_body=payload,
    )
    LOG.info("Submitted an organisation create request for %s", name)

    record: dict[str, Any] = {
        "kind": "org_admin",
        "username": username,
        "email": email,
        "user_id": user_id,
        "org_name": name,
        "org_request_id": None,
        "org_id": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    if not approval.get("enabled", True):
        LOG.info("approval.enabled is false — leaving the request pending, "
                 "so the account is not an org admin yet")
        return record

    cos_token = actor_token(kc, config["cos_admin"], "cos_admin")
    request_id = find_org_create_request(config, cp, cos_token, name)
    record["org_request_id"] = request_id
    LOG.info("COS admin sees request %s", request_id)

    cp.request(
        "POST", cp_path(config, "cp_org_create_approve"),
        "approve org create request", token=cos_token,
        json_body={"req_id": request_id, "status": str(approval.get("status") or "granted")},
    )
    LOG.info("Approved")

    if config["compute"].get("enabled", False):
        record.update(add_compute_role(config, kc, cp, user_token, username, password, user_id))

    record["roles"] = report_roles(config, kc, user_id)

    # The approval wrote the organisation attribute; the existing token predates it.
    user_token = kc.user_token(username, password)
    org_attr = kc.attributes(user_id).get("organisation_id")
    if not org_attr:
        raise ApiError(f"{username} has no organisation_id attribute after approval")
    record["org_id"] = org_attr[0] if isinstance(org_attr, list) else org_attr
    LOG.info("Organisation %s", record["org_id"])

    if create.get("confirm_membership", True):
        members = cp.request(
            "GET", cp_path(config, "cp_org_users", org_id=str(record["org_id"])),
            "confirm org membership", token=user_token,
        )
        LOG.info("Organisation has %d member(s)", len(rows_of(members)))

    settle = float(create.get("settle_seconds") or 0)
    if settle > 0:
        LOG.info("Waiting %.0fs for the platform to settle", settle)
        time.sleep(settle)

    return record


# ---------------------------------------------------------------------- run


def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(
        str(args.log_level or config["logging"].get("level") or "INFO").upper()
    )
    configure_output(config)

    if not config["create"].get("fetch_token", True):
        raise ConfigError(
            "an org admin is made by the user asking for an organisation, so "
            "this script must sign in as the account — create.fetch_token "
            "cannot be false here."
        )

    username, email = resolve_identity(config, args.username)
    name = organisation_name(config, args.org_name)

    if args.dry_run:
        LOG.info("Dry run — no calls will be made")
        LOG.info("Keycloak realm %s at %s",
                 config["keycloak"].get("realm"), config["keycloak"].get("url"))
        LOG.info("ControlPlane at %s", config["control_plane"].get("base_url"))
        LOG.info("Would create %s (email %s)", username, email)
        LOG.info("Would request organisation %s", name)
        if config["approval"].get("enabled", True):
            LOG.info("Would have the COS admin approve it, granting org_admin and provider")
        if config["compute"].get("enabled", False):
            LOG.info(
                "Would then request the compute role and %s",
                "have the COS admin approve it"
                if config["compute"].get("approve", True)
                else "leave the request pending",
            )
        if config["output"].get("enabled", True):
            LOG.info("Would write %s",
                     resolve_path(str(config["output"].get("file")), config_path))
        return 0

    require_requests()
    kc = Keycloak(config["keycloak"], config["endpoints"])
    cp = ControlPlane(config["control_plane"])

    record = create_org_admin(config, kc, cp, username, email, name)
    write_output(config, config_path, record)
    log_account(config, record)
    LOG.info("Created org admin %s (%s) of organisation %s",
             record["username"], record["user_id"], record.get("org_id") or "pending")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("org_admin_creation_config.json"),
        help="JSON config path (default: org_admin_creation_config.json beside this script)",
    )
    parser.add_argument("--username", help="override the configured or generated username")
    parser.add_argument("--org-name", help="override the configured or generated organisation name")
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
