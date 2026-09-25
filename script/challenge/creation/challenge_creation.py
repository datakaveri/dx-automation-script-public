#!/usr/bin/env python3
"""Create one challenge (competition) in dx-community-layer as the COS admin.

Usage:
    python challenge_creation.py challenge_creation_config.json
    python challenge_creation.py challenge_creation_config.json --title "My challenge"
    python challenge_creation.py challenge_creation_config.json --publish
    python challenge_creation.py challenge_creation_config.json --dry-run

The challenge is created through `POST /challenge/admin/challenge`, which only a
token carrying the `cos_admin` realm role may call. Every field of that body is
a config key under `challenge`, and the three times live together under
`times`: `creation` (when the challenge goes live), `submission_start`,
`submission_end` and `evaluation_end`. Each is an expression — `today`, an
absolute `2026-10-01`, `today+1d`, `submission_start+14d`, `now+2h` — so
"today" for all of them is fine, and so is any date you care to write.

It is created as a **draft** unless `challenge.draft` is false or `--publish` is
passed. That is deliberate: a published challenge can be neither updated nor
deleted through the API, so the submission-time, evaluation-time and deletion
scripts beside this one only work on a draft (or scheduled) challenge.

What was created — id, title, status, the timeline as sent — is written to the
handoff file named by `output.file`, which the other scripts read.

Everything the script touches — URLs, credentials, endpoint paths, the body —
comes from the JSON config. Values may reference the environment as ${VAR} or
${VAR:-fallback}.
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
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import requests
except ModuleNotFoundError:  # Allows --help and config validation before install.
    requests = None  # type: ignore[assignment]


LOG = logging.getLogger("challenge_creation")

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
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    # Who creates the challenge. A ready-made token wins over credentials.
    "cos_admin": {
        "username": "",
        "password": "",
        "token": "",
    },
    # The request body of POST /challenge/admin/challenge, key for key. null
    # fields are left out of the request (see create.omit_null_fields) so the
    # server's own defaults apply.
    "challenge": {
        "draft": True,
        # Blank builds a title from title_prefix + title_template below.
        "title": "",
        "title_prefix": "e2e-challenge",
        # Placeholders: {prefix}, {timestamp}, {random}. Titles are unique on
        # the server, so a repeatable run needs something that varies.
        "title_template": "{prefix}-{timestamp}-{random}",
        "timestamp_format": "%Y%m%d%H%M%S",
        "random_hex_bytes": 2,
        "subtitle": "Automation-created challenge",
        "overview": "Created by dx-automation-script; safe to delete.",
        "description": "This challenge was created by an automation script to exercise "
                       "the community layer's challenge APIs.",
        # S3 key of an already-uploaded image (POST /challenge/attachment).
        "image_url": None,
        "constraints": "None.",
        "evaluation_criteria_definition": "Highest score wins.",
        "submission_file_definition": "A single CSV file.",
        "other_resources": None,
        # CASH needs currency (3 letters) and total_pool_amount; NO_CASH
        # ignores both. The description is free text shown to participants.
        "prize_type": "CASH",
        "total_pool_amount": 10000,
        "currency": "INR",
        "prize_pool_description": "INR 10,000 total: 5,000 / 3,000 / 2,000 for the top three submissions.",
        # S3 key of an already-uploaded rules file — or leave it null and name
        # a local file in uploads.rules_and_guidelines_file below.
        "rules_and_guidelines": None,
        "dataset_description": "No dataset.",
        # [{"id": "<uuid>", "name": "..."}] — catalogue items. Publishing
        # requires data_models to be a list (empty is accepted on create).
        "data_models": [],
        "ai_models": None,
        # [{"object_key": "<s3 key>", "description": "..."}]
        "additional_assets": None,
        # Anything else to put in the body verbatim, for fields added after
        # this script was written.
        "extra_fields": {},
    },
    # Local files to upload before creating, through POST /challenge/attachment
    # (a presigned S3 PUT). The keys the server hands back go into the body —
    # rules_and_guidelines, image_url, additional_assets — in place of what
    # the `challenge` block says. Paths are relative to the config file.
    # rules_and_guidelines is required by the server when draft is false.
    "uploads": {
        "enabled": True,
        "rules_and_guidelines_file": "sample_rules_and_guidelines.md",
        "image_file": "",
        # [{"file": "path", "description": "..."}]
        "additional_asset_files": [],
        "content_type": "application/octet-stream",
        "timeout_seconds": 120,
    },
    # The three times of the challenge, each an expression:
    #   [base][offset...][ HH:MM]
    # base:    today/now (or blank), tomorrow, yesterday, an absolute date in
    #          one of time_format.date_formats, or the name of a time above it
    #          in this block (creation, submission_start, submission_end).
    # offset:  +N or -N followed by min, h, d or w; several may be chained.
    # HH:MM:   a clock time — kept for creation, dropped for the others, which
    #          the server stores as plain dates.
    # So "today" for all of them works, as does "2026-10-01", "today+7d" or
    # "submission_start+14d". null leaves that time unset.
    "times": {
        # When the challenge goes live (the server's publish_schedule). Only
        # sent when publishing: now or in the past publishes at once, in the
        # future makes it SCHEDULED. A draft ignores it, as the server does.
        "creation": "today",
        "submission_start": "today",
        "submission_end": "today+14d",
        "evaluation_end": "today+21d",
    },
    # How the expressions above are read and written.
    "time_format": {
        # The server stamps everything in Asia/Kolkata; "today" is read there.
        "timezone": "Asia/Kolkata",
        "date_formats": ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S%z",
                         "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"],
        # How the resolved values are written into the request body. The
        # timeline columns are plain dates; publish_schedule is a datetime.
        "send_date_format": "%Y-%m-%d",
        "send_datetime_format": "iso",
    },
    # The server does not check the timeline order on create at all — only the
    # update route does, when publishing — so "today" for every time is
    # accepted even with draft false. The check here is advisory: auto warns
    # (the same problems would block a later publish through the update
    # scripts), fail stops the run, off says nothing.
    "validate": {
        "mode": "auto",
        "starts_not_before_publish": True,
        "ends_after_starts": True,
        "evaluation_after_ends": True,
    },
    "create": {
        "omit_null_fields": True,
        "expect_http_status": [200, 201],
        # Read the challenge back through GET /challenge/admin/challenge/{id}.
        "verify_after": True,
        # DRAFT, SCHEDULED or PUBLISHED; blank only logs what came back.
        "expect_status": "",
        "settle_seconds": 0,
    },
    "endpoints": {
        "kc_token": "/realms/{realm}/protocol/openid-connect/token",
        "admin_create": "/challenge/admin/challenge",
        "admin_get": "/challenge/admin/challenge/{competition_id}",
        "attachment": "/challenge/attachment",
    },
    "output": {
        "enabled": True,
        # Written beside this script; the other scripts read it across.
        "file": "challenge_created.json",
    },
    "logging": {
        "level": "INFO",
        "print_requests": True,
        "print_responses": True,
        "response_preview_chars": 2000,
        # Off means bodies print verbatim — the admin password and access
        # tokens included. Turn it on before sharing a log.
        "mask_secrets": False,
    },
}

# Config key → request-body field, in the order they resolve: a later time may
# be written relative to an earlier one, by either name.
TIMES = {
    "creation": "publish_schedule",
    "submission_start": "submission_starts_at",
    "submission_end": "submission_ends_at",
    "evaluation_end": "evaluation_ends_at",
}
DATE_FIELDS = ("submission_starts_at", "submission_ends_at", "evaluation_ends_at")
DATETIME_FIELDS = ("publish_schedule",)


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
    """Password-grant tokens for platform accounts. Nothing else is needed here."""

    def __init__(self, config: dict[str, Any], endpoints: dict[str, Any]):
        self.base_url = require_string(config.get("url"), "keycloak.url").rstrip("/")
        self.realm = require_string(config.get("realm"), "keycloak.realm")
        self.user_client_id = require_string(
            config.get("user_client_id"), "keycloak.user_client_id"
        )
        self.user_client_secret = str(config.get("user_client_secret") or "")
        self.timeout = float(config.get("timeout_seconds") or 30)
        self.endpoints = endpoints
        self.session = requests.Session()
        self.session.verify = bool(config.get("verify_tls", True))

    def path(self, name: str, **fields: str) -> str:
        template = self.endpoints.get(name)
        if not isinstance(template, str) or not template:
            raise ConfigError(f"'endpoints.{name}' must be a non-empty string")
        return template.format(realm=self.realm, **fields)

    def user_token(self, username: str, password: str) -> str:
        path = self.path("kc_token")
        form = {
            "grant_type": "password",
            "client_id": self.user_client_id,
            "username": username,
            "password": password,
        }
        if self.user_client_secret:
            form["client_secret"] = self.user_client_secret
        label = f"token for {username}"
        log_request(label, "POST", path, body=form)
        response = self.session.post(
            f"{self.base_url}/{path.lstrip('/')}",
            data=form,
            timeout=self.timeout,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code != 200:
            log_response(label, "POST", path, response.status_code, response.text[:400])
            raise ApiError(f"{label} failed ({response.status_code}): {response.text[:400]}")
        payload = response.json()
        log_response(label, "POST", path, response.status_code, payload)
        return payload["access_token"]


def actor_token(config: dict[str, Any], block: dict[str, Any], label: str) -> str:
    """A ready-made token from the config, or one minted from credentials."""
    token = str(block.get("token") or "").strip()
    if token:
        LOG.info("Using the configured %s token", label)
        return token
    username = require_string(block.get("username"), f"{label}.username")
    password = require_string(block.get("password"), f"{label}.password")
    LOG.info("Signing in as %s %s", label, username)
    return Keycloak(config["keycloak"], config["endpoints"]).user_token(username, password)


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
                f"{describe_error(payload)}"
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


def data_of(payload: Any) -> dict[str, Any]:
    """The `data` object of a response envelope, or the payload itself."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload
    return {}


# --------------------------------------------------------------------- time

_OFFSET = re.compile(r"([+-]\s*\d+)\s*(min|h|d|w)\b")
_CLOCK = re.compile(r"\s(\d{1,2}):(\d{2})\s*$")


def zone(config: dict[str, Any]) -> ZoneInfo:
    name = str(config["time_format"].get("timezone") or "Asia/Kolkata")
    try:
        return ZoneInfo(name)
    except Exception as exc:  # unknown zone, or tzdata not installed
        raise ConfigError(f"'time_format.timezone' {name!r} is not a known timezone: {exc}") from exc


def parse_absolute(text: str, config: dict[str, Any], tz: ZoneInfo) -> datetime | None:
    """An absolute date/datetime in one of time.date_formats, or ISO 8601."""
    for fmt in config["time_format"].get("date_formats") or []:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def resolve_expression(
    spec: Any, name: str, bases: dict[str, datetime | None], config: dict[str, Any]
) -> datetime | None:
    """Turn one date expression into an aware datetime, or None when unset.

    `bases` holds the dates already resolved in this run (and, in the update
    scripts, what the server currently has), so one can be written relative to
    another: `submission_ends_at: "submission_starts_at+14d"`.
    """
    if spec is None:
        return None
    if isinstance(spec, (date, datetime)):
        spec = spec.isoformat()
    text = str(spec).strip()
    if not text:
        return None
    tz = zone(config)

    clock = None
    match = _CLOCK.search(text)
    if match:
        clock = (int(match.group(1)), int(match.group(2)))
        text = text[: match.start()].strip()

    first = _OFFSET.search(text)
    base_text = text[: first.start()].strip() if first else text
    tail = text[first.start():] if first else ""
    if tail and _OFFSET.sub("", tail).strip():
        raise ConfigError(f"'{name}': cannot read the offset part {tail!r} of {spec!r}")

    lowered = base_text.lower()
    if lowered in ("", "today", "now"):
        value = datetime.now(tz)
    elif lowered == "tomorrow":
        value = datetime.now(tz) + timedelta(days=1)
    elif lowered == "yesterday":
        value = datetime.now(tz) - timedelta(days=1)
    elif lowered in bases:
        value = bases[lowered]
        if value is None:
            raise ConfigError(
                f"'{name}' is written relative to {base_text}, which is not set"
            )
    else:
        value = parse_absolute(base_text, config, tz)
        if value is None:
            known = ", ".join(sorted(k for k, v in bases.items() if v is not None))
            raise ConfigError(
                f"'{name}': cannot read {base_text!r} as a date (formats: "
                f"{', '.join(config['time_format'].get('date_formats') or [])}), a keyword "
                f"(today, tomorrow, yesterday) or a known date ({known or 'none yet'})"
            )

    for amount, unit in _OFFSET.findall(tail):
        count = int(amount.replace(" ", ""))
        value += {
            "min": timedelta(minutes=count),
            "h": timedelta(hours=count),
            "d": timedelta(days=count),
            "w": timedelta(weeks=count),
        }[unit]

    if clock:
        value = value.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
    return value


def server_time_local(raw: Any, config: dict[str, Any]) -> str | None:
    """A datetime the server returned, as an ISO string in the configured zone.

    The server stamps in Asia/Kolkata, the column is timestamptz so Postgres
    keeps UTC, and the response encoder prints the UTC value with
    strftime("%Y-%m-%dT%H:%M:%S") — no offset. So a bare server datetime is
    UTC, whatever the clock in it looks like.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(zone(config)).isoformat()


def format_date(value: datetime, config: dict[str, Any]) -> str:
    return value.strftime(str(config["time_format"].get("send_date_format") or "%Y-%m-%d"))


def format_datetime(value: datetime, config: dict[str, Any]) -> str:
    fmt = str(config["time_format"].get("send_datetime_format") or "iso")
    return value.isoformat() if fmt.lower() == "iso" else value.strftime(fmt)


def resolve_times(
    config: dict[str, Any], overrides: dict[str, Any], publishing: bool
) -> tuple[dict[str, datetime | None], dict[str, str]]:
    """The `times` block resolved in order, keyed by request-body field.

    Returns the aware datetimes (for validation) and the strings to send.
    `creation` only goes on the wire when publishing, and only when it is in
    the future — the server treats a publish_schedule as "schedule for then",
    so "today"/"now" means publish at once and is simply left out.
    """
    times = config["times"]
    if not isinstance(times, dict):
        raise ConfigError("'times' must be an object")
    # Every name is known from the start — by config key and by body field —
    # so a reference to one that is not set yet is reported as exactly that.
    resolved: dict[str, datetime | None] = {}
    for key, field_name in TIMES.items():
        resolved[key] = resolved[field_name] = None
    sent: dict[str, str] = {}
    for key, field_name in TIMES.items():
        spec = overrides.get(key) if overrides.get(key) is not None else times.get(key)
        value = resolve_expression(spec, f"times.{key}", resolved, config)
        resolved[key] = resolved[field_name] = value
        if value is None:
            continue
        if field_name in DATETIME_FIELDS:
            if not publishing:
                LOG.info("times.creation %s is noted but not sent: a draft has no publish time",
                         format_datetime(value, config))
            elif value <= datetime.now(zone(config)):
                LOG.info("times.creation %s is not in the future: publishing at once",
                         format_datetime(value, config))
            else:
                sent[field_name] = format_datetime(value, config)
        else:
            sent[field_name] = format_date(value, config)
    return resolved, sent


def validate_timeline(
    config: dict[str, Any], resolved: dict[str, datetime | None], publishing: bool
) -> None:
    """Apply the server's publish-time rules locally, before anything is sent.

    The server only checks these when publishing; a draft with a broken
    timeline is accepted silently and then cannot be published.
    """
    rules = config["validate"]
    mode = str(rules.get("mode") or "auto").lower()
    if mode == "off":
        return
    if mode == "auto":
        mode = "warn"

    starts = resolved.get("submission_starts_at")
    ends = resolved.get("submission_ends_at")
    evaluation = resolved.get("evaluation_ends_at")
    # A creation time that is not in the future means "publish now".
    now = datetime.now(zone(config))
    publish_at = max(resolved.get("publish_schedule") or now, now)

    problems = []
    if rules.get("starts_not_before_publish", True) and starts and starts.date() < publish_at.date():
        problems.append(
            f"submission_starts_at {starts.date()} is before the publish date "
            f"{publish_at.date()}; the server refuses to publish that"
        )
    if rules.get("ends_after_starts", True) and starts and ends and ends.date() <= starts.date():
        problems.append(
            f"submission_ends_at {ends.date()} is not after submission_starts_at {starts.date()}"
        )
    if rules.get("evaluation_after_ends", True) and ends and evaluation and evaluation.date() <= ends.date():
        problems.append(
            f"evaluation_ends_at {evaluation.date()} is not after submission_ends_at {ends.date()}"
        )
    if publishing:
        for name in DATE_FIELDS:
            if resolved.get(name) is None:
                problems.append(f"{name} is not set, and publishing requires it")

    for problem in problems:
        if mode == "fail":
            LOG.error("Timeline: %s", problem)
        else:
            LOG.warning("Timeline: %s", problem)
    if problems and mode == "fail":
        raise ConfigError(
            f"{len(problems)} timeline problem(s); fix the dates, or set validate.mode "
            "to \"warn\" to send them anyway"
        )
    # The server returns published_at as UTC with no offset, and the public
    # detail page parses that as local time, so a publish between 00:00 and
    # 05:30 IST (still the previous day in UTC) shows yesterday as the start.
    if publishing and publish_at.astimezone(timezone.utc).date() != publish_at.date():
        LOG.warning(
            "Publishing at %s: in UTC that is still %s, and the UI's challenge detail "
            "page shows the publish date in UTC — the start will read as %s. Set "
            "times.creation to a time after 05:30 (e.g. \"today 06:00\") to schedule "
            "it instead",
            publish_at.strftime("%H:%M %Z"), publish_at.astimezone(timezone.utc).date(),
            publish_at.astimezone(timezone.utc).date(),
        )
    if publishing and ends and ends.date() <= now.date():
        LOG.warning(
            "submission_ends_at %s is not after today: the server's cron moves a "
            "published challenge to EVALUATION once that date has passed, so it will "
            "show under 'evaluation' rather than 'published' from tomorrow", ends.date(),
        )


# -------------------------------------------------------------- handoff file


def resolve_path(name: str, config_path: Path) -> Path:
    """A configured file path, read relative to the config file that named it."""
    candidate = Path(name)
    return candidate if candidate.is_absolute() else config_path.resolve().parent / candidate


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    LOG.info("Wrote %s", path)


def write_output(config: dict[str, Any], config_path: Path, record: dict[str, Any]) -> None:
    output = config["output"]
    if not output.get("enabled", True):
        return
    write_json(resolve_path(str(output.get("file") or "challenge_created.json"), config_path), record)


# -------------------------------------------------------------------- body


def resolve_title(config: dict[str, Any], override: str | None) -> str:
    """The title to create: the command line, then the config, then a template."""
    challenge = config["challenge"]
    if override and override.strip():
        return override.strip()
    literal = str(challenge.get("title") or "").strip()
    if literal:
        return literal
    template = str(challenge.get("title_template") or "{prefix}-{timestamp}-{random}")
    fields = {
        "prefix": str(challenge.get("title_prefix") or "challenge"),
        # Stamped in time_format.timezone, so the title reads like the clock.
        "timestamp": datetime.now(zone(config)).strftime(
            str(challenge.get("timestamp_format") or "%Y%m%d%H%M%S")
        ),
        "random": secrets.token_hex(max(int(challenge.get("random_hex_bytes") or 2), 1)),
    }
    try:
        return template.format(**fields).strip()
    except KeyError as exc:
        raise ConfigError(f"'challenge.title_template' uses an unknown placeholder {exc}") from exc


def build_body(
    config: dict[str, Any], title: str, draft: bool, sent_times: dict[str, str]
) -> dict[str, Any]:
    """The POST body, field for field from the config."""
    challenge = config["challenge"]
    body: dict[str, Any] = {"draft": bool(draft), "title": title}
    for name in (
        "subtitle", "overview", "description", "image_url", "constraints",
        "evaluation_criteria_definition", "submission_file_definition",
        "other_resources", "prize_type", "total_pool_amount", "currency",
        "prize_pool_description", "rules_and_guidelines", "dataset_description",
        "data_models", "ai_models", "additional_assets",
    ):
        body[name] = challenge.get(name)
    body.update(sent_times)
    extra = challenge.get("extra_fields") or {}
    if not isinstance(extra, dict):
        raise ConfigError("'challenge.extra_fields' must be an object")
    body.update(extra)
    if config["create"].get("omit_null_fields", True):
        body = {key: value for key, value in body.items() if value is not None}
    return body


# ------------------------------------------------------------------ uploads


def upload_file(
    config: dict[str, Any], api: Community, token: str, local: Path, batch_id: str
) -> str:
    """Put one local file into the challenge bucket and return its S3 key.

    POST /challenge/attachment answers with a presigned PUT URL for a temp key;
    the bytes go straight to S3; the key then goes into the create body, and
    the server copies it into place under the new challenge.
    """
    uploads = config["uploads"]
    if not local.is_file():
        raise ConfigError(f"upload file not found: {local}")
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
    content_type = str(uploads.get("content_type") or "application/octet-stream")
    log_request(f"upload {local.name}", "PUT", url.split("?")[0], body=f"<{local.stat().st_size} bytes>")
    response = requests.put(
        url, data=local.read_bytes(), headers={"Content-Type": content_type},
        timeout=float(uploads.get("timeout_seconds") or 120), verify=api.session.verify,
    )
    log_response(f"upload {local.name}", "PUT", url.split("?")[0], response.status_code, response.text[:200])
    if response.status_code not in (200, 201, 204):
        raise ApiError(f"upload of {local.name} failed ({response.status_code}): {response.text[:400]}")
    LOG.info("Uploaded %s as %s", local, key)
    return key


def planned_uploads(config: dict[str, Any], config_path: Path) -> list[tuple[str, Path, str]]:
    """(body field, local path, description) for every file the config names."""
    uploads = config["uploads"]
    if not uploads.get("enabled", True):
        return []
    plan: list[tuple[str, Path, str]] = []
    rules = str(uploads.get("rules_and_guidelines_file") or "").strip()
    if rules:
        plan.append(("rules_and_guidelines", resolve_path(rules, config_path), ""))
    image = str(uploads.get("image_file") or "").strip()
    if image:
        plan.append(("image_url", resolve_path(image, config_path), ""))
    for asset in uploads.get("additional_asset_files") or []:
        if not isinstance(asset, dict) or not str(asset.get("file") or "").strip():
            raise ConfigError("'uploads.additional_asset_files' entries need a \"file\"")
        plan.append(("additional_assets", resolve_path(str(asset["file"]), config_path),
                     str(asset.get("description") or "")))
    return plan


def apply_uploads(
    config: dict[str, Any], api: Community, token: str, body: dict[str, Any],
    plan: list[tuple[str, Path, str]],
) -> None:
    """Upload every planned file and put the keys into the body."""
    if not plan:
        return
    batch_id = str(uuid.uuid4())
    assets: list[dict[str, str]] = []
    for field_name, local, description in plan:
        key = upload_file(config, api, token, local, batch_id)
        if field_name == "additional_assets":
            assets.append({"object_key": key, "description": description})
        else:
            body[field_name] = key
    if assets:
        body["additional_assets"] = assets


# ---------------------------------------------------------------------- run


def create_challenge(
    config: dict[str, Any], api: Community, token: str, body: dict[str, Any]
) -> dict[str, Any]:
    create = config["create"]
    expect = tuple(int(code) for code in create.get("expect_http_status") or (200, 201))
    payload = api.request(
        "POST", api_path(config, "admin_create"), "create challenge", token,
        json_body=body, expect=expect,
    )
    data = data_of(payload)
    competition_id = str(data.get("competition_id") or data.get("id") or "").strip()
    if not competition_id:
        raise ApiError(f"create challenge: no competition_id in the response — {preview(payload)}")
    status = str(data.get("status") or "")
    LOG.info("Created challenge %s (%s) with status %s", body["title"], competition_id, status or "?")

    record: dict[str, Any] = {
        "kind": "challenge",
        "competition_id": competition_id,
        "title": body["title"],
        "status": status,
        "draft": body["draft"],
        "created_by": str(config["cos_admin"].get("username") or "") or None,
        "community_base_url": api.base_url,
        "timelines": {name: body.get(name) for name in DATE_FIELDS},
        "creation_time": body.get("publish_schedule"),
        "created_at": datetime.now(zone(config)).isoformat(),
    }

    settle = float(create.get("settle_seconds") or 0)
    if settle > 0:
        time.sleep(settle)

    if create.get("verify_after", True):
        payload = api.request(
            "GET", api_path(config, "admin_get", competition_id=competition_id),
            "read challenge back", token,
        )
        detail = data_of(payload)
        record["status"] = str(detail.get("status") or record["status"])
        timelines = detail.get("timelines")
        if isinstance(timelines, dict):
            record["timelines"] = {name: timelines.get(name) for name in DATE_FIELDS}
        # The server stores these in UTC and answers without an offset, so the
        # same instant is shown again in time_format.timezone.
        for name in ("published_at", "scheduled_publish_at"):
            record[name] = detail.get(name)
            record[f"{name}_local"] = server_time_local(detail.get(name), config)
        expected = str(create.get("expect_status") or "").strip().upper()
        if expected and record["status"].upper() != expected:
            raise ApiError(
                f"challenge {competition_id} has status {record['status']}, expected {expected}"
            )
    return record


def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(
        str(args.log_level or config["logging"].get("level") or "INFO").upper()
    )
    configure_output(config)

    title = resolve_title(config, args.title)
    draft = False if args.publish else bool(config["challenge"].get("draft", True))
    overrides = {
        "creation": args.creation_time,
        "submission_start": args.submission_start,
        "submission_end": args.submission_end,
        "evaluation_end": args.evaluation_end,
    }
    resolved, sent_times = resolve_times(config, overrides, publishing=not draft)
    validate_timeline(config, resolved, publishing=not draft)
    body = build_body(config, title, draft, sent_times)

    if not draft and body.get("publish_schedule"):
        LOG.warning(
            "Creating a SCHEDULED challenge: the server's cron publishes it at %s, and "
            "from then on it can be neither updated nor deleted through the API",
            body["publish_schedule"],
        )
    elif not draft:
        LOG.warning(
            "Creating a PUBLISHED challenge: once published it can be neither updated "
            "nor deleted through the API"
        )

    plan = planned_uploads(config, config_path)
    for field_name, local, _ in plan:
        if not local.is_file():
            raise ConfigError(f"uploads: {field_name} file not found: {local}")

    log_block("=== challenge to create ===", {
        "title": title,
        "draft": draft,
        "creation": format_datetime(resolved["creation"], config) if resolved["creation"] else None,
        **{name: sent_times.get(name) for name in DATE_FIELDS},
        "community": config["community"].get("base_url"),
        "cos_admin": config["cos_admin"].get("username") or "(token)",
        "uploads": ", ".join(f"{field_name}={local.name}" for field_name, local, _ in plan) or "none",
    })

    if args.dry_run:
        LOG.info("Dry run — no calls will be made")
        for field_name, local, _ in plan:
            LOG.info("Would upload %s and use its key as %s", local, field_name)
        LOG.info("Would POST %s with body %s", api_path(config, "admin_create"), preview(body))
        if config["output"].get("enabled", True):
            LOG.info("Would write %s",
                     resolve_path(str(config["output"].get("file")), config_path))
        return 0

    require_requests()
    token = actor_token(config, config["cos_admin"], "cos_admin")
    api = Community(config["community"])

    apply_uploads(config, api, token, body, plan)
    record = create_challenge(config, api, token, body)
    write_output(config, config_path, record)
    log_block("=== challenge created ===", record)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("challenge_creation_config.json"),
        help="JSON config path (default: challenge_creation_config.json beside this script)",
    )
    parser.add_argument("--title", help="override the configured or generated title")
    parser.add_argument("--publish", action="store_true",
                        help="send draft=false, publishing (or scheduling) the challenge at once")
    parser.add_argument("--creation-time", metavar="EXPR", help="override times.creation")
    parser.add_argument("--submission-start", metavar="EXPR", help="override times.submission_start")
    parser.add_argument("--submission-end", metavar="EXPR", help="override times.submission_end")
    parser.add_argument("--evaluation-end", metavar="EXPR", help="override times.evaluation_end")
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
