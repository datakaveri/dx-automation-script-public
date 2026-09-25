#!/usr/bin/env python3
"""Move a challenge's dates into the past directly in the database, so the
evaluation flow can be tested today instead of tomorrow.

Usage:
    python challenge_db_backdate.py challenge_db_backdate_config.json
    python challenge_db_backdate.py challenge_db_backdate_config.json --competition-id <uuid>
    python challenge_db_backdate.py challenge_db_backdate_config.json --submission-end yesterday
    python challenge_db_backdate.py challenge_db_backdate_config.json --status EVALUATION
    python challenge_db_backdate.py challenge_db_backdate_config.json --dry-run

The server keeps the timeline as plain dates and gates two things on them:
the cron moves PUBLISHED → EVALUATION only when `submission_ends_at` is
before today, and announce-result refuses until then too. Through the API
nothing can shorten that wait. This script edits the rows instead:

    competition_timelines   submission_starts_at, submission_ends_at,
                            evaluation_ends_at               ← times.*
    competitions            published_at (and scheduled_publish_at cleared)
    competition_submissions created_at / updated_at          ← times.submissions_created_at

then either lets the server's cron (every minute) notice and move the
challenge to EVALUATION (`status.mode: "cron"`, waited for), or sets the
status itself (`"direct"`). After it, the evaluation script can score and
`--announce` the same day.

Every UPDATE is scoped to one competition id and runs in one transaction;
`--dry-run` runs the SELECTs and prints the UPDATEs without executing them.
The challenge is found like the other scripts find it: `--competition-id`,
`--title` (exact, in the database), `target.*`, or the creation handoff.

Everything the script touches — connection, schema, table and column
names, the dates — comes from the JSON config. Values may reference the
environment as ${VAR} or ${VAR:-fallback}.
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
    import psycopg2
    from psycopg2 import sql
except ModuleNotFoundError:  # Allows --help and config validation before install.
    psycopg2 = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]


LOG = logging.getLogger("challenge_db_backdate")

DEFAULT_CONFIG: dict[str, Any] = {
    # The community layer's challenge database — CHALLENGE_DATABASE_URL and
    # CHALLENGE_DB_SCHEMA in its deployment.
    "postgres": {
        "host": "",
        "port": 5432,
        "database": "",
        "schema": "tgdx_dev",
        "user": "",
        "password": "",
        "sslmode": "require",
        "connect_timeout_seconds": 10,
    },
    "target": {
        "competition_id": "",
        "title": "",
        "input_file": "../creation/challenge_created.json",
        "require_input_file": False,
        # Write the dates and status now in the database back into that file.
        "update_input_file": True,
    },
    # What to write. Each is an expression — today, yesterday, tomorrow, an
    # absolute date, another time's name, with +N/-N min/h/d/w offsets and an
    # optional HH:MM — or null to leave that column alone.
    "times": {
        "published_at": "yesterday",
        "submission_start": "yesterday",
        "submission_end": "yesterday",
        "evaluation_end": "today+7d",
        # created_at / updated_at of every submission on the challenge.
        "submissions_created_at": "yesterday",
    },
    # The cron republishes anything with a past scheduled_publish_at that is
    # not PUBLISHED, which would undo an EVALUATION status every minute.
    "clear_scheduled_publish_at": True,
    "status": {
        # "cron": leave the status to the server's cron and wait for it below.
        # "direct": set competitions.status to `value` here.
        # "none": touch only the dates.
        "mode": "cron",
        "value": "EVALUATION",
        # Only proceed when the challenge currently has one of these; empty
        # means any.
        "require_current": ["PUBLISHED"],
    },
    "wait": {
        "enabled": True,
        "timeout_seconds": 150,
        "poll_seconds": 10,
    },
    "time_format": {
        "timezone": "Asia/Kolkata",
        "date_formats": ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S%z",
                         "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"],
    },
    # Table and column names, in case a deployment differs.
    "tables": {
        "competitions": "competitions",
        "timelines": "competition_timelines",
        "submissions": "competition_submissions",
    },
    "columns": {
        "competition_id": "id",
        "title": "title",
        "status": "status",
        "published_at": "published_at",
        "scheduled_publish_at": "scheduled_publish_at",
        "updated_at": "updated_at",
        "timeline_competition_id": "competition_id",
        "submission_starts_at": "submission_starts_at",
        "submission_ends_at": "submission_ends_at",
        "evaluation_ends_at": "evaluation_ends_at",
        "submission_competition_id": "competition_id",
        "submission_created_at": "created_at",
        "submission_updated_at": "updated_at",
    },
    "logging": {
        "level": "INFO",
        "print_sql": True,
        "mask_secrets": False,
    },
}

TIME_COLUMNS = {
    # times key → (table key, column key, is datetime)
    "submission_start": ("timelines", "submission_starts_at", False),
    "submission_end": ("timelines", "submission_ends_at", False),
    "evaluation_end": ("timelines", "evaluation_ends_at", False),
    "published_at": ("competitions", "published_at", True),
    "submissions_created_at": ("submissions", "submission_created_at", True),
}


class ConfigError(ValueError):
    """A required configuration value is absent or invalid."""


class DbError(RuntimeError):
    """The database refused, or holds something other than expected."""


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


def require_psycopg2() -> None:
    if psycopg2 is None:
        raise ConfigError(
            "missing Python dependency 'psycopg2'; run "
            "'python -m pip install -r ../requirements.txt'"
        )


def zone(config: dict[str, Any]) -> ZoneInfo:
    name = str(config["time_format"].get("timezone") or "Asia/Kolkata")
    try:
        return ZoneInfo(name)
    except Exception as exc:
        raise ConfigError(f"'time_format.timezone' {name!r} is not a known timezone: {exc}") from exc


def table(config: dict[str, Any], key: str) -> str:
    return require_string(config["tables"].get(key), f"tables.{key}")


def column(config: dict[str, Any], key: str) -> str:
    return require_string(config["columns"].get(key), f"columns.{key}")


# ------------------------------------------------------------------- output

OUTPUT = {"print_sql": True, "mask_secrets": False}


def configure_output(config: dict[str, Any]) -> None:
    settings = config.get("logging") or {}
    OUTPUT["print_sql"] = bool(settings.get("print_sql", True))
    OUTPUT["mask_secrets"] = bool(settings.get("mask_secrets", False))


def log_block(title: str, fields: dict[str, Any]) -> None:
    LOG.info("%s", title)
    width = max((len(str(k)) for k in fields), default=0)
    for key, value in fields.items():
        if OUTPUT["mask_secrets"] and "password" in str(key).lower():
            value = "***"
        LOG.info("    %-*s  %s", width, key, "" if value is None else value)


def render(cursor: Any, statement: Any, params: Any) -> str:
    try:
        return " ".join(cursor.mogrify(statement, params).decode().split())
    except Exception:
        return str(statement)


def log_sql(cursor: Any, statement: Any, params: Any) -> None:
    if OUTPUT["print_sql"]:
        LOG.info("SQL: %s", render(cursor, statement, params))


# --------------------------------------------------------------------- time

_OFFSET = re.compile(r"([+-]\s*\d+)\s*(min|h|d|w)\b")
_CLOCK = re.compile(r"\s(\d{1,2}):(\d{2})\s*$")


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
            raise ConfigError(f"'{name}' is written relative to {base_text}, which is not set")
    else:
        value = parse_absolute(base_text, config, tz)
        if value is None:
            known = ", ".join(sorted(k for k, v in bases.items() if v is not None))
            raise ConfigError(
                f"'{name}': cannot read {base_text!r} as a date, a keyword "
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


def resolve_times(config: dict[str, Any], overrides: dict[str, Any]) -> dict[str, datetime | None]:
    """times.* resolved in order; a later one may name an earlier one."""
    times = config["times"]
    if not isinstance(times, dict):
        raise ConfigError("'times' must be an object")
    resolved: dict[str, datetime | None] = {}
    for key, (_, column_key, _) in TIME_COLUMNS.items():
        resolved[key] = resolved[column_key] = None
    for key, (_, column_key, _) in TIME_COLUMNS.items():
        spec = overrides.get(key) if overrides.get(key) is not None else times.get(key)
        value = resolve_expression(spec, f"times.{key}", resolved, config)
        resolved[key] = resolved[column_key] = value
    return resolved


def as_local(value: Any, config: dict[str, Any]) -> str | None:
    """A database timestamp shown in time_format.timezone."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(zone(config)).isoformat()
    return str(value)


# ----------------------------------------------------------------- handoff


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


def resolve_target(config: dict[str, Any], config_path: Path,
                   args: argparse.Namespace) -> tuple[str, str]:
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
        LOG.warning("%s describes %s (%s), not the challenge asked for — using only the "
                    "config and command line", input_file, recorded_title or "?", recorded_id or "?")
        record = {}
    competition_id = asked_id or recorded_id
    title = asked_title or recorded_title
    if not competition_id and not title:
        raise ConfigError(
            "no challenge to backdate: pass --competition-id or --title, set "
            "'target.competition_id' or 'target.title', or point 'target.input_file' "
            "at the file the creation script wrote"
        )
    return competition_id, title


def update_input_file(config: dict[str, Any], config_path: Path, competition_id: str,
                      after: dict[str, Any]) -> None:
    target = config["target"]
    if not target.get("update_input_file", True):
        return
    name = str(target.get("input_file") or "").strip()
    if not name:
        return
    path = resolve_path(name, config_path)
    record = read_json(path)
    if not record or str(record.get("competition_id") or "") != competition_id:
        return
    record["status"] = after.get("status") or record.get("status")
    record["timelines"] = {
        key: str(after.get(key)) if after.get(key) is not None else None
        for key in ("submission_starts_at", "submission_ends_at", "evaluation_ends_at")
    }
    record["published_at_local"] = as_local(after.get("published_at"), config)
    record["backdated_at"] = datetime.now(zone(config)).isoformat()
    write_json(path, record)


# ---------------------------------------------------------------- database


def connect(config: dict[str, Any]) -> Any:
    postgres = config["postgres"]
    return psycopg2.connect(
        host=require_string(postgres.get("host"), "postgres.host"),
        port=int(postgres.get("port") or 5432),
        dbname=require_string(postgres.get("database"), "postgres.database"),
        user=require_string(postgres.get("user"), "postgres.user"),
        password=str(postgres.get("password") or ""),
        sslmode=str(postgres.get("sslmode") or "require"),
        connect_timeout=int(postgres.get("connect_timeout_seconds") or 10),
    )


def ident(config: dict[str, Any], table_key: str) -> Any:
    return sql.Identifier(str(config["postgres"].get("schema") or "public"), table(config, table_key))


def read_challenge(config: dict[str, Any], cursor: Any, competition_id: str,
                   title: str) -> dict[str, Any]:
    """The competition row joined to its timeline, by id or exact title."""
    c = lambda key: sql.Identifier(column(config, key))  # noqa: E731
    where = sql.SQL("c.{} = %s").format(c("competition_id")) if competition_id \
        else sql.SQL("c.{} = %s").format(c("title"))
    statement = sql.SQL(
        "SELECT c.{id}, c.{title}, c.{status}, c.{published}, c.{scheduled}, "
        "t.{starts}, t.{ends}, t.{evaluation}, "
        "(SELECT count(*) FROM {subs} s WHERE s.{sub_cid} = c.{id}) "
        "FROM {competitions} c LEFT JOIN {timelines} t ON t.{tl_cid} = c.{id} WHERE "
    ).format(
        id=c("competition_id"), title=c("title"), status=c("status"),
        published=c("published_at"), scheduled=c("scheduled_publish_at"),
        starts=c("submission_starts_at"), ends=c("submission_ends_at"),
        evaluation=c("evaluation_ends_at"), subs=ident(config, "submissions"),
        sub_cid=c("submission_competition_id"), competitions=ident(config, "competitions"),
        timelines=ident(config, "timelines"), tl_cid=c("timeline_competition_id"),
    ) + where
    params = (competition_id or title,)
    log_sql(cursor, statement, params)
    cursor.execute(statement, params)
    rows = cursor.fetchall()
    if not rows:
        raise DbError(f"no challenge {competition_id or title!r} in "
                      f"{config['postgres'].get('schema')}.{table(config, 'competitions')}")
    if len(rows) > 1:
        raise DbError(f"{len(rows)} challenges titled {title!r}; pass --competition-id")
    row = rows[0]
    return {
        "competition_id": str(row[0]), "title": row[1], "status": row[2],
        "published_at": row[3], "scheduled_publish_at": row[4],
        "submission_starts_at": row[5], "submission_ends_at": row[6],
        "evaluation_ends_at": row[7], "submissions": int(row[8]),
        "has_timeline": row[5] is not None or row[6] is not None or row[7] is not None,
    }


def shown(row: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    return {k: (as_local(v, config) if k in ("published_at", "scheduled_publish_at") else v)
            for k, v in row.items() if k != "has_timeline"}


def planned_updates(config: dict[str, Any], resolved: dict[str, datetime | None],
                    before: dict[str, Any], direct_status: str) -> list[tuple[Any, tuple, str]]:
    """(statement, params, description) for every UPDATE this run makes."""
    c = lambda key: sql.Identifier(column(config, key))  # noqa: E731
    competition_id = before["competition_id"]
    now = datetime.now(zone(config))
    plan: list[tuple[Any, tuple, str]] = []

    timeline_sets, timeline_params = [], []
    for key in ("submission_start", "submission_end", "evaluation_end"):
        value = resolved.get(key)
        if value is not None:
            timeline_sets.append(sql.SQL("{} = %s").format(c(TIME_COLUMNS[key][1])))
            timeline_params.append(value.date())
    if timeline_sets:
        plan.append((
            sql.SQL("UPDATE {} SET {} WHERE {} = %s").format(
                ident(config, "timelines"), sql.SQL(", ").join(timeline_sets),
                c("timeline_competition_id")),
            tuple(timeline_params) + (competition_id,),
            "timeline dates",
        ))

    comp_sets, comp_params = [], []
    if resolved.get("published_at") is not None:
        comp_sets.append(sql.SQL("{} = %s").format(c("published_at")))
        comp_params.append(resolved["published_at"])
    if config.get("clear_scheduled_publish_at", True) and before.get("scheduled_publish_at") is not None:
        comp_sets.append(sql.SQL("{} = NULL").format(c("scheduled_publish_at")))
    if direct_status:
        comp_sets.append(sql.SQL("{} = %s").format(c("status")))
        comp_params.append(direct_status)
    if comp_sets:
        comp_sets.append(sql.SQL("{} = %s").format(c("updated_at")))
        comp_params.append(now)
        plan.append((
            sql.SQL("UPDATE {} SET {} WHERE {} = %s").format(
                ident(config, "competitions"), sql.SQL(", ").join(comp_sets), c("competition_id")),
            tuple(comp_params) + (competition_id,),
            "competition row",
        ))

    if resolved.get("submissions_created_at") is not None and before["submissions"]:
        plan.append((
            sql.SQL("UPDATE {} SET {} = %s, {} = %s WHERE {} = %s").format(
                ident(config, "submissions"), c("submission_created_at"),
                c("submission_updated_at"), c("submission_competition_id")),
            (resolved["submissions_created_at"], resolved["submissions_created_at"], competition_id),
            f"{before['submissions']} submission(s) created_at",
        ))
    return plan


def wait_for_status(config: dict[str, Any], connection: Any, competition_id: str,
                    wanted: str) -> dict[str, Any]:
    wait = config["wait"]
    timeout = float(wait.get("timeout_seconds") or 150)
    poll = max(float(wait.get("poll_seconds") or 10), 1)
    deadline = time.monotonic() + timeout
    LOG.info("Waiting up to %.0fs for the server's cron to set status %s …", timeout, wanted)
    while True:
        with connection.cursor() as cursor:
            after = read_challenge(config, cursor, competition_id, "")
        connection.rollback()  # release the read snapshot
        if str(after["status"]).upper() == wanted.upper():
            LOG.info("Status is now %s", after["status"])
            return after
        if time.monotonic() >= deadline:
            raise DbError(
                f"status is still {after['status']} after {timeout:.0f}s — is the cron "
                f"running on this deployment? Set status.mode to \"direct\" to set it here"
            )
        time.sleep(poll)


# ---------------------------------------------------------------------- run


def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(
        str(args.log_level or config["logging"].get("level") or "INFO").upper()
    )
    configure_output(config)

    competition_id, title = resolve_target(config, config_path, args)
    overrides = {
        "published_at": args.published_at,
        "submission_start": args.submission_start,
        "submission_end": args.submission_end,
        "evaluation_end": args.evaluation_end,
        "submissions_created_at": args.submissions_created_at,
    }
    resolved = resolve_times(config, overrides)

    status = config["status"]
    mode = str(args.status_mode or status.get("mode") or "cron").lower()
    if args.status:
        mode = "direct"
    wanted = str(args.status or status.get("value") or "EVALUATION").strip().upper()
    if mode not in ("cron", "direct", "none"):
        raise ConfigError("'status.mode' must be \"cron\", \"direct\" or \"none\"")
    direct_status = wanted if mode == "direct" else ""

    log_block("=== backdate ===", {
        "challenge": competition_id or f"(by title) {title}",
        **{key: ((value.isoformat() if TIME_COLUMNS[key][2] else value.date().isoformat())
                 if value else "(unchanged)")
           for key, value in resolved.items() if key in TIME_COLUMNS},
        "status": mode + (f" → {wanted}" if mode != "none" else ""),
        "database": f"{config['postgres'].get('host')}/{config['postgres'].get('database')} "
                    f"schema {config['postgres'].get('schema')}",
    })

    require_psycopg2()
    connection = connect(config)
    try:
        with connection.cursor() as cursor:
            before = read_challenge(config, cursor, competition_id, title)
            competition_id = before["competition_id"]
            log_block("=== before ===", shown(before, config))
            required = [str(s).upper() for s in status.get("require_current") or []]
            if required and str(before["status"]).upper() not in required:
                raise DbError(f"the challenge is {before['status']}; status.require_current "
                              f"allows only {', '.join(required)}")
            if not before["has_timeline"] and any(
                resolved.get(k) for k in ("submission_start", "submission_end", "evaluation_end")
            ):
                raise DbError("the challenge has no timeline row to update")

            plan = planned_updates(config, resolved, before, direct_status)
            if not plan:
                LOG.warning("Nothing to change")
            if args.dry_run:
                LOG.info("Dry run — no changes will be made")
                for statement, params, what in plan:
                    LOG.info("Would run (%s): %s", what, render(cursor, statement, params))
                connection.rollback()
                return 0

            for statement, params, what in plan:
                log_sql(cursor, statement, params)
                cursor.execute(statement, params)
                LOG.info("Updated %s (%d row(s))", what, cursor.rowcount)
                if cursor.rowcount == 0:
                    raise DbError(f"UPDATE for {what} matched no rows — rolling back")
        connection.commit()
        LOG.info("Committed")

        with connection.cursor() as cursor:
            after = read_challenge(config, cursor, competition_id, "")
        connection.rollback()
        if mode == "cron" and config["wait"].get("enabled", True) \
                and str(after["status"]).upper() != wanted:
            after = wait_for_status(config, connection, competition_id, wanted)
    finally:
        connection.close()

    log_block("=== after ===", shown(after, config))
    update_input_file(config, config_path, competition_id, after)
    if mode == "cron" and not config["wait"].get("enabled", True):
        LOG.info("The cron runs every minute; the status should be %s shortly", wanted)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config", nargs="?", type=Path,
        default=Path(__file__).with_name("challenge_db_backdate_config.json"),
        help="JSON config path (default: challenge_db_backdate_config.json beside this script)",
    )
    parser.add_argument("--competition-id", help="the challenge to backdate (overrides target.*)")
    parser.add_argument("--title", help="the challenge's exact title, looked up in the database")
    parser.add_argument("--published-at", metavar="EXPR", help="override times.published_at")
    parser.add_argument("--submission-start", metavar="EXPR", help="override times.submission_start")
    parser.add_argument("--submission-end", metavar="EXPR", help="override times.submission_end")
    parser.add_argument("--evaluation-end", metavar="EXPR", help="override times.evaluation_end")
    parser.add_argument("--submissions-created-at", metavar="EXPR",
                        help="override times.submissions_created_at")
    parser.add_argument("--status", metavar="STATUS",
                        help="set competitions.status directly to this (implies status.mode direct)")
    parser.add_argument("--status-mode", choices=["cron", "direct", "none"],
                        help="override status.mode")
    parser.add_argument("--dry-run", action="store_true",
                        help="read the rows and print the UPDATEs, change nothing")
    parser.add_argument("--log-level", help="override logging.level from the config")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        return run(args.config, args)
    except (ConfigError, DbError, OSError) as exc:
        LOG.error("%s", exc)
        return 1
    except Exception as exc:  # psycopg2 errors carry the server's message
        if psycopg2 is not None and isinstance(exc, psycopg2.Error):
            LOG.error("database: %s", str(exc).strip())
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
