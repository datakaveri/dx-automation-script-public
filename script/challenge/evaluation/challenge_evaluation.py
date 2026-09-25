#!/usr/bin/env python3
"""Score the submissions of a challenge as the COS admin, and optionally
announce the result.

Usage:
    python challenge_evaluation.py challenge_evaluation_config.json
    python challenge_evaluation.py challenge_evaluation_config.json --competition-id <uuid>
    python challenge_evaluation.py challenge_evaluation_config.json --submission-id <uuid>
    python challenge_evaluation.py challenge_evaluation_config.json --announce
    python challenge_evaluation.py challenge_evaluation_config.json --dry-run

Each submission is scored through
`PUT /challenge/admin/submission/{submission_id}/evaluate` with a score, a
comment and a disqualify flag. The server accepts that at any time except on
a CANCELLED challenge — it does not wait for the EVALUATION status — so a
submission made today can be scored today. The score must be greater than
zero: the server treats 0 as "no score" and refuses to announce a result
while any submission still has none.

Which submissions: everything the admin list returns for the challenge
(`evaluate.which: "all"`), or only the ones in the handoff file the submission
script wrote (`"file"`), or the ids given (`evaluate.submission_ids`,
`--submission-id`). Scores come from `evaluate.score`: a fixed number, a
random one within a range, a descending series, or an explicit list.

`--announce` (or `announce.enabled`) then calls
`POST /challenge/admin/challenges/announce-result`, which moves the challenge
to COMPLETED. The server refuses that until the calendar day after
`submission_ends_at` (Asia/Kolkata), and until every non-disqualified
submission has a score, so the script checks both first and says plainly
when it cannot happen yet.

Everything the script touches — URLs, credentials, endpoint paths, the body —
comes from the JSON config. Values may reference the environment as ${VAR} or
${VAR:-fallback}.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import requests
except ModuleNotFoundError:  # Allows --help and config validation before install.
    requests = None  # type: ignore[assignment]


LOG = logging.getLogger("challenge_evaluation")

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
    # Who evaluates: must carry the cos_admin realm role. A token wins.
    "cos_admin": {
        "username": "",
        "password": "",
        "token": "",
    },
    # Which challenge. An id wins over a title; both win over the handoff
    # files: the one the submission script wrote, then the creation one.
    "target": {
        "competition_id": "",
        "title": "",
        "submissions_file": "../submission/submissions_created.json",
        "input_file": "../creation/challenge_created.json",
        "require_input_file": False,
    },
    # How --title is looked up: which admin lists to page and how far.
    "lookup": {
        "choices": ["published", "evaluation", "completed", "draft", "scheduled"],
        "page_size": 100,
        "max_pages": 20,
        "use_query": False,
    },
    "evaluate": {
        # "all": every submission the admin list returns for the challenge.
        # "file": only the ids in target.submissions_file.
        # "ids": only evaluate.submission_ids / --submission-id.
        "which": "all",
        "submission_ids": [],
        # Submissions that already carry a score are left alone unless this
        # is false.
        "skip_scored": True,
        "score": {
            # "fixed": always `fixed`; "random": uniform in [min, max];
            # "descending": max for the first, stepping down by `step`,
            # never below min; "list": `list` in order, repeating the last.
            "mode": "random",
            "fixed": 80,
            "min": 50,
            "max": 100,
            "step": 5,
            "list": [],
            "decimals": 1,
        },
        # Placeholders: {score}, {index}, {title}, {username}, {submission_id}.
        "comments_template": "Scored {score} by dx-automation-script.",
        "disqualify": False,
        "expect_http_status": [200],
        # Read the admin list back and check each score landed.
        "verify_after": True,
        "pause_seconds": 0,
    },
    "announce": {
        "enabled": False,
        "expect_http_status": [200],
        # Read the challenge back and expect this status afterwards.
        "expect_status": "COMPLETED",
    },
    "list": {
        "page_size": 100,
        "max_pages": 20,
    },
    "endpoints": {
        "kc_token": "/realms/{realm}/protocol/openid-connect/token",
        "admin_get": "/challenge/admin/challenge/{competition_id}",
        "admin_list": "/challenge/admin/challenges/{choice}",
        "admin_submissions": "/challenge/admin/challenges/{competition_id}/submissions",
        "evaluate": "/challenge/admin/submission/{submission_id}/evaluate",
        "announce": "/challenge/admin/challenges/announce-result",
    },
    "time_format": {
        "timezone": "Asia/Kolkata",
    },
    "output": {
        "enabled": True,
        "file": "submissions_evaluated.json",
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


def zone(config: dict[str, Any]) -> ZoneInfo:
    name = str(config["time_format"].get("timezone") or "Asia/Kolkata")
    try:
        return ZoneInfo(name)
    except Exception as exc:
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
    data = data_of(payload)
    for key in ("submissions", "competitions", "challenges", "results", "items"):
        if isinstance(data.get(key), list):
            return data[key]
    return payload if isinstance(payload, list) else []


# -------------------------------------------------------------------- files


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


# -------------------------------------------------------------------- target


def find_by_title(config: dict[str, Any], api: Community, token: str, title: str) -> str:
    lookup = config["lookup"]
    page_size = int(lookup.get("page_size") or 100)
    max_pages = int(lookup.get("max_pages") or 20)
    for choice in lookup.get("choices") or ["published"]:
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
) -> tuple[str, str, dict[str, Any]]:
    """(competition_id, title, submissions handoff record)."""
    target = config["target"]
    submissions_record: dict[str, Any] = {}
    creation_record: dict[str, Any] = {}
    for key, holder in (("submissions_file", "submissions"), ("input_file", "creation")):
        name = str(target.get(key) or "").strip()
        if not name:
            continue
        path = resolve_path(name, config_path)
        record = read_json(path)
        if record:
            LOG.info("Read %s", path)
        elif target.get("require_input_file", False):
            raise ConfigError(f"target.{key} {path} is missing or unreadable")
        if holder == "submissions":
            submissions_record = record
        else:
            creation_record = record

    asked_id = (args.competition_id or str(target.get("competition_id") or "")).strip()
    asked_title = (args.title or str(target.get("title") or "")).strip()

    competition_id = asked_id
    title = asked_title
    for record in (submissions_record, creation_record):
        recorded_id = str(record.get("competition_id") or "").strip()
        recorded_title = str(record.get("title") or "").strip()
        if not record:
            continue
        if (asked_id and recorded_id and asked_id != recorded_id) or (
            asked_title and recorded_title and asked_title != recorded_title and not asked_id
        ):
            LOG.warning("A handoff file describes %s (%s), not the challenge asked for — ignoring it",
                        recorded_title or "?", recorded_id or "?")
            if record is submissions_record:
                submissions_record = {}
            continue
        competition_id = competition_id or recorded_id
        title = title or recorded_title
    if not competition_id and not title:
        raise ConfigError(
            "no challenge to evaluate: pass --competition-id or --title, set "
            "'target.competition_id' or 'target.title', or point 'target.submissions_file' / "
            "'target.input_file' at the handoff files"
        )
    return competition_id, title, submissions_record


# ------------------------------------------------------------- submissions


def list_submissions(config: dict[str, Any], api: Community, token: str,
                     competition_id: str) -> list[dict[str, Any]]:
    """Every submission of the challenge, from the admin list, all pages."""
    listing = config["list"]
    page_size = int(listing.get("page_size") or 100)
    max_pages = int(listing.get("max_pages") or 20)
    rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload = api.request(
            "GET", api_path(config, "admin_submissions", competition_id=competition_id),
            f"list submissions (page {page})", token, params={"page": page, "limit": page_size},
        )
        batch = [row for row in rows_of(payload) if isinstance(row, dict)]
        rows.extend(batch)
        if len(batch) < page_size:
            break
    return rows


def submission_username(row: dict[str, Any]) -> str:
    user = row.get("user")
    if isinstance(user, dict):
        return str(user.get("email") or user.get("name") or user.get("id") or "")
    return str(row.get("user_id") or "")


def choose_submissions(
    config: dict[str, Any], args: argparse.Namespace, rows: list[dict[str, Any]],
    submissions_record: dict[str, Any],
) -> list[dict[str, Any]]:
    """The subset of the server's submissions this run scores, in list order."""
    evaluate = config["evaluate"]
    which = str(evaluate.get("which") or "all").lower()
    wanted: set[str] = set()
    if args.submission_id:
        which = "ids"
        wanted.update(str(item).strip() for item in args.submission_id)
    elif which == "ids":
        wanted.update(str(item).strip() for item in evaluate.get("submission_ids") or [])
        if not wanted:
            raise ConfigError("evaluate.which is \"ids\" but evaluate.submission_ids is empty")
    elif which == "file":
        for entry in submissions_record.get("submissions") or []:
            if isinstance(entry, dict) and entry.get("submission_id"):
                wanted.add(str(entry["submission_id"]))
        if not wanted:
            raise ConfigError("evaluate.which is \"file\" but the submissions handoff names none")
    elif which != "all":
        raise ConfigError("'evaluate.which' must be \"all\", \"file\" or \"ids\"")

    by_id = {str(row.get("id") or row.get("submission_id") or ""): row for row in rows}
    if which == "all":
        chosen = list(rows)
    else:
        missing = sorted(wanted - set(by_id))
        if missing:
            raise ConfigError(f"submissions not found on the challenge: {', '.join(missing)}")
        chosen = [by_id[item] for item in by_id if item in wanted]

    if evaluate.get("skip_scored", True):
        kept = []
        for row in chosen:
            if row.get("score") not in (None, 0, 0.0):
                LOG.info("Skipping %s — already scored %s", row.get("id"), row.get("score"))
                continue
            kept.append(row)
        chosen = kept
    return chosen


def planned_scores(config: dict[str, Any], count: int) -> list[float]:
    score = config["evaluate"]["score"]
    mode = str(score.get("mode") or "random").lower()
    decimals = int(score.get("decimals") if score.get("decimals") is not None else 1)
    low = float(score.get("min") if score.get("min") is not None else 50)
    high = float(score.get("max") if score.get("max") is not None else 100)
    values: list[float] = []
    if mode == "fixed":
        values = [float(score.get("fixed") if score.get("fixed") is not None else 80)] * count
    elif mode == "random":
        if low > high:
            raise ConfigError("'evaluate.score.min' must not exceed 'evaluate.score.max'")
        values = [random.uniform(low, high) for _ in range(count)]
    elif mode == "descending":
        step = float(score.get("step") if score.get("step") is not None else 5)
        values = [max(high - step * position, low) for position in range(count)]
    elif mode == "list":
        given = [float(item) for item in score.get("list") or []]
        if not given:
            raise ConfigError("'evaluate.score.mode' is \"list\" but 'evaluate.score.list' is empty")
        values = [given[min(position, len(given) - 1)] for position in range(count)]
    else:
        raise ConfigError("'evaluate.score.mode' must be fixed, random, descending or list")
    values = [round(value, decimals) if decimals >= 0 else value for value in values]
    if not config["evaluate"].get("disqualify", False):
        for value in values:
            if value <= 0:
                raise ConfigError(
                    f"a score of {value} would be stored as none — the server treats 0 as "
                    "unscored and will not announce a result; use a score above 0"
                )
    return values


def evaluate_one(
    config: dict[str, Any], api: Community, token: str, row: dict[str, Any],
    index: int, score: float,
) -> dict[str, Any]:
    evaluate = config["evaluate"]
    submission_id = str(row.get("id") or row.get("submission_id") or "")
    disqualify = bool(evaluate.get("disqualify", False))
    fields = {
        "score": score, "index": index, "title": row.get("title") or "",
        "username": submission_username(row), "submission_id": submission_id,
    }
    try:
        comments = str(evaluate.get("comments_template") or "").format(**fields)
    except KeyError as exc:
        raise ConfigError(f"'evaluate.comments_template' uses an unknown placeholder {exc}") from exc
    body: dict[str, Any] = {"disqualify": disqualify}
    if comments:
        body["comments"] = comments
    if not disqualify:
        body["score"] = score
    expect = tuple(int(code) for code in evaluate.get("expect_http_status") or (200,))
    api.request(
        "PUT", api_path(config, "evaluate", submission_id=submission_id),
        f"evaluate submission #{index}", token, json_body=body, expect=expect,
    )
    return {
        "index": index,
        "submission_id": submission_id,
        "title": row.get("title"),
        "username": fields["username"],
        "score": None if disqualify else score,
        "disqualified": disqualify,
        "comments": comments or None,
    }


def verify_scores(config: dict[str, Any], api: Community, token: str,
                  competition_id: str, results: list[dict[str, Any]]) -> None:
    rows = {str(row.get("id") or ""): row for row in list_submissions(config, api, token, competition_id)}
    for result in results:
        row = rows.get(result["submission_id"])
        if row is None:
            raise ApiError(f"submission {result['submission_id']} vanished from the admin list")
        if result["disqualified"]:
            if not row.get("is_disqualified"):
                raise ApiError(f"submission {result['submission_id']} is not marked disqualified")
        elif row.get("score") is None or abs(float(row["score"]) - float(result["score"])) > 1e-6:
            raise ApiError(
                f"submission {result['submission_id']} has score {row.get('score')}, "
                f"expected {result['score']}"
            )
        result["stored_score"] = row.get("score")
    LOG.info("Verified %d score(s) on the server", len(results))


# ---------------------------------------------------------------- announce


def announce_result(config: dict[str, Any], api: Community, token: str,
                    competition_id: str, detail: dict[str, Any],
                    rows: list[dict[str, Any]]) -> str:
    """Try to announce; returns the status afterwards, or '' when not attempted."""
    announce = config["announce"]
    timelines = detail.get("timelines") if isinstance(detail.get("timelines"), dict) else {}
    ends = str(timelines.get("submission_ends_at") or "")
    today = datetime.now(zone(config)).date().isoformat()
    if not ends:
        LOG.warning("Not announcing: the challenge has no submission_ends_at")
        return ""
    if ends >= today:
        LOG.warning(
            "Not announcing: submission_ends_at is %s and today is %s — the server "
            "accepts announce-result only from the day after the submission end date",
            ends, today,
        )
        return ""
    unscored = [str(row.get("id")) for row in rows
                if not row.get("is_disqualified") and row.get("score") in (None, 0, 0.0)]
    if unscored:
        LOG.warning("Not announcing: %d submission(s) still unscored: %s",
                    len(unscored), ", ".join(unscored))
        return ""
    expect = tuple(int(code) for code in announce.get("expect_http_status") or (200,))
    api.request(
        "POST", api_path(config, "announce"), "announce result", token,
        params={"competition_id": competition_id}, expect=expect,
    )
    after = data_of(api.request(
        "GET", api_path(config, "admin_get", competition_id=competition_id),
        "read challenge back", token,
    ))
    status = str(after.get("status") or "")
    expected = str(announce.get("expect_status") or "").strip().upper()
    if expected and status.upper() != expected:
        raise ApiError(f"challenge {competition_id} has status {status}, expected {expected}")
    LOG.info("Result announced — challenge is now %s", status or "?")
    return status


# ---------------------------------------------------------------------- run


def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(
        str(args.log_level or config["logging"].get("level") or "INFO").upper()
    )
    configure_output(config)

    competition_id, title, submissions_record = resolve_target(config, config_path, args)
    announcing = bool(args.announce or config["announce"].get("enabled", False))
    evaluate = config["evaluate"]

    log_block("=== evaluation ===", {
        "challenge": competition_id or f"(by title) {title}",
        "title": title or None,
        "which": "ids (command line)" if args.submission_id else evaluate.get("which"),
        "score mode": evaluate["score"].get("mode"),
        "disqualify": evaluate.get("disqualify", False),
        "announce": announcing,
        "community": config["community"].get("base_url"),
        "cos_admin": config["cos_admin"].get("username") or "(token)",
    })

    if args.dry_run:
        LOG.info("Dry run — no calls will be made")
        LOG.info("Would GET %s, PUT %s for each chosen submission%s",
                 api_path(config, "admin_submissions", competition_id=competition_id or "<id>"),
                 api_path(config, "evaluate", submission_id="<submission_id>"),
                 f", then POST {api_path(config, 'announce')}" if announcing else "")
        sample = planned_scores(config, 3)
        LOG.info("Scores would look like %s", sample)
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
    title = title or str(detail.get("title") or "")
    if str(detail.get("status") or "").upper() == "CANCELLED":
        raise ApiError("the challenge is CANCELLED; the server refuses evaluations on it")

    rows = list_submissions(config, api, token, competition_id)
    LOG.info("The challenge has %d submission(s)", len(rows))
    chosen = choose_submissions(config, args, rows, submissions_record)
    if not chosen:
        LOG.warning("Nothing to evaluate")
    scores = planned_scores(config, len(chosen))

    results: list[dict[str, Any]] = []
    pause = float(evaluate.get("pause_seconds") or 0)
    for index, (row, score) in enumerate(zip(chosen, scores), start=1):
        result = evaluate_one(config, api, token, row, index, score)
        results.append(result)
        log_block(f"=== submission #{index} ===", result)
        if pause > 0 and index < len(chosen):
            time.sleep(pause)

    if results and evaluate.get("verify_after", True):
        verify_scores(config, api, token, competition_id, results)

    status_after = str(detail.get("status") or "")
    if announcing:
        rows = list_submissions(config, api, token, competition_id)
        status_after = announce_result(config, api, token, competition_id, detail, rows) or status_after

    record = {
        "kind": "challenge_evaluations",
        "competition_id": competition_id,
        "title": title,
        "status": status_after,
        "community_base_url": config["community"].get("base_url"),
        "evaluated_by": config["cos_admin"].get("username") or None,
        "evaluated_at": datetime.now(zone(config)).isoformat(),
        "evaluations": results,
    }
    if config["output"].get("enabled", True):
        write_json(resolve_path(str(config["output"].get("file") or "submissions_evaluated.json"),
                                config_path), record)
    log_block("=== summary ===", {
        "challenge": competition_id,
        "status": status_after,
        "evaluated": len(results),
        "skipped": len(rows) - len(results),
        "scores": ", ".join(f"{r['submission_id'][:8]}…={r['score']}" for r in results) or "none",
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
        default=Path(__file__).with_name("challenge_evaluation_config.json"),
        help="JSON config path (default: challenge_evaluation_config.json beside this script)",
    )
    parser.add_argument("--competition-id", help="the challenge to evaluate (overrides target.*)")
    parser.add_argument("--title", help="the challenge's exact title, looked up in the admin lists")
    parser.add_argument("--submission-id", action="append", metavar="UUID",
                        help="evaluate only this submission (repeatable)")
    parser.add_argument("--announce", action="store_true",
                        help="announce the result afterwards (challenge → COMPLETED)")
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
