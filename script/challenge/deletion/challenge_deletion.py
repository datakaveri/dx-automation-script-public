#!/usr/bin/env python3
"""Delete one challenge (competition) from dx-community-layer as the COS admin.

Usage:
    python challenge_deletion.py challenge_deletion_config.json
    python challenge_deletion.py challenge_deletion_config.json --competition-id <uuid>
    python challenge_deletion.py challenge_deletion_config.json --title "e2e-challenge-…"
    python challenge_deletion.py challenge_deletion_config.json --dry-run

The call is `DELETE /challenge/admin/challenges/{id}`. The server refuses it
for a PUBLISHED challenge outright, and for any challenge the caller did not
create — so this must run as the same COS admin the creation script used. It
is a hard delete: the competition row goes, and its timeline, prize pool,
evaluation and dataset rows cascade with it.

The script reads the challenge first and shows what it is about to remove,
refuses anything outside `delete.allowed_statuses`, deletes, and then checks
the id is gone. Which challenge: `--competition-id`, `target.competition_id`,
`target.title`, or the handoff file the creation script wrote
(`target.input_file`), which is removed once the challenge is.

Everything the script touches — URLs, credentials, endpoint paths — comes from
the JSON config. Values may reference the environment as ${VAR} or
${VAR:-fallback}.
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


LOG = logging.getLogger("challenge_deletion")

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
    # Who deletes the challenge — it must be whoever created it. A ready-made
    # token wins over credentials.
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
        # Remove that file once the challenge it describes is gone.
        "remove_input_file": True,
    },
    "delete": {
        # Statuses this script is willing to delete. The server itself refuses
        # only PUBLISHED, so EVALUATION, COMPLETED and CANCELLED can be added
        # here when that is really wanted. Empty means whatever the server allows.
        "allowed_statuses": ["DRAFT", "SCHEDULED"],
        "expect_http_status": [200],
        # Read the id back afterwards and expect one of these.
        "verify_after": True,
        "expect_gone_status": [404],
        "settle_seconds": 0,
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
    "endpoints": {
        "kc_token": "/realms/{realm}/protocol/openid-connect/token",
        "admin_get": "/challenge/admin/challenge/{competition_id}",
        "admin_delete": "/challenge/admin/challenges/{competition_id}",
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


def consume_input_file(config: dict[str, Any], config_path: Path, competition_id: str) -> None:
    """Remove the handoff file once the challenge it described is gone.

    Only when it described *this* challenge: deleting one named on the command
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
    recorded = str(read_json(path).get("competition_id") or "").strip()
    if recorded and recorded != competition_id:
        LOG.info("Leaving %s in place — it describes %s, which is still there", path, recorded)
        return
    path.unlink()
    LOG.info("Removed %s", path)


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
    """Which challenge to delete: (competition_id, title) — the id may be blank
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
            "no challenge to delete: pass --competition-id or --title, set "
            "'target.competition_id' or 'target.title', or point 'target.input_file' "
            "at the file the creation script wrote"
        )
    return competition_id, title


# ---------------------------------------------------------------------- run


def read_challenge(config: dict[str, Any], api: Community, token: str, competition_id: str) -> dict[str, Any] | None:
    """The challenge as the admin sees it, or None when the server has no such id."""
    path = api_path(config, "admin_get", competition_id=competition_id)
    try:
        return data_of(api.request("GET", path, "read challenge", token, expect=(200, 404)))
    except ApiError as exc:
        if "NOT_FOUND" in str(exc) or "not found" in str(exc).lower():
            return None
        raise


def delete_challenge(
    config: dict[str, Any], api: Community, token: str, competition_id: str
) -> None:
    delete = config["delete"]
    expect = tuple(int(code) for code in delete.get("expect_http_status") or (200,))
    api.request(
        "DELETE", api_path(config, "admin_delete", competition_id=competition_id),
        "delete challenge", token, expect=expect,
    )
    LOG.info("Deleted challenge %s", competition_id)

    settle = float(delete.get("settle_seconds") or 0)
    if settle > 0:
        time.sleep(settle)
    if not delete.get("verify_after", True):
        return
    gone = tuple(int(code) for code in delete.get("expect_gone_status") or (404,))
    path = api_path(config, "admin_get", competition_id=competition_id)
    label = "confirm challenge is gone"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    log_request(label, "GET", path)
    response = api.session.get(f"{api.base_url}/{path.lstrip('/')}", headers=headers, timeout=api.timeout)
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    log_response(label, "GET", path, response.status_code, payload)
    if response.status_code not in gone:
        raise ApiError(
            f"challenge {competition_id} still answers {response.status_code} after deletion, "
            f"expected {' or '.join(str(code) for code in gone)}"
        )


def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(
        str(args.log_level or config["logging"].get("level") or "INFO").upper()
    )
    configure_output(config)

    competition_id, title = resolve_target(config, config_path, args)
    allowed = [str(s).upper() for s in config["delete"].get("allowed_statuses") or []]

    log_block("=== challenge to delete ===", {
        "competition_id": competition_id or "(look up by title)",
        "title": title,
        "community": config["community"].get("base_url"),
        "cos_admin": config["cos_admin"].get("username") or "(token)",
        "allowed_statuses": ", ".join(allowed) or "whatever the server allows",
    })

    if args.dry_run:
        LOG.info("Dry run — no calls will be made")
        LOG.info("Would DELETE %s",
                 api_path(config, "admin_delete", competition_id=competition_id or "<id>"))
        return 0

    require_requests()
    token = actor_token(config, config["cos_admin"], "cos_admin")
    api = Community(config["community"])
    if not competition_id:
        competition_id = find_by_title(config, api, token, title)
        LOG.info("Challenge %r is %s", title, competition_id)

    detail = read_challenge(config, api, token, competition_id)
    if detail is None:
        LOG.info("Challenge %s is already gone", competition_id)
        consume_input_file(config, config_path, competition_id)
        return 0

    status = str(detail.get("status") or "")
    timelines = detail.get("timelines") if isinstance(detail.get("timelines"), dict) else {}
    creator = detail.get("creator") if isinstance(detail.get("creator"), dict) else {}
    log_block("=== found ===", {
        "competition_id": competition_id,
        "title": detail.get("title"),
        "status": status,
        "created_by": creator.get("email") or creator.get("name") or creator.get("id"),
        **{name: timelines.get(name) for name in DATE_FIELDS},
        "participants": detail.get("participant_count"),
        "submissions": detail.get("submission_count"),
    })
    if status.upper() == "PUBLISHED":
        raise ApiError(
            f"challenge {competition_id} is PUBLISHED; the server does not allow "
            "deleting a published challenge"
        )
    if allowed and status.upper() not in allowed:
        raise ApiError(
            f"challenge {competition_id} is {status}; delete.allowed_statuses permits only "
            f"{', '.join(allowed)}"
        )

    delete_challenge(config, api, token, competition_id)
    consume_input_file(config, config_path, competition_id)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("challenge_deletion_config.json"),
        help="JSON config path (default: challenge_deletion_config.json beside this script)",
    )
    parser.add_argument("--competition-id", help="the challenge to delete")
    parser.add_argument("--title", help="the challenge to delete, looked up by exact title")
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
