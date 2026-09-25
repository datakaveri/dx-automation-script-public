#!/usr/bin/env python3
"""Submit N solutions to a published challenge, one per consumer account.

Usage:
    python challenge_submission.py challenge_submission_config.json
    python challenge_submission.py challenge_submission_config.json --count 5
    python challenge_submission.py challenge_submission_config.json --competition-id <uuid>
    python challenge_submission.py challenge_submission_config.json --title "My challenge"
    python challenge_submission.py challenge_submission_config.json --dry-run

The server allows one submission per user per challenge, so N submissions need
N accounts. `consumers.accounts` lists the ones to use, in order; when
`consumers.count` (or `--count`) asks for more than are listed, the rest are
created in Keycloak through the Admin API (`consumers.auto_create`), which
needs `keycloak.admin_client_id` / `admin_client_secret`. Nothing else is
needed for a fresh account: the community layer inserts the user from the
token on its first request, and the consumer routes require no realm role.

For each account the script signs in, joins the challenge
(`POST /challenge/users/challenges/{id}/join`), uploads the two files the
server insists on — a `.zip` of the solution and a `.pdf`/`.doc`/`.docx`
write-up — through `POST /challenge/attachment` (a presigned S3 PUT), and
creates the submission (`POST /challenge/{id}/submission`). The files come
from `submission.solution_zip_file` / `submission.document_file`, or are
generated on the fly when those are blank, so nothing has to be prepared.

The server accepts a submission only while the challenge is PUBLISHED and
today (Asia/Kolkata) lies within submission_starts_at..submission_ends_at,
both inclusive. Which challenge: `--competition-id`, `--title` (looked up in
the public published list), `target.*`, or the handoff file the creation
script wrote (`target.input_file`).

What was submitted — each account, its submission id, the uploaded keys, and
which accounts this run created — is written to the file named by
`output.file`, which the evaluation and cleanup scripts beside this one read.

Everything the script touches — URLs, credentials, endpoint paths, the body —
comes from the JSON config. Values may reference the environment as ${VAR} or
${VAR:-fallback}.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import random
import re
import secrets
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import requests
except ModuleNotFoundError:  # Allows --help and config validation before install.
    requests = None  # type: ignore[assignment]


LOG = logging.getLogger("challenge_submission")

# Every key the script reads, with the value used when the config omits it. The
# shipped example config repeats these, so any of them can be pasted over.
DEFAULT_CONFIG: dict[str, Any] = {
    "community": {
        # The community-layer root, including its mount path, e.g.
        # https://host/community — /challenge/... is appended to this.
        "base_url": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    "keycloak": {
        "url": "",
        "realm": "",
        # The public client the platform's own front end signs in with; the
        # community layer checks the token's audience and issuer, so it must
        # be a client that deployment accepts.
        "user_client_id": "",
        "user_client_secret": "",
        # A confidential client with realm user management — only needed when
        # consumers.auto_create has to make accounts.
        "admin_client_id": "",
        "admin_client_secret": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    # Which challenge to submit to. An id wins over a title; both win over the
    # handoff file the creation script wrote.
    "target": {
        "competition_id": "",
        "title": "",
        "input_file": "../creation/challenge_created.json",
        "require_input_file": False,
    },
    # How --title is looked up: the public list of challenges, paged whole.
    "lookup": {
        "status": "PUBLISHED",
        "page_size": 100,
        "max_pages": 20,
    },
    # Who submits. One submission per account: the first `count` of `accounts`
    # are used, and the shortfall is created when auto_create.enabled is true.
    "consumers": {
        "count": 1,
        # [{"username": "...", "password": "...", "token": ""}] — a token wins
        # over the password.
        "accounts": [],
        "auto_create": {
            "enabled": True,
            "password": "Consumer@Pass1",
            "first_name": "Consumer",
            "last_name": "Submitter",
            # Placeholders: {prefix}, {timestamp}, {index}, {random}, {domain}.
            "prefix": "e2e-dev-submitter",
            "email_domain": "example.invalid",
            "username_template": "{prefix}-{timestamp}-{index}-{random}@{domain}",
            "timestamp_format": "%Y%m%d%H%M%S",
            "random_hex_bytes": 2,
            "enabled_account": True,
            "email_verified": True,
            "temporary_password": False,
            "attributes": {},
            # Wait this long after creating an account before signing in as it.
            "settle_seconds": 0,
        },
    },
    "join": {
        "enabled": True,
        # 409 "already joined" is fine — the account can still submit.
        "already_joined_ok": True,
        "expect_http_status": [200, 201],
    },
    "submission": {
        # Placeholders: {index}, {username}, {challenge}, {timestamp}, {random}.
        "title_template": "Solution {index} by {username}",
        "description_template": "Automated submission {index} for '{challenge}' "
                                "from dx-automation-script; safe to disqualify.",
        # Local files to upload for every account. Blank means generate one:
        # the zip holds a predictions.csv plus a README, the document is a
        # one-page PDF. Paths are relative to the config file.
        "solution_zip_file": "",
        "document_file": "",
        "generate": {
            "zip_name_template": "solution-{index}.zip",
            "document_name_template": "writeup-{index}.pdf",
            # Rows of random predictions in the generated CSV.
            "csv_rows": 20,
            # Where generated files are written; blank uses a temp directory.
            "directory": "",
        },
        "content_type": "application/octet-stream",
        "upload_timeout_seconds": 120,
        # What to do when the account has already submitted (server 409):
        # "fail" stops the run, "skip" records it and moves on.
        "on_existing": "fail",
        "expect_http_status": [200, 201],
        # Read the account's submissions back and confirm the new id is there.
        "verify_after": True,
        # Pause between accounts.
        "pause_seconds": 0,
    },
    "endpoints": {
        "kc_token": "/realms/{realm}/protocol/openid-connect/token",
        "kc_users": "/admin/realms/{realm}/users",
        "kc_user": "/admin/realms/{realm}/users/{user_id}",
        "public_list": "/challenge/users/challenges",
        "join": "/challenge/users/challenges/{competition_id}/join",
        "attachment": "/challenge/attachment",
        "submit": "/challenge/{competition_id}/submission",
        "my_submissions": "/challenge/users/challenges/{competition_id}/submissions",
    },
    "time_format": {
        "timezone": "Asia/Kolkata",
    },
    "output": {
        "enabled": True,
        # Written beside this script; the evaluation and cleanup scripts read it.
        "file": "submissions_created.json",
        # The cleanup script signs in as nobody, but a later manual check might.
        "include_password": True,
    },
    "logging": {
        "level": "INFO",
        "print_requests": True,
        "print_responses": True,
        "response_preview_chars": 2000,
        # Off means bodies print verbatim — passwords and access tokens
        # included. Turn it on before sharing a log.
        "mask_secrets": False,
    },
}


class ConfigError(ValueError):
    """A required configuration value is absent or invalid."""


class ApiError(RuntimeError):
    """Keycloak or the community layer rejected a request."""


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
            "'python -m pip install -r ../requirements.txt'"
        )


def api_path(config: dict[str, Any], name: str, **fields: str) -> str:
    """One endpoint path from the config, with its placeholders filled in."""
    template = config["endpoints"].get(name)
    if not isinstance(template, str) or not template:
        raise ConfigError(f"'endpoints.{name}' must be a non-empty string")
    try:
        return template.format(**fields)
    except KeyError as exc:
        raise ConfigError(f"'endpoints.{name}' uses an unknown placeholder {exc}") from exc


def zone(config: dict[str, Any]) -> ZoneInfo:
    name = str(config["time_format"].get("timezone") or "Asia/Kolkata")
    try:
        return ZoneInfo(name)
    except Exception as exc:  # unknown zone, or tzdata not installed
        raise ConfigError(f"'time_format.timezone' {name!r} is not a known timezone: {exc}") from exc


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
    """Print a labelled block of details, one per line."""
    shown = scrub(dict(fields))
    LOG.info("%s", title)
    width = max((len(str(k)) for k in shown), default=0)
    for key, value in shown.items():
        LOG.info("    %-*s  %s", width, key, "" if value is None else value)


# ------------------------------------------------------------------- keycloak


class Keycloak:
    """Password-grant tokens for platform accounts, plus the Admin API for
    creating the accounts consumers.auto_create asks for."""

    def __init__(self, config: dict[str, Any], endpoints: dict[str, Any]):
        self.base_url = require_string(config.get("url"), "keycloak.url").rstrip("/")
        self.realm = require_string(config.get("realm"), "keycloak.realm")
        self.user_client_id = require_string(
            config.get("user_client_id"), "keycloak.user_client_id"
        )
        self.user_client_secret = str(config.get("user_client_secret") or "")
        self.admin_client_id = str(config.get("admin_client_id") or "")
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

    def user_token(self, username: str, password: str) -> str:
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

    def admin_token(self) -> str:
        """Client-credentials token for the Admin API, refreshed when stale."""
        if self._admin_token and time.monotonic() < self._admin_expiry:
            return self._admin_token
        if not self.admin_client_id:
            raise ConfigError(
                "'keycloak.admin_client_id' is needed to create accounts — set it, "
                "list enough consumers.accounts, or turn consumers.auto_create off"
            )
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
            method, self.url(path), json=json_body, params=params,
            headers=headers, timeout=self.timeout,
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

    def find_user(self, username: str) -> dict[str, Any] | None:
        matches = self.request(
            "GET", self.path("kc_users"), f"find user {username}",
            params={"username": username, "exact": "true", "max": 2},
        )
        return matches[0] if matches else None

    def find_by_email(self, email: str) -> dict[str, Any] | None:
        matches = self.request(
            "GET", self.path("kc_users"), f"find user by email {email}",
            params={"email": email, "exact": "true", "max": 2},
        )
        return matches[0] if matches else None

    def create_user(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Create the account and return it as Keycloak actually stored it.

        A realm with registrationEmailAsUsername replaces the username with the
        email on create, so the account is read back by both — the caller needs
        the stored username, because that is what the password grant accepts.
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


# ------------------------------------------------------------ community layer


class Community:
    """The community-layer HTTP API, one bearer token per call.

    Every response is the same envelope — success, status_code, message, data,
    error — so the body is handed back parsed and the envelope is checked here.
    """

    def __init__(self, config: dict[str, Any]):
        self.base_url = require_string(config.get("base_url"), "community.base_url").rstrip("/")
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
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        log_request(label, method, path, body=json_body, params=params)
        response = self.session.request(
            method,
            f"{self.base_url}/{path.lstrip('/')}",
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
                f"expected {' or '.join(str(code) for code in expect)}\n"
                f"{describe_error(payload)}",
            )
        if isinstance(payload, dict) and payload.get("success") is False:
            raise ApiError(f"{label}: the server reported failure — {describe_error(payload)}")
        return payload


def describe_error(payload: Any) -> str:
    """The useful part of an error envelope, whichever shape it took."""
    if not isinstance(payload, dict):
        return str(payload)[:400]
    parts = []
    if payload.get("message"):
        parts.append(str(payload["message"]))
    error = payload.get("error")
    if isinstance(error, dict):
        parts.append(json.dumps(error, default=str)[:600])
    elif error:
        parts.append(str(error)[:600])
    # FastAPI's own 422 shape.
    if payload.get("detail"):
        parts.append(json.dumps(payload["detail"], default=str)[:600])
    return " | ".join(parts) or json.dumps(payload, default=str)[:400]


def status_of(exc: ApiError) -> int:
    """The HTTP status an ApiError carries in its message, or 0."""
    match = re.search(r"returned (\d{3})", str(exc))
    return int(match.group(1)) if match else 0


def data_of(payload: Any) -> dict[str, Any]:
    """The `data` object of a response envelope, or the payload itself."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload
    return {}


def rows_of(payload: Any) -> list[Any]:
    """The list of records in a list response, whichever key it sits under."""
    data = data_of(payload)
    for key in ("competitions", "challenges", "submissions", "results", "items"):
        if isinstance(data.get(key), list):
            return data[key]
    return payload if isinstance(payload, list) else []


# -------------------------------------------------------------------- files


def resolve_path(name: str, config_path: Path) -> Path:
    """A configured file path, read relative to the config file that named it."""
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


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    LOG.info("Wrote %s", path)


def write_output(config: dict[str, Any], config_path: Path, record: dict[str, Any]) -> None:
    output = config["output"]
    if not output.get("enabled", True):
        return
    write_json(resolve_path(str(output.get("file") or "submissions_created.json"), config_path), record)


# -------------------------------------------------------------------- target


def find_by_title(config: dict[str, Any], api: Community, token: str, title: str) -> str:
    """Page the public challenge list for an exact title and return its id."""
    lookup = config["lookup"]
    page_size = int(lookup.get("page_size") or 100)
    max_pages = int(lookup.get("max_pages") or 20)
    for page in range(1, max_pages + 1):
        params: dict[str, Any] = {"page": page, "limit": page_size}
        status = str(lookup.get("status") or "").strip()
        if status:
            params["status"] = status.upper()
        payload = api.request(
            "GET", api_path(config, "public_list"),
            f"list challenges (page {page})", token, params=params,
        )
        rows = rows_of(payload)
        for row in rows:
            if isinstance(row, dict) and str(row.get("title") or "").strip() == title:
                return str(row.get("id") or row.get("competition_id") or "")
        if len(rows) < page_size:
            break
    raise ConfigError(f"no {lookup.get('status') or ''} challenge titled {title!r} in the public list")


def resolve_target(
    config: dict[str, Any], config_path: Path, args: argparse.Namespace
) -> tuple[str, str]:
    """Which challenge: (competition_id, title) — the id may be blank when only
    a title is known, in which case it is looked up once signed in."""
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

    asked_id = (args.competition_id or str(target.get("competition_id") or "")).strip()
    asked_title = (args.title or str(target.get("title") or "")).strip()
    recorded_id = str(record.get("competition_id") or "").strip()
    recorded_title = str(record.get("title") or "").strip()

    if record and (
        (asked_id and recorded_id and asked_id != recorded_id)
        or (asked_title and recorded_title and asked_title != recorded_title and not asked_id)
    ):
        LOG.warning(
            "%s describes %s (%s), which is not the challenge asked for — using only "
            "the config and command line", input_file, recorded_title or "?", recorded_id or "?",
        )
        record = {}

    competition_id = asked_id or recorded_id
    title = asked_title or recorded_title
    if not competition_id and not title:
        raise ConfigError(
            "no challenge to submit to: pass --competition-id or --title, set "
            "'target.competition_id' or 'target.title', or point 'target.input_file' "
            "at the file the creation script wrote"
        )
    if record and str(record.get("status") or "").upper() not in ("", "PUBLISHED"):
        LOG.warning(
            "%s says the challenge is %s; the server accepts submissions only while "
            "it is PUBLISHED", input_file, record.get("status"),
        )
    return competition_id, title


# ----------------------------------------------------------------- accounts


def planned_accounts(config: dict[str, Any], count: int) -> list[dict[str, Any]]:
    """The accounts to submit with: the configured ones first, then the ones to
    create. Each entry is {index, username, password, token, create}."""
    consumers = config["consumers"]
    accounts = consumers.get("accounts") or []
    if not isinstance(accounts, list):
        raise ConfigError("'consumers.accounts' must be a list")
    plan: list[dict[str, Any]] = []
    for index, entry in enumerate(accounts[:count], start=1):
        if not isinstance(entry, dict):
            raise ConfigError(f"'consumers.accounts[{index - 1}]' must be an object")
        username = str(entry.get("username") or "").strip()
        token = str(entry.get("token") or "").strip()
        if not username and not token:
            raise ConfigError(f"'consumers.accounts[{index - 1}]' needs a username or a token")
        plan.append({
            "index": index,
            "username": username or f"(token #{index})",
            "password": str(entry.get("password") or ""),
            "token": token,
            "create": False,
        })
    shortfall = count - len(plan)
    if shortfall > 0:
        auto = consumers.get("auto_create") or {}
        if not auto.get("enabled", True):
            raise ConfigError(
                f"{count} submissions asked for but only {len(plan)} consumers.accounts "
                "listed, and consumers.auto_create is off"
            )
        require_string(auto.get("password"), "consumers.auto_create.password")
        for index in range(len(plan) + 1, count + 1):
            plan.append({
                "index": index,
                "username": "",  # decided at creation time
                "password": str(auto.get("password")),
                "token": "",
                "create": True,
            })
    return plan


def generated_username(config: dict[str, Any], index: int) -> tuple[str, str]:
    """(username, email) for an account to create, from the template."""
    auto = config["consumers"]["auto_create"]
    template = str(auto.get("username_template") or "{prefix}-{timestamp}-{index}-{random}@{domain}")
    fields = {
        "prefix": str(auto.get("prefix") or "e2e-submitter"),
        "timestamp": datetime.now(zone(config)).strftime(
            str(auto.get("timestamp_format") or "%Y%m%d%H%M%S")
        ),
        "index": index,
        "random": secrets.token_hex(max(int(auto.get("random_hex_bytes") or 2), 1)),
        "domain": str(auto.get("email_domain") or "example.invalid"),
    }
    try:
        username = template.format(**fields).strip().lower()
    except KeyError as exc:
        raise ConfigError(
            f"'consumers.auto_create.username_template' uses an unknown placeholder {exc}"
        ) from exc
    email = username if "@" in username else f"{username}@{fields['domain']}"
    return username, email


def user_payload(config: dict[str, Any], username: str, email: str) -> dict[str, Any]:
    """The Keycloak representation of the account to create."""
    auto = config["consumers"]["auto_create"]
    payload: dict[str, Any] = {
        "username": username,
        "email": email,
        "firstName": str(auto.get("first_name") or "Consumer"),
        "lastName": str(auto.get("last_name") or "Submitter"),
        "enabled": bool(auto.get("enabled_account", True)),
        "emailVerified": bool(auto.get("email_verified", True)),
        "credentials": [
            {
                "type": "password",
                "value": str(auto.get("password")),
                "temporary": bool(auto.get("temporary_password", False)),
            }
        ],
    }
    attributes = auto.get("attributes") or {}
    if attributes:
        if not isinstance(attributes, dict):
            raise ConfigError("'consumers.auto_create.attributes' must be an object")
        # Keycloak stores attribute values as lists of strings.
        payload["attributes"] = {
            key: value if isinstance(value, list) else [str(value)]
            for key, value in attributes.items()
        }
    return payload


def create_account(config: dict[str, Any], kc: Keycloak, account: dict[str, Any]) -> None:
    """Create the account in Keycloak and fill username / id into the plan entry."""
    username, email = generated_username(config, account["index"])
    record = kc.create_user(user_payload(config, username, email))
    stored = str(record.get("username") or username)
    if stored != username:
        LOG.warning(
            "Keycloak stored this account as %s, not %s — this realm has "
            "registrationEmailAsUsername enabled. Continuing as %s.", stored, username, stored,
        )
    account["username"] = stored
    account["email"] = str(record.get("email") or email)
    account["keycloak_user_id"] = str(record.get("id") or "")
    LOG.info("Created consumer %s (%s)", stored, account["keycloak_user_id"])
    settle = float(config["consumers"]["auto_create"].get("settle_seconds") or 0)
    if settle > 0:
        time.sleep(settle)


def account_token(kc: Keycloak, account: dict[str, Any]) -> str:
    if account.get("token"):
        LOG.info("Using the configured token for consumer #%s", account["index"])
        return account["token"]
    LOG.info("Signing in as consumer #%s %s", account["index"], account["username"])
    return kc.user_token(account["username"], require_string(
        account.get("password"), f"consumers.accounts[{account['index'] - 1}].password"
    ))


# --------------------------------------------------------------- solutions


def fill(template: str, name: str, **fields: Any) -> str:
    try:
        return template.format(**fields)
    except KeyError as exc:
        raise ConfigError(f"'{name}' uses an unknown placeholder {exc}") from exc


def make_zip(path: Path, index: int, username: str, rows: int) -> None:
    """A solution archive: predictions.csv with random values, plus a README."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "prediction"])
    for row in range(1, max(rows, 1) + 1):
        writer.writerow([row, round(random.random(), 4)])
    readme = (
        f"Automated solution #{index} submitted by {username}\n"
        f"Generated by dx-automation-script at {datetime.now().isoformat()}\n"
        "This archive exists to exercise the challenge submission flow.\n"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("predictions.csv", buffer.getvalue())
        archive.writestr("README.txt", readme)


def make_pdf(path: Path, lines: list[str]) -> None:
    """A one-page PDF with the given lines, built by hand — no library needed."""
    def escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for line in lines:
        content.append(f"({escape(line)}) Tj T*")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


def solution_files(
    config: dict[str, Any], config_path: Path, account: dict[str, Any], challenge_title: str,
    work_dir: Path,
) -> tuple[Path, Path]:
    """(zip, document) for this account: the configured files, or generated ones."""
    submission = config["submission"]
    generate = submission.get("generate") or {}
    index = account["index"]
    username = account["username"]

    zip_name = str(submission.get("solution_zip_file") or "").strip()
    if zip_name:
        zip_path = resolve_path(zip_name, config_path)
        if not zip_path.is_file():
            raise ConfigError(f"submission.solution_zip_file not found: {zip_path}")
    else:
        zip_path = work_dir / fill(
            str(generate.get("zip_name_template") or "solution-{index}.zip"),
            "submission.generate.zip_name_template", index=index, username=username,
        )
        make_zip(zip_path, index, username, int(generate.get("csv_rows") or 20))
        LOG.info("Generated %s", zip_path)

    doc_name = str(submission.get("document_file") or "").strip()
    if doc_name:
        doc_path = resolve_path(doc_name, config_path)
        if not doc_path.is_file():
            raise ConfigError(f"submission.document_file not found: {doc_path}")
    else:
        doc_path = work_dir / fill(
            str(generate.get("document_name_template") or "writeup-{index}.pdf"),
            "submission.generate.document_name_template", index=index, username=username,
        )
        make_pdf(doc_path, [
            f"Solution write-up #{index}",
            f"Challenge: {challenge_title or '(untitled)'}",
            f"Submitted by: {username}",
            f"Generated: {datetime.now(zone(config)).isoformat()}",
            "",
            "This document was produced by dx-automation-script to exercise",
            "the challenge submission and evaluation flow.",
        ])
        LOG.info("Generated %s", doc_path)

    if zip_path.suffix.lower() != ".zip":
        raise ConfigError(f"the solution file must be a .zip, not {zip_path.name}")
    if doc_path.suffix.lower() not in (".pdf", ".doc", ".docx"):
        raise ConfigError(f"the document must be .pdf/.doc/.docx, not {doc_path.name}")
    return zip_path, doc_path


def upload_file(
    config: dict[str, Any], api: Community, token: str, local: Path, batch_id: str
) -> str:
    """Put one local file into the challenge bucket and return its S3 key.

    POST /challenge/attachment answers with a presigned PUT URL for a temp key;
    the bytes go straight to S3; the key then goes into the submission body,
    and the server copies it into place under the submission.
    """
    submission = config["submission"]
    payload = api.request(
        "POST", api_path(config, "attachment"), f"presign upload of {local.name}", token,
        json_body={"batch_id": batch_id, "file_name": local.name, "md_attachment": False},
    )
    data = data_of(payload)
    meta = payload.get("meta") if isinstance(payload, dict) and isinstance(payload.get("meta"), dict) else {}
    url = str(data.get("presigned_url") or "")
    key = str(meta.get("object_key") or data.get("object_key") or "")
    if not url or not key:
        raise ApiError(f"presign upload of {local.name}: no presigned_url/object_key in {preview(payload)}")
    content_type = str(submission.get("content_type") or "application/octet-stream")
    log_request(f"upload {local.name}", "PUT", url.split("?")[0], body=f"<{local.stat().st_size} bytes>")
    response = requests.put(
        url, data=local.read_bytes(), headers={"Content-Type": content_type},
        timeout=float(submission.get("upload_timeout_seconds") or 120), verify=api.session.verify,
    )
    log_response(f"upload {local.name}", "PUT", url.split("?")[0], response.status_code, response.text[:200])
    if response.status_code not in (200, 201, 204):
        raise ApiError(f"upload of {local.name} failed ({response.status_code}): {response.text[:400]}")
    LOG.info("Uploaded %s as %s", local, key)
    return key


# ---------------------------------------------------------------------- run


def join_challenge(config: dict[str, Any], api: Community, token: str,
                   competition_id: str, account: dict[str, Any]) -> str:
    """Join as this account; returns 'joined', 'already' or 'skipped'."""
    join = config["join"]
    if not join.get("enabled", True):
        return "skipped"
    expect = tuple(int(code) for code in join.get("expect_http_status") or (200, 201))
    label = f"consumer #{account['index']} joins"
    try:
        api.request("POST", api_path(config, "join", competition_id=competition_id),
                    label, token, expect=expect)
    except ApiError as exc:
        if status_of(exc) == 409 and join.get("already_joined_ok", True):
            LOG.info("%s: already a participant — continuing", label)
            return "already"
        raise
    return "joined"


def submit_solution(
    config: dict[str, Any], api: Community, token: str, competition_id: str,
    account: dict[str, Any], body: dict[str, Any],
) -> tuple[str, str]:
    """POST the submission; returns (submission_id, 'created' | 'existing')."""
    submission = config["submission"]
    expect = tuple(int(code) for code in submission.get("expect_http_status") or (200, 201))
    label = f"consumer #{account['index']} submits"
    try:
        payload = api.request(
            "POST", api_path(config, "submit", competition_id=competition_id),
            label, token, json_body=body, expect=expect,
        )
    except ApiError as exc:
        on_existing = str(submission.get("on_existing") or "fail").lower()
        if status_of(exc) == 409 and on_existing == "skip":
            LOG.warning("%s: this account has already submitted — skipping it", label)
            return "", "existing"
        raise
    data = data_of(payload)
    submission_id = str(data.get("id") or data.get("submission_id") or "").strip()
    if not submission_id:
        raise ApiError(f"{label}: no submission id in the response — {preview(payload)}")
    return submission_id, "created"


def verify_submission(config: dict[str, Any], api: Community, token: str,
                      competition_id: str, account: dict[str, Any], submission_id: str) -> None:
    payload = api.request(
        "GET", api_path(config, "my_submissions", competition_id=competition_id),
        f"consumer #{account['index']} reads submissions back", token,
        params={"page": 1, "limit": 100},
    )
    ids = {str(row.get("submission_id") or row.get("id") or "") for row in rows_of(payload)
           if isinstance(row, dict)}
    if submission_id not in ids:
        raise ApiError(f"submission {submission_id} is not in the account's submission list")
    LOG.info("Verified submission %s is listed for consumer #%s", submission_id, account["index"])


def submit_all(
    config: dict[str, Any], config_path: Path, plan: list[dict[str, Any]],
    competition_id: str, title: str, work_dir: Path,
) -> list[dict[str, Any]]:
    kc = Keycloak(config["keycloak"], config["endpoints"])
    api = Community(config["community"])
    submission = config["submission"]
    entries: list[dict[str, Any]] = []
    pause = float(submission.get("pause_seconds") or 0)

    for account in plan:
        if account["create"]:
            create_account(config, kc, account)
        token = account_token(kc, account)
        if not competition_id:
            competition_id = find_by_title(config, api, token, title)
            LOG.info("Challenge %r is %s", title, competition_id)

        entry: dict[str, Any] = {
            "index": account["index"],
            "username": account["username"],
            "created_user": bool(account["create"]),
            "keycloak_user_id": account.get("keycloak_user_id"),
            "email": account.get("email"),
        }
        if config["output"].get("include_password", True) and account.get("password"):
            entry["password"] = account["password"]

        entry["join"] = join_challenge(config, api, token, competition_id, account)

        zip_path, doc_path = solution_files(config, config_path, account, title, work_dir)
        batch_id = str(uuid.uuid4())
        keys = [upload_file(config, api, token, zip_path, batch_id),
                upload_file(config, api, token, doc_path, batch_id)]

        fields = {
            "index": account["index"], "username": account["username"],
            "challenge": title or competition_id,
            "timestamp": datetime.now(zone(config)).strftime("%Y%m%d%H%M%S"),
            "random": secrets.token_hex(2),
        }
        body = {
            "title": fill(str(submission.get("title_template") or "Solution {index}"),
                          "submission.title_template", **fields),
            "description": fill(str(submission.get("description_template") or "Automated submission."),
                                "submission.description_template", **fields),
            "attachments": keys,
        }
        submission_id, outcome = submit_solution(config, api, token, competition_id, account, body)
        entry.update({
            "submission_id": submission_id or None,
            "submission_title": body["title"],
            "attachments": keys,
            "files": [zip_path.name, doc_path.name],
            "outcome": outcome,
        })
        if submission_id and submission.get("verify_after", True):
            verify_submission(config, api, token, competition_id, account, submission_id)
        entries.append(entry)
        log_block(f"=== consumer #{account['index']} ===", entry)
        if pause > 0 and account is not plan[-1]:
            time.sleep(pause)
    # The id may have been resolved from the title on the first account.
    for entry in entries:
        entry["competition_id"] = competition_id
    return entries


def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(
        str(args.log_level or config["logging"].get("level") or "INFO").upper()
    )
    configure_output(config)

    count = int(args.count if args.count is not None else config["consumers"].get("count") or 1)
    if count < 1:
        raise ConfigError("'consumers.count' must be at least 1")
    competition_id, title = resolve_target(config, config_path, args)
    plan = planned_accounts(config, count)
    on_existing = str(config["submission"].get("on_existing") or "fail").lower()
    if on_existing not in ("fail", "skip"):
        raise ConfigError("'submission.on_existing' must be \"fail\" or \"skip\"")

    generate = config["submission"].get("generate") or {}
    configured_dir = str(generate.get("directory") or "").strip()
    work_dir = resolve_path(configured_dir, config_path) if configured_dir else Path(
        tempfile.mkdtemp(prefix="challenge-submission-")
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    log_block("=== submissions to make ===", {
        "challenge": competition_id or f"(by title) {title}",
        "title": title or None,
        "count": count,
        "configured accounts": sum(1 for a in plan if not a["create"]),
        "accounts to create": sum(1 for a in plan if a["create"]),
        "solution zip": config["submission"].get("solution_zip_file") or "(generated)",
        "document": config["submission"].get("document_file") or "(generated)",
        "work dir": str(work_dir),
        "community": config["community"].get("base_url"),
    })

    if args.dry_run:
        LOG.info("Dry run — no calls will be made")
        for account in plan:
            who = "a new Keycloak account" if account["create"] else account["username"]
            LOG.info("Would sign in as %s, POST %s, upload 2 files via %s and POST %s",
                     who,
                     api_path(config, "join", competition_id=competition_id or "<id>"),
                     api_path(config, "attachment"),
                     api_path(config, "submit", competition_id=competition_id or "<id>"))
        if config["output"].get("enabled", True):
            LOG.info("Would write %s", resolve_path(str(config["output"].get("file")), config_path))
        return 0

    require_requests()
    entries = submit_all(config, config_path, plan, competition_id, title, work_dir)

    record = {
        "kind": "challenge_submissions",
        "competition_id": entries[0]["competition_id"] if entries else competition_id,
        "title": title,
        "community_base_url": config["community"].get("base_url"),
        "keycloak_realm": config["keycloak"].get("realm"),
        "created_at": datetime.now(zone(config)).isoformat(),
        "submissions": entries,
    }
    write_output(config, config_path, record)
    created = [e for e in entries if e["outcome"] == "created"]
    log_block("=== summary ===", {
        "challenge": record["competition_id"],
        "submitted": len(created),
        "skipped (already submitted)": len(entries) - len(created),
        "accounts created": sum(1 for e in entries if e["created_user"]),
        "submission ids": ", ".join(e["submission_id"] for e in created) or "none",
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
        default=Path(__file__).with_name("challenge_submission_config.json"),
        help="JSON config path (default: challenge_submission_config.json beside this script)",
    )
    parser.add_argument("--count", type=int, help="how many submissions to make (overrides consumers.count)")
    parser.add_argument("--competition-id", help="the challenge to submit to (overrides target.*)")
    parser.add_argument("--title", help="the challenge's exact title, looked up in the public list")
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
