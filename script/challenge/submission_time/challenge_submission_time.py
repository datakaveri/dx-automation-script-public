#!/usr/bin/env python3
"""Set the submission window of one challenge in dx-community-layer.

Usage:
    python challenge_submission_time.py challenge_submission_time_config.json
    python challenge_submission_time.py challenge_submission_time_config.json --competition-id <uuid>
    python challenge_submission_time.py challenge_submission_time_config.json --submission-start today --submission-end today+10d
    python challenge_submission_time.py challenge_submission_time_config.json --publish
    python challenge_submission_time.py challenge_submission_time_config.json --dry-run

The window is `submission_starts_at` and `submission_ends_at`, written through
`PUT /challenge/admin/challenge/{id}` as the COS admin. The server only accepts
that call while the challenge is DRAFT or SCHEDULED — a published challenge is
frozen — so the script reads the challenge first and stops with a clear message
if it is past editing.

Both come from the `times` block of the config — `times.submission_start` and
`times.submission_end` — and either may be null to keep what the server has.
Each is an expression: `today`, an absolute `2026-10-01`, `today+1d`,
`submission_start+14d` (relative to the value set in this run, or failing that
the one already on the server). "today" for both is accepted; the server only
insists on the order when publishing.

`update.draft` is true by default, which keeps the challenge editable. Set it
false (or pass `--publish`) to publish in the same call; the server then
requires every field to be filled in and the whole timeline to be in order, and
the script checks the order locally first.

Which challenge: `--competition-id`, `target.competition_id`, `target.title`,
or the handoff file the creation script wrote (`target.input_file`). That file
is updated afterwards with the dates now on the server.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import requests
except ModuleNotFoundError:  # Allows --help and config validation before install.
    requests = None  # type: ignore[assignment]


LOG = logging.getLogger("challenge_submission_time")

# Every key the script reads, with the value used when the config omits it. The
# shipped example config repeats these, so any of them can be pasted over.
DEFAULT_CONFIG: dict[str, Any] = {
    "community": {
        "base_url": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    "keycloak": {
        "url": "",
        "realm": "",
        "user_client_id": "",
        "user_client_secret": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    # Who edits the challenge. A ready-made token wins over credentials.
    "cos_admin": {
        "username": "",
        "password": "",
        "token": "",
    },
    # Which challenge. The command line wins, then competition_id, then title
    # (looked up in the admin lists), then the handoff file.
    "target": {
        "competition_id": "",
        "title": "",
        "input_file": "../creation/challenge_created.json",
        "require_input_file": False,
        # Write the dates now on the server back into that file.
        "update_input_file": True,
    },
    # The times to set, each an expression:
    #   [base][offset...][ HH:MM]
    # base:    today/now (or blank), tomorrow, yesterday, an absolute date in
    #          one of time_format.date_formats, or the name of a time —
    #          creation, submission_start, submission_end, evaluation_end —
    #          meaning the value set in this run, else the one on the server.
    # offset:  +N or -N followed by min, h, d or w; several may be chained.
    # So "today" for everything works, as does "2026-10-01" or "today+14d".
    # null keeps the server's value. This script writes the submission window;
    # `creation` is only used when publishing (update.draft false): a future
    # time schedules the challenge for then, now or the past publishes at once.
    "times": {
        "creation": "today",
        "submission_start": "today",
        "submission_end": "today+14d",
    },
    "update": {
        # true keeps the challenge a draft (editable). false publishes it in
        # the same call — or schedules it when times.creation is in the future
        # — and after that nothing about it can be changed or deleted via the
        # API.
        "draft": True,
        # Statuses the server will accept an update for. Empty means do not
        # check; the server will refuse anything else with a 400 anyway.
        "require_status": ["DRAFT", "SCHEDULED"],
        "expect_http_status": [200],
        # Read the challenge back and check the dates landed.
        "verify_after": True,
        "expect_status": "",
        "settle_seconds": 0,
        # Anything else to put in the PUT body verbatim.
        "extra_fields": {},
    },
    # How the challenge is found by title: which admin lists to page and how far.
    "lookup": {
        "choices": ["draft", "scheduled", "published", "evaluation", "completed", "cancelled"],
        "page_size": 100,
        "max_pages": 20,
        # The list endpoint's `query` is a prefix full-text search that does
        # not cope with hyphenated titles; off pages the whole list instead.
        "use_query": False,
    },
    # How the expressions in `times` are read and written.
    "time_format": {
        "timezone": "Asia/Kolkata",
        "date_formats": ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S%z",
                         "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"],
        "send_date_format": "%Y-%m-%d",
        "send_datetime_format": "iso",
    },
    # auto: fail when publishing, warn for a draft. Or fail / warn / off.
    "validate": {
        "mode": "auto",
        "starts_not_before_publish": True,
        "ends_after_starts": True,
        "evaluation_after_ends": True,
    },
    "endpoints": {
        "kc_token": "/realms/{realm}/protocol/openid-connect/token",
        "admin_get": "/challenge/admin/challenge/{competition_id}",
        "admin_update": "/challenge/admin/challenge/{competition_id}",
        "admin_list": "/challenge/admin/challenges/{choice}",
    },
    "logging": {
        "level": "INFO",
        "print_requests": True,
        "print_responses": True,
        "response_preview_chars": 2000,
        "mask_secrets": False,
    },
}

DATE_FIELDS = ("submission_starts_at", "submission_ends_at", "evaluation_ends_at")
# Config key → request-body field. An expression may use either name.
TIMES = {
    "creation": "publish_schedule",
    "submission_start": "submission_starts_at",
    "submission_end": "submission_ends_at",
    "evaluation_end": "evaluation_ends_at",
}
# The fields this script writes, in the order they are resolved.
MANAGED = (("submission_starts_at", "submission_start"), ("submission_ends_at", "submission_end"))


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
    """Password-grant tokens for platform accounts."""

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
    """The community-layer HTTP API, one bearer token per call."""

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
    if payload.get("detail"):
        parts.append(json.dumps(payload["detail"], default=str)[:600])
    return " | ".join(parts) or json.dumps(payload, default=str)[:400]


def data_of(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload
    return {}


def rows_of(payload: Any) -> list[Any]:
    """The list of records in a list response, whichever key it sits under."""
    data = data_of(payload)
    for key in ("competitions", "challenges", "results", "items"):
        if isinstance(data.get(key), list):
            return data[key]
    return payload if isinstance(payload, list) else []


# --------------------------------------------------------------------- time

_OFFSET = re.compile(r"([+-]\s*\d+)\s*(min|h|d|w)\b")
_CLOCK = re.compile(r"\s(\d{1,2}):(\d{2})\s*$")


def zone(config: dict[str, Any]) -> ZoneInfo:
    name = str(config["time_format"].get("timezone") or "Asia/Kolkata")
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise ConfigError(f"'time_format.timezone' {name!r} is not a known timezone: {exc}") from exc


def parse_absolute(text: str, config: dict[str, Any], tz: ZoneInfo) -> datetime | None:
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
    """Turn one date expression into an aware datetime, or None when unset."""
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
                f"'{name}' is written relative to {base_text}, which is set neither "
                "in this run nor on the server"
            )
    else:
        value = parse_absolute(base_text, config, tz)
        if value is None:
            known = ", ".join(sorted(k for k, v in bases.items() if v is not None))
            raise ConfigError(
                f"'{name}': cannot read {base_text!r} as a date (formats: "
                f"{', '.join(config['time_format'].get('date_formats') or [])}), a keyword "
                f"(today, tomorrow, yesterday) or a known date ({known or 'none'})"
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


def format_date(value: datetime, config: dict[str, Any]) -> str:
    return value.strftime(str(config["time_format"].get("send_date_format") or "%Y-%m-%d"))


def format_datetime(value: datetime, config: dict[str, Any]) -> str:
    fmt = str(config["time_format"].get("send_datetime_format") or "iso")
    return value.isoformat() if fmt.lower() == "iso" else value.strftime(fmt)


def server_time_local(raw: Any, config: dict[str, Any]) -> datetime | None:
    """A datetime the server returned, as an aware value in the configured zone.

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
    return parsed.astimezone(zone(config))


def server_bases(detail: dict[str, Any], config: dict[str, Any]) -> dict[str, datetime | None]:
    """What the server currently holds, as bases an expression may refer to."""
    tz = zone(config)
    bases: dict[str, datetime | None] = {}
    timelines = detail.get("timelines") if isinstance(detail.get("timelines"), dict) else {}
    for name in DATE_FIELDS:
        raw = timelines.get(name)
        bases[name] = parse_absolute(str(raw), config, tz) if raw else None
    for name in ("published_at", "scheduled_publish_at"):
        bases[name] = server_time_local(detail.get(name), config)
    # The same values under their config names, so an expression can say
    # "submission_start+14d" as well as "submission_starts_at+14d".
    for key, field_name in TIMES.items():
        bases[key] = bases.get(field_name)
    return bases


def validate_timeline(
    config: dict[str, Any], final: dict[str, datetime | None], publishing: bool
) -> None:
    """Apply the server's publish-time rules to what the timeline will be."""
    rules = config["validate"]
    mode = str(rules.get("mode") or "auto").lower()
    if mode == "off":
        return
    if mode == "auto":
        mode = "fail" if publishing else "warn"

    starts = final.get("submission_starts_at")
    ends = final.get("submission_ends_at")
    evaluation = final.get("evaluation_ends_at")
    # A creation time that is not in the future means "publish now".
    now = datetime.now(zone(config))
    publish_at = max(final.get("publish_schedule") or now, now)

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
            if final.get(name) is None:
                problems.append(f"{name} is not set, and publishing requires it")

    for problem in problems:
        (LOG.error if mode == "fail" else LOG.warning)("Timeline: %s", problem)
    if problems and mode == "fail":
        raise ConfigError(
            f"{len(problems)} timeline problem(s); fix the dates, or set validate.mode "
            "to \"warn\" to send them anyway"
        )



def publish_preflight(config: dict[str, Any], detail: dict[str, Any], body: dict[str, Any]) -> None:
    """The checks the server runs on a publish (admin_update_competition_handler),
    applied to what it currently holds plus what this call changes, so a
    refusal is explained before the call rather than by a bare 400."""
    mode = str(config["validate"].get("mode") or "auto").lower()
    if mode == "off":
        return
    evaluations = detail.get("evaluations") if isinstance(detail.get("evaluations"), dict) else {}
    prize = detail.get("prize_pools") if isinstance(detail.get("prize_pools"), dict) else {}
    timelines = detail.get("timelines") if isinstance(detail.get("timelines"), dict) else {}
    datasets = detail.get("datasets") if isinstance(detail.get("datasets"), dict) else {}
    # Mirrors the server's required_fields_map, keyed by the name it reports.
    present = {
        "title": detail.get("title"),
        "subtitle": detail.get("subtitle"),
        "overview": detail.get("overview"),
        "description": detail.get("detailed_description"),
        "constraints": detail.get("constraints"),
        "evaluation_criteria_definition": evaluations.get("evaluation_criteria"),
        "submission_file_definition": evaluations.get("submission_criteria"),
        "prize_pool_description": prize.get("prize_description"),
        "submission_starts_at": body.get("submission_starts_at") or timelines.get("submission_starts_at"),
        "submission_ends_at": body.get("submission_ends_at") or timelines.get("submission_ends_at"),
        "evaluation_ends_at": body.get("evaluation_ends_at") or timelines.get("evaluation_ends_at"),
        "rules_and_guidelines": detail.get("rules_and_guidelines"),
        "dataset_description": datasets.get("description"),
    }
    problems = [f"{name} is empty on the server" for name, value in present.items() if not value]
    if str(prize.get("prize_type") or "").upper() == "CASH":
        if not prize.get("currency"):
            problems.append("prize_type is CASH but currency is empty")
        if not prize.get("total_pool_amount"):
            problems.append("prize_type is CASH but total_pool_amount is empty")
    if not datasets.get("datasets") and not datasets.get("ai_models"):
        problems.append("data_models and ai_models are both empty; the server needs one of them to publish")
    for problem in problems:
        (LOG.error if mode != "warn" else LOG.warning)("Publish: %s", problem)
    if problems and mode != "warn":
        raise ConfigError(
            f"{len(problems)} thing(s) the server will refuse to publish with; fix them "
            "(the creation script's config, or the admin UI), or set validate.mode to \"warn\""
        )


# -------------------------------------------------------------- handoff file


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


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    LOG.info("Wrote %s", path)


def update_input_file(
    config: dict[str, Any], config_path: Path, competition_id: str, detail: dict[str, Any]
) -> None:
    """Write the dates now on the server back into the handoff record.

    Only when that record describes this challenge: a run against an id named
    on the command line must not overwrite the record of a different one.
    """
    target = config["target"]
    if not target.get("update_input_file", True):
        return
    name = str(target.get("input_file") or "").strip()
    if not name:
        return
    path = resolve_path(name, config_path)
    record = read_json(path)
    if not record:
        return
    recorded = str(record.get("competition_id") or "").strip()
    if recorded and recorded != competition_id:
        LOG.info("Leaving %s alone — it describes %s, not %s", path, recorded, competition_id)
        return
    timelines = detail.get("timelines") if isinstance(detail.get("timelines"), dict) else {}
    record["timelines"] = {name: timelines.get(name) for name in DATE_FIELDS}
    record["status"] = detail.get("status") or record.get("status")
    for name in ("published_at", "scheduled_publish_at"):
        record[name] = detail.get(name)
        local = server_time_local(detail.get(name), config)
        record[f"{name}_local"] = local.isoformat() if local else None
    record["updated_at"] = datetime.now(zone(config)).isoformat()
    write_json(path, record)


# -------------------------------------------------------------------- target


def find_by_title(config: dict[str, Any], api: Community, token: str, title: str) -> str:
    """Page the admin lists for an exact title and return its id."""
    lookup = config["lookup"]
    page_size = int(lookup.get("page_size") or 100)
    max_pages = int(lookup.get("max_pages") or 20)
    for choice in lookup.get("choices") or ["draft", "scheduled"]:
        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {"page": page, "limit": page_size}
            if lookup.get("use_query", False):
                params["query"] = title
            payload = api.request(
                "GET", api_path(config, "admin_list", choice=str(choice)),
                f"list {choice} challenges (page {page})", token, params=params,
            )
            rows = rows_of(payload)
            for row in rows:
                if isinstance(row, dict) and str(row.get("title") or "").strip() == title:
                    return str(row.get("id") or row.get("competition_id") or "")
            if len(rows) < page_size:
                break
    raise ConfigError(f"no challenge titled {title!r} in the admin lists {lookup.get('choices')}")


def resolve_target(
    config: dict[str, Any], config_path: Path, args: argparse.Namespace
) -> tuple[str, str]:
    """Which challenge to edit: (competition_id, title) — the id may be blank
    when only a title is known, in which case it is looked up once signed in."""
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

    competition_id = asked_id or str(record.get("competition_id") or "").strip()
    title = asked_title or str(record.get("title") or "").strip()
    if not competition_id and not title:
        raise ConfigError(
            "no challenge to edit: pass --competition-id or --title, set "
            "'target.competition_id' or 'target.title', or point 'target.input_file' "
            "at the file the creation script wrote"
        )
    return competition_id, title


# ---------------------------------------------------------------------- run


def plan_update(
    config: dict[str, Any], detail: dict[str, Any], args: argparse.Namespace, publishing: bool
) -> tuple[dict[str, Any], dict[str, datetime | None]]:
    """The PUT body and the timeline as it will stand once applied."""
    bases = server_bases(detail, config)
    final = dict(bases)
    body: dict[str, Any] = {"draft": not publishing}

    times = config["times"]
    if not isinstance(times, dict):
        raise ConfigError("'times' must be an object")
    overrides = {
        "creation": args.creation_time,
        "submission_start": args.submission_start,
        "submission_end": args.submission_end,
    }

    def spec_for(key: str) -> Any:
        return overrides[key] if overrides.get(key) is not None else times.get(key)

    # The publish time first, so the dates may be written relative to it.
    creation = resolve_expression(spec_for("creation"), "times.creation", final, config)
    final["creation"] = final["publish_schedule"] = creation
    if creation is not None and publishing:
        if creation <= datetime.now(zone(config)):
            LOG.info("times.creation %s is not in the future: publishing at once",
                     format_datetime(creation, config))
        else:
            body["publish_schedule"] = format_datetime(creation, config)

    for field_name, key in MANAGED:
        value = resolve_expression(spec_for(key), f"times.{key}", final, config)
        if value is not None:
            final[field_name] = final[key] = value
            body[field_name] = format_date(value, config)

    extra = config["update"].get("extra_fields") or {}
    if not isinstance(extra, dict):
        raise ConfigError("'update.extra_fields' must be an object")
    body.update(extra)
    return body, final


def apply_update(
    config: dict[str, Any], api: Community, token: str, competition_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    update = config["update"]
    expect = tuple(int(code) for code in update.get("expect_http_status") or (200,))
    api.request(
        "PUT", api_path(config, "admin_update", competition_id=competition_id),
        "update submission window", token, json_body=body, expect=expect,
    )
    settle = float(update.get("settle_seconds") or 0)
    if settle > 0:
        time.sleep(settle)
    if not update.get("verify_after", True):
        return {}
    detail = data_of(api.request(
        "GET", api_path(config, "admin_get", competition_id=competition_id),
        "read challenge back", token,
    ))
    timelines = detail.get("timelines") if isinstance(detail.get("timelines"), dict) else {}
    for field_name, _ in MANAGED:
        if field_name in body and str(timelines.get(field_name) or "") != body[field_name]:
            raise ApiError(
                f"{field_name} on the server is {timelines.get(field_name)!r}, "
                f"but {body[field_name]!r} was sent"
            )
    expected = str(update.get("expect_status") or "").strip().upper()
    status = str(detail.get("status") or "")
    if expected and status.upper() != expected:
        raise ApiError(f"challenge {competition_id} has status {status}, expected {expected}")
    return detail


def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(
        str(args.log_level or config["logging"].get("level") or "INFO").upper()
    )
    configure_output(config)

    competition_id, title = resolve_target(config, config_path, args)
    publishing = bool(args.publish) or not bool(config["update"].get("draft", True))

    if args.dry_run:
        LOG.info("Dry run — no calls will be made")
        LOG.info("Community layer at %s", config["community"].get("base_url"))
        LOG.info("Would edit challenge %s", competition_id or f"titled {title!r}")
        # Without the server's dates only self-contained expressions resolve.
        try:
            body, _ = plan_update(config, {}, args, publishing)
            LOG.info("Would PUT body %s", preview(body))
        except ConfigError as exc:
            LOG.info("Body depends on the server's current dates (%s)", exc)
        if publishing:
            LOG.warning("Would publish the challenge — irreversible")
        return 0

    require_requests()
    token = actor_token(config, config["cos_admin"], "cos_admin")
    api = Community(config["community"])
    if not competition_id:
        competition_id = find_by_title(config, api, token, title)
        LOG.info("Challenge %r is %s", title, competition_id)

    detail = data_of(api.request(
        "GET", api_path(config, "admin_get", competition_id=competition_id),
        "read challenge", token,
    ))
    status = str(detail.get("status") or "")
    required = [str(s).upper() for s in config["update"].get("require_status") or []]
    if required and status.upper() not in required:
        raise ApiError(
            f"challenge {competition_id} is {status or 'of unknown status'}; the server "
            f"only accepts updates while it is {' or '.join(required)}"
        )

    body, final = plan_update(config, detail, args, publishing)
    validate_timeline(config, final, publishing)
    if publishing:
        publish_preflight(config, detail, body)
    if len(body) == 1 and not publishing:
        LOG.warning("Neither times.submission_start nor times.submission_end is set; nothing to change")
        return 0
    if publishing:
        LOG.warning(
            "Publishing challenge %s in this call: afterwards it can be neither "
            "updated nor deleted through the API", competition_id,
        )

    current = detail.get("timelines") if isinstance(detail.get("timelines"), dict) else {}
    log_block("=== submission window ===", {
        "competition_id": competition_id,
        "title": detail.get("title") or title,
        "status": status,
        "submission_starts_at": f"{current.get('submission_starts_at')} → {body.get('submission_starts_at', '(unchanged)')}",
        "submission_ends_at": f"{current.get('submission_ends_at')} → {body.get('submission_ends_at', '(unchanged)')}",
        "evaluation_ends_at": current.get("evaluation_ends_at"),
        "draft": body["draft"],
        "creation": format_datetime(final["creation"], config) if final.get("creation") else None,
    })

    after = apply_update(config, api, token, competition_id, body)
    if after:
        update_input_file(config, config_path, competition_id, after)
        timelines = after.get("timelines") if isinstance(after.get("timelines"), dict) else {}
        log_block("=== challenge now ===", {
            "competition_id": competition_id,
            "title": after.get("title"),
            "status": after.get("status"),
            **{name: timelines.get(name) for name in DATE_FIELDS},
            "published_at": f"{after.get('published_at')} UTC → {server_time_local(after.get('published_at'), config) or ''}",
            "scheduled_publish_at": f"{after.get('scheduled_publish_at')} UTC → {server_time_local(after.get('scheduled_publish_at'), config) or ''}",
        })
    LOG.info("Submission window set on challenge %s", competition_id)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("challenge_submission_time_config.json"),
        help="JSON config path (default: challenge_submission_time_config.json beside this script)",
    )
    parser.add_argument("--competition-id", help="the challenge to edit")
    parser.add_argument("--title", help="the challenge to edit, looked up by exact title")
    parser.add_argument("--submission-start", metavar="EXPR", help="override times.submission_start")
    parser.add_argument("--submission-end", metavar="EXPR", help="override times.submission_end")
    parser.add_argument("--creation-time", metavar="EXPR", help="override times.creation")
    parser.add_argument("--publish", action="store_true",
                        help="send draft=false, publishing (or scheduling) the challenge")
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
