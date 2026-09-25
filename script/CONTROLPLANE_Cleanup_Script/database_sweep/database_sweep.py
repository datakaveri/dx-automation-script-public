#!/usr/bin/env python3
"""Hard-delete a run's rows from the ControlPlane database — the final sweep.

Usage:
    python database_sweep.py                            # database_sweep_config.json beside this script
    python database_sweep.py my_config.json
    python database_sweep.py --dry-run                  # run every DELETE, print the counts, roll back
    python database_sweep.py --verify-only              # count what is left under the prefix, delete nothing

Every API on the platform stops short somewhere: policies and access requests
are only soft-deleted, an approved organisation cannot be deleted while its
admin exists, the leaderboards and credit rows are never deleted at all. This
is what removes them — the same statements the harness runs at the end of a
teardown, in children-before-parents order.

Rows are found three ways, all of which must agree with the run being cleaned:

    target.prefix      the namespaced columns — emails, organisation names,
                       asset names — matched with LIKE '<prefix>%'
    target.user_ids    Keycloak user ids (= user_table._id). Resolved from the
                       prefix and from Keycloak when keycloak.* is filled in;
                       pasted here otherwise. Needed for tables with no
                       namespaced column of their own, which the prefix alone
                       cannot reach once user_table is swept.
    target.org_ids / item_ids   likewise, for the run's organisation and item

Resolve or paste the ids BEFORE deleting the Keycloak accounts: once they are
gone nothing can look them up, and an empty id list makes every id-keyed
DELETE a silent no-op rather than an error.

The SQL is not copied here. It is imported from
script/ControlPlane_Workflow/cleanup.py, so a table added or a column renamed
is fixed in one place for the harness and for this script alike. That module
also adapts each statement to the live schema — a missing table is skipped, a
missing column dropped from its OR-group, and a statement whose anchor would
vanish is skipped whole rather than widened.

Exit codes:
    0  swept (and verified clean, when sweep.verify is on)
    1  a statement failed, or rows survived
    2  configuration error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

# The statements live with the harness; see the module docstring.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ControlPlane_Workflow.cleanup import (  # noqa: E402
    ANCHOR_QUERIES,
    AUDIT_SWEEP_STATEMENTS,
    COS_ADMIN_LOG_ALL,
    COS_ADMIN_LOG_SCOPED,
    COS_ADMIN_ROWS_ALL,
    COS_ADMIN_ROWS_SCOPED,
    SWEEP_STATEMENTS,
    VERIFY_STATEMENTS,
    render_sql,
    _adapt_to_schema,
    _schema_columns,
)

try:
    import psycopg2
except ModuleNotFoundError:  # allows --help before install
    psycopg2 = None  # type: ignore[assignment]

try:
    import requests
except ModuleNotFoundError:
    requests = None  # type: ignore[assignment]

LOG = logging.getLogger("database_sweep")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

DEFAULT_CONFIG: dict[str, Any] = {
    "postgres": {
        "host": "",
        "port": 5432,
        "database": "",
        "schema": "aaa",
        "user": "",
        "password": "",
        "sslmode": "prefer",
        "connect_timeout_seconds": 10,
    },
    # Optional. When filled in, the prefix's users are looked up in Keycloak and
    # their ids added to target.user_ids — the way the harness does it.
    "keycloak": {
        "url": "",
        "realm": "",
        "admin_client_id": "",
        "admin_client_secret": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    "target": {
        "prefix": "",
        "user_ids": [],
        "org_ids": [],
        "item_ids": [],
        # Accounts whose rows are never swept: their ids are kept out of user_ids
        # even when Keycloak returns them under the prefix.
        "protected_user_ids": [],
        # A borrowed platform administrator (cos_admin) that approved this run's
        # requests. Its account is never touched; how much of its history goes
        # is sweep.delete_all_cos_admin_data.
        "cos_admin_id": "",
    },
    "sweep": {
        # Also sweep the four audit log tables for these users.
        "delete_audit_rows": False,
        # With a cos_admin_id: every row it owns rather than this run's only.
        "delete_all_cos_admin_data": False,
        # Re-count under the prefix afterwards and fail if anything is left.
        "verify": True,
    },
    "logging": {
        "level": "INFO",
    },
}


class ConfigError(ValueError):
    """A required configuration value is absent or invalid."""


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


def require(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} is required")
    return value.strip()


def validate(config: dict[str, Any]) -> None:
    pg = config["postgres"]
    for key in ("host", "database", "user", "password", "schema"):
        require(pg.get(key), f"postgres.{key}")
    prefix = require(config["target"].get("prefix"), "target.prefix")
    if len(prefix) < 3:
        # 'e%' would match most of a shared database. Refuse rather than ask.
        raise ConfigError(f"target.prefix {prefix!r} is too short to sweep on safely")
    if "%" in prefix:
        raise ConfigError("target.prefix must be a literal prefix — the LIKE wildcard is added by the script")
    if psycopg2 is None:
        raise ConfigError("psycopg2 is not installed: pip install -r ../requirements.txt")
    kc = config["keycloak"]
    if kc.get("url") and requests is None:
        raise ConfigError("the 'requests' package is not installed: pip install -r ../requirements.txt")


# ------------------------------------------------------------------- keycloak

def keycloak_user_ids(config: dict[str, Any], prefix: str) -> list[str]:
    """Ids of every Keycloak user under the prefix, or [] when Keycloak is not configured."""
    kc = config["keycloak"]
    if not (kc.get("url") and kc.get("realm") and kc.get("admin_client_id")):
        return []
    base = kc["url"].rstrip("/")
    session = requests.Session()
    session.verify = kc["verify_tls"]
    token = session.post(
        f"{base}/realms/{kc['realm']}/protocol/openid-connect/token",
        data={
            "grant_type": "client_credentials",
            "client_id": kc["admin_client_id"],
            "client_secret": kc["admin_client_secret"],
        },
        timeout=kc["timeout_seconds"],
    )
    if token.status_code != 200:
        raise RuntimeError(f"keycloak admin token: {token.status_code} {token.text[:300]}")
    users = session.get(
        f"{base}/admin/realms/{kc['realm']}/users",
        params={"username": prefix, "max": 500},
        headers={"Authorization": f"Bearer {token.json()['access_token']}"},
        timeout=kc["timeout_seconds"],
    )
    if users.status_code != 200:
        raise RuntimeError(f"keycloak list users: {users.status_code} {users.text[:300]}")
    found = [u["id"] for u in users.json() if str(u.get("username", "")).startswith(prefix)]
    LOG.info("keycloak: %d user(s) under %s*", len(found), prefix)
    return found


# ---------------------------------------------------------------------- sweep

def connect(config: dict[str, Any]):
    pg = config["postgres"]
    return psycopg2.connect(
        host=pg["host"], port=int(pg["port"]), dbname=pg["database"], user=pg["user"],
        password=pg["password"], sslmode=pg["sslmode"],
        connect_timeout=int(pg["connect_timeout_seconds"]),
    )


def resolve_anchors(cursor, schema: str, pattern: str, config: dict[str, Any]) -> dict[str, Any]:
    """The ids the sweep may touch: the prefix queries, plus what the config names."""
    target = config["target"]
    anchors: dict[str, Any] = {"pattern": pattern}
    for key, template in ANCHOR_QUERIES.items():
        try:
            cursor.execute(template.format(schema=schema), {"pattern": pattern})
            anchors[key] = [str(row[0]) for row in cursor.fetchall()]
        except Exception:  # noqa: BLE001 - a missing table must not stop the sweep
            cursor.connection.rollback()
            anchors[key] = []

    extra = {
        "user_ids": list(target.get("user_ids") or []) + keycloak_user_ids(config, target["prefix"]),
        "org_ids": list(target.get("org_ids") or []),
        "item_ids": list(target.get("item_ids") or []),
    }
    for key, values in extra.items():
        for value in values:
            if value and str(value) not in anchors[key]:
                anchors[key].append(str(value))

    protected = {str(i).lower() for i in target.get("protected_user_ids") or [] if i}
    cos_admin = str(target.get("cos_admin_id") or "").strip()
    if cos_admin:
        protected.add(cos_admin.lower())
    anchors["user_ids"] = [i for i in anchors["user_ids"] if i.lower() not in protected]
    anchors["cos_admin_ids"] = [cos_admin] if cos_admin else []
    return anchors


def statements_for(config: dict[str, Any]) -> list[tuple[str, str]]:
    sweep = config["sweep"]
    statements = list(SWEEP_STATEMENTS)
    if sweep["delete_audit_rows"]:
        leading = list(AUDIT_SWEEP_STATEMENTS)
        if config["target"].get("cos_admin_id"):
            if sweep["delete_all_cos_admin_data"]:
                leading += COS_ADMIN_LOG_ALL + COS_ADMIN_ROWS_ALL
                LOG.info("borrowed cos admin: every row it owns; the account itself is kept")
            else:
                leading += COS_ADMIN_LOG_SCOPED + COS_ADMIN_ROWS_SCOPED
                LOG.info("borrowed cos admin: rows from this run only; the account itself is kept")
        statements = leading + statements
    return statements


def sweep(connection, config: dict[str, Any], dry_run: bool) -> list[str]:
    problems: list[str] = []
    schema = config["postgres"]["schema"]
    pattern = f"{config['target']['prefix']}%"
    with connection.cursor() as cursor:
        anchors = resolve_anchors(cursor, schema, pattern, config)
        LOG.info(
            "sweeping LIKE %r: %d user(s), %d org(s), %d item(s)%s",
            pattern, len(anchors["user_ids"]), len(anchors["org_ids"]), len(anchors["item_ids"]),
            f", cos admin {anchors['cos_admin_ids'][0]}" if anchors["cos_admin_ids"] else "",
        )
        LOG.debug("anchors: %s", json.dumps(anchors, indent=2))
        columns = _schema_columns(cursor, schema)
        skipped = []
        total = 0
        for table, template in statements_for(config):
            sql = template.format(schema=schema)
            if columns is not None:
                sql, dropped = _adapt_to_schema(sql, schema, columns)
                if sql is None:
                    skipped.append(table)
                    continue
                if dropped:
                    LOG.info("%s: no %s column here, matched on the rest", table, ", ".join(sorted(set(dropped))))
            LOG.info("SQL: %s", render_sql(cursor, sql, anchors))
            if dry_run:
                # One transaction, rolled back at the end, so the counts reflect
                # the cascade a real run would see. A failing statement would
                # poison that transaction, so each runs under a savepoint.
                cursor.execute("SAVEPOINT stmt")
            try:
                cursor.execute(sql, anchors)
                LOG.info("  -> %s %d row(s) from %s", "would sweep" if dry_run else "swept", cursor.rowcount, table)
                if cursor.rowcount:
                    total += cursor.rowcount
                if dry_run:
                    cursor.execute("RELEASE SAVEPOINT stmt")
                else:
                    connection.commit()
            except Exception as err:  # noqa: BLE001 - one bad statement must not stop the rest
                if dry_run:
                    cursor.execute("ROLLBACK TO SAVEPOINT stmt")
                else:
                    connection.rollback()
                problems.append(f"sweep {table}: {err}")
        if skipped:
            LOG.info("no such table on this deployment, skipped: %s", ", ".join(skipped))
        LOG.info("%s: %d row(s) in total", "DRY RUN" if dry_run else "swept", total)
    if dry_run:
        connection.rollback()
    return problems


def verify(connection, config: dict[str, Any]) -> list[str]:
    schema = config["postgres"]["schema"]
    pattern = f"{config['target']['prefix']}%"
    survivors: list[str] = []
    with connection.cursor() as cursor:
        columns = _schema_columns(cursor, schema)
        for table, template in VERIFY_STATEMENTS:
            if columns is not None and table not in columns:
                continue
            statement = template.format(schema=schema)
            LOG.info("SQL: %s", render_sql(cursor, statement, {"pattern": pattern}))
            try:
                cursor.execute(statement, {"pattern": pattern})
                count = cursor.fetchone()[0]
                LOG.info("  -> %d row(s) left in %s", count, table)
            except Exception as err:  # noqa: BLE001
                connection.rollback()
                survivors.append(f"could not verify {table}: {err}")
                continue
            if count:
                survivors.append(f"{count} row(s) in {table}")
    return survivors


def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(str(args.log_level or config["logging"]["level"]).upper())
    validate(config)

    connection = connect(config)
    try:
        problems: list[str] = []
        if not args.verify_only:
            problems = sweep(connection, config, args.dry_run)
            for problem in problems:
                LOG.error("%s", problem)
        if args.verify_only or (config["sweep"]["verify"] and not args.dry_run):
            survivors = verify(connection, config)
            if survivors:
                LOG.error("rows still under %r%%:", config["target"]["prefix"])
                for line in survivors:
                    LOG.error("  %s", line)
                problems.extend(survivors)
            else:
                LOG.info("nothing left under %r%%", config["target"]["prefix"])
    finally:
        connection.close()

    if problems:
        LOG.error("%d problem(s)", len(problems))
        return EXIT_FAILED
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config", nargs="?", type=Path,
        default=Path(__file__).with_name("database_sweep_config.json"),
        help="JSON config path (default: database_sweep_config.json beside this script)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="run the DELETEs in a transaction, report the counts, roll back")
    parser.add_argument("--verify-only", action="store_true",
                        help="only count what is left under the prefix")
    parser.add_argument("--log-level", help="override logging.level from the config")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        return run(args.config, args)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001 - connection failures, Keycloak refusals
        LOG.error("%s", exc)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
