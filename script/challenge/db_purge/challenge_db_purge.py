#!/usr/bin/env python3
"""Delete one challenge and everything hanging off it straight from the
database, plus the consumer accounts the submission script created.

Usage:
    python challenge_db_purge.py challenge_db_purge_config.json
    python challenge_db_purge.py challenge_db_purge_config.json --competition-id <uuid>
    python challenge_db_purge.py challenge_db_purge_config.json --title "e2e-dev-challenge-…"
    python challenge_db_purge.py challenge_db_purge_config.json --dry-run

The API deletes only DRAFT and SCHEDULED challenges. A PUBLISHED, EVALUATION
or COMPLETED one — which is what a full submit-and-score run leaves behind —
can only be removed here. In one transaction, scoped to that one id:

  1. every table in the schema with a `competition_id` column (found in
     information_schema, so it survives schema drift): submissions,
     participants, timeline, prize pool, evaluation criteria, datasets,
     bookmarks — `DELETE … WHERE competition_id = <id>`
  2. the `competitions` row itself
  3. the community layer's own `users` rows (and any `user_id` rows) for the
     accounts flagged `created_user` in the submission handoff — never the
     accounts you listed yourself
  4. a count of every touched table afterwards, which must be zero

A `purge.require_title_prefix` guard refuses any challenge whose title does
not start with it, so a typo in an id cannot take a real challenge with it.
`--dry-run` runs only the counts and prints the DELETEs.

Not covered: the S3 objects under `private/<user>/<competition>/…` — this
script has no bucket credentials — and the Keycloak accounts, which
`submission_cleanup` removes.

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
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import psycopg2
    from psycopg2 import sql
except ModuleNotFoundError:  # Allows --help and config validation before install.
    psycopg2 = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]


LOG = logging.getLogger("challenge_db_purge")

DEFAULT_CONFIG: dict[str, Any] = {
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
        "submissions_file": "../submission/submissions_created.json",
        "require_input_file": False,
    },
    "purge": {
        # Refuse a challenge whose title does not start with this; blank
        # disables the guard.
        "require_title_prefix": "e2e-",
        # Only proceed when the challenge has one of these statuses; empty
        # means any.
        "allowed_statuses": [],
        "challenge": True,
        # "created": the accounts flagged created_user in submissions_file.
        # "none": leave every users row.
        "users": "created",
        # Read every touched table back and insist on zero rows.
        "verify_after": True,
        # Handoff files to remove once the rows are gone. Relative to the
        # config file.
        "remove_files": [
            "../creation/challenge_created.json",
            "../submission/submissions_created.json",
            "../evaluation/submissions_evaluated.json",
        ],
    },
    "tables": {
        "competitions": "competitions",
        "users": "users",
        # Tables never to touch even if they carry the column.
        "skip": [],
    },
    "columns": {
        "competition_id": "id",
        "title": "title",
        "status": "status",
        "child_competition_id": "competition_id",
        "user_id": "id",
        "child_user_id": "user_id",
        "user_email": "email",
    },
    "time_format": {
        "timezone": "Asia/Kolkata",
    },
    "logging": {
        "level": "INFO",
        "print_sql": True,
        "mask_secrets": False,
    },
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


def resolve_target(config: dict[str, Any], config_path: Path,
                   args: argparse.Namespace) -> tuple[str, str, list[dict[str, Any]]]:
    """(competition_id, title, accounts this run may delete)."""
    target = config["target"]
    creation: dict[str, Any] = {}
    submissions: dict[str, Any] = {}
    for key in ("input_file", "submissions_file"):
        name = str(target.get(key) or "").strip()
        if not name:
            continue
        path = resolve_path(name, config_path)
        record = read_json(path)
        if record:
            LOG.info("Read %s", path)
        elif target.get("require_input_file", False):
            raise ConfigError(f"target.{key} {path} is missing or unreadable")
        if key == "input_file":
            creation = record
        else:
            submissions = record

    asked_id = (args.competition_id or str(target.get("competition_id") or "")).strip()
    asked_title = (args.title or str(target.get("title") or "")).strip()
    competition_id, title = asked_id, asked_title
    for record in (creation, submissions):
        if not record:
            continue
        recorded_id = str(record.get("competition_id") or "").strip()
        recorded_title = str(record.get("title") or "").strip()
        if (asked_id and recorded_id and asked_id != recorded_id) or (
            asked_title and recorded_title and asked_title != recorded_title and not asked_id
        ):
            LOG.warning("A handoff file describes %s (%s), not the challenge asked for — ignoring it",
                        recorded_title or "?", recorded_id or "?")
            if record is submissions:
                submissions = {}
            continue
        competition_id = competition_id or recorded_id
        title = title or recorded_title
    if not competition_id and not title:
        raise ConfigError(
            "no challenge to purge: pass --competition-id or --title, set "
            "'target.competition_id' or 'target.title', or point 'target.input_file' "
            "at the file the creation script wrote"
        )

    accounts: list[dict[str, Any]] = []
    if str(config["purge"].get("users") or "created").lower() == "created":
        for entry in submissions.get("submissions") or []:
            if isinstance(entry, dict) and entry.get("created_user") and entry.get("keycloak_user_id"):
                accounts.append(entry)
    return competition_id, title, accounts


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


def schema(config: dict[str, Any]) -> str:
    return str(config["postgres"].get("schema") or "public")


def ident(config: dict[str, Any], table_name: str) -> Any:
    return sql.Identifier(schema(config), table_name)


def read_challenge(config: dict[str, Any], cursor: Any, competition_id: str,
                   title: str) -> dict[str, Any]:
    c = lambda key: sql.Identifier(column(config, key))  # noqa: E731
    where = sql.SQL("{} = %s").format(c("competition_id")) if competition_id \
        else sql.SQL("{} = %s").format(c("title"))
    statement = sql.SQL("SELECT {}, {}, {} FROM {} WHERE ").format(
        c("competition_id"), c("title"), c("status"),
        ident(config, str(config["tables"].get("competitions") or "competitions")),
    ) + where
    params = (competition_id or title,)
    log_sql(cursor, statement, params)
    cursor.execute(statement, params)
    rows = cursor.fetchall()
    if not rows:
        return {}
    if len(rows) > 1:
        raise DbError(f"{len(rows)} challenges titled {title!r}; pass --competition-id")
    return {"competition_id": str(rows[0][0]), "title": rows[0][1], "status": rows[0][2]}


def tables_with_column(config: dict[str, Any], cursor: Any, column_name: str) -> list[str]:
    """Every table in the schema carrying this column, minus tables.skip."""
    statement = (
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = %s AND column_name = %s ORDER BY table_name"
    )
    log_sql(cursor, statement, (schema(config), column_name))
    cursor.execute(statement, (schema(config), column_name))
    skip = {str(name) for name in config["tables"].get("skip") or []}
    return [row[0] for row in cursor.fetchall() if row[0] not in skip]


def count_rows(config: dict[str, Any], cursor: Any, table_name: str, column_name: str,
               values: list[str]) -> int:
    statement = sql.SQL("SELECT count(*) FROM {} WHERE {} = ANY(%s::uuid[])").format(
        ident(config, table_name), sql.Identifier(column_name))
    cursor.execute(statement, (values,))
    return int(cursor.fetchone()[0])


def delete_rows(config: dict[str, Any], cursor: Any, table_name: str, column_name: str,
                values: list[str], dry_run: bool) -> int:
    statement = sql.SQL("DELETE FROM {} WHERE {} = ANY(%s::uuid[])").format(
        ident(config, table_name), sql.Identifier(column_name))
    if dry_run:
        LOG.info("Would run: %s", render(cursor, statement, (values,)))
        return 0
    log_sql(cursor, statement, (values,))
    cursor.execute(statement, (values,))
    return cursor.rowcount


def user_emails(config: dict[str, Any], cursor: Any, user_ids: list[str]) -> dict[str, str]:
    users = str(config["tables"].get("users") or "users")
    statement = sql.SQL("SELECT {}, {} FROM {} WHERE {} = ANY(%s::uuid[])").format(
        sql.Identifier(column(config, "user_id")), sql.Identifier(column(config, "user_email")),
        ident(config, users), sql.Identifier(column(config, "user_id")))
    cursor.execute(statement, (user_ids,))
    return {str(row[0]): str(row[1]) for row in cursor.fetchall()}


# ---------------------------------------------------------------------- run


def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(
        str(args.log_level or config["logging"].get("level") or "INFO").upper()
    )
    configure_output(config)
    purge = config["purge"]

    competition_id, title, accounts = resolve_target(config, config_path, args)
    competitions_table = str(config["tables"].get("competitions") or "competitions")
    users_table = str(config["tables"].get("users") or "users")
    user_ids = [str(a["keycloak_user_id"]) for a in accounts]

    log_block("=== purge ===", {
        "challenge": competition_id or f"(by title) {title}",
        "title": title or None,
        "delete challenge": purge.get("challenge", True),
        "delete users": ", ".join(str(a.get("username") or a["keycloak_user_id"]) for a in accounts)
                        or "none",
        "title guard": purge.get("require_title_prefix") or "(off)",
        "database": f"{config['postgres'].get('host')}/{config['postgres'].get('database')} "
                    f"schema {schema(config)}",
    })

    require_psycopg2()
    connection = connect(config)
    touched: list[tuple[str, str, list[str]]] = []  # (table, column, ids)
    try:
        with connection.cursor() as cursor:
            challenge = read_challenge(config, cursor, competition_id, title)
            if challenge:
                competition_id = challenge["competition_id"]
                title = str(challenge["title"] or "")
                prefix = str(purge.get("require_title_prefix") or "")
                if prefix and not title.startswith(prefix):
                    raise DbError(
                        f"refusing: title {title!r} does not start with "
                        f"purge.require_title_prefix {prefix!r}"
                    )
                allowed = [str(s).upper() for s in purge.get("allowed_statuses") or []]
                if allowed and str(challenge["status"]).upper() not in allowed:
                    raise DbError(f"refusing: the challenge is {challenge['status']}; "
                                  f"purge.allowed_statuses allows only {', '.join(allowed)}")
                log_block("=== challenge ===", challenge)
            else:
                LOG.info("No challenge %s in the database — already gone", competition_id or title)

            # What is there now, table by table.
            if challenge and purge.get("challenge", True):
                for table_name in tables_with_column(config, cursor, column(config, "child_competition_id")):
                    if table_name == competitions_table:
                        continue
                    touched.append((table_name, column(config, "child_competition_id"), [competition_id]))
                touched.append((competitions_table, column(config, "competition_id"), [competition_id]))
            if user_ids:
                present = user_emails(config, cursor, user_ids)
                missing = [u for u in user_ids if u not in present]
                if missing:
                    LOG.info("%d account(s) have no users row (never called the API?): %s",
                             len(missing), ", ".join(missing))
                for table_name in tables_with_column(config, cursor, column(config, "child_user_id")):
                    if table_name == users_table:
                        continue
                    touched.append((table_name, column(config, "child_user_id"), user_ids))
                touched.append((users_table, column(config, "user_id"), user_ids))

            counts = {f"{t}.{c}": count_rows(config, cursor, t, c, ids) for t, c, ids in touched}
            log_block("=== rows found ===", counts or {"(nothing)": ""})

            if args.dry_run:
                LOG.info("Dry run — no changes will be made")
                for table_name, column_name, ids in touched:
                    delete_rows(config, cursor, table_name, column_name, ids, dry_run=True)
                connection.rollback()
                return 0

            deleted: dict[str, int] = {}
            for table_name, column_name, ids in touched:
                deleted[f"{table_name}.{column_name}"] = delete_rows(
                    config, cursor, table_name, column_name, ids, dry_run=False)

            if purge.get("verify_after", True):
                left = {key: count_rows(config, cursor, t, c, ids)
                        for (t, c, ids), key in zip(touched, deleted)}
                survivors = {k: v for k, v in left.items() if v}
                if survivors:
                    raise DbError(f"rows survived the delete, rolling back: {survivors}")
        connection.commit()
        LOG.info("Committed")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    for name in purge.get("remove_files") or []:
        path = resolve_path(str(name), config_path)
        if path.is_file():
            record = read_json(path)
            recorded = str(record.get("competition_id") or "")
            if recorded and competition_id and recorded != competition_id:
                LOG.info("Leaving %s alone — it describes %s", path, recorded)
                continue
            path.unlink()
            LOG.info("Removed %s", path)

    log_block("=== summary ===", {
        "challenge": competition_id or title,
        "deleted": ", ".join(f"{k}={v}" for k, v in deleted.items()) or "nothing",
        "verified": purge.get("verify_after", True),
        "purged_at": datetime.now(zone(config)).isoformat(),
    })
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config", nargs="?", type=Path,
        default=Path(__file__).with_name("challenge_db_purge_config.json"),
        help="JSON config path (default: challenge_db_purge_config.json beside this script)",
    )
    parser.add_argument("--competition-id", help="the challenge to purge (overrides target.*)")
    parser.add_argument("--title", help="the challenge's exact title, looked up in the database")
    parser.add_argument("--dry-run", action="store_true",
                        help="count the rows and print the DELETEs, change nothing")
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
