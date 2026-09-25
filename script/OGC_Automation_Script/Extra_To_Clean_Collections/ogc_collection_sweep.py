#!/usr/bin/env python3
"""Find and delete OGC collections left behind in the OGC server's database.

Usage:
    python ogc_collection_sweep.py                      # ogc_collection_sweep_config.json beside this script
    python ogc_collection_sweep.py my_config.json
    python ogc_collection_sweep.py --list               # show what matches, delete nothing
    python ogc_collection_sweep.py --collection-id <uuid> [...]
    python ogc_collection_sweep.py --dry-run

Vector_Automation/Deletion/vector_deletion.py removes one collection whose id
you already know. This is for the rest of a teardown:

  * finding them — collections live in the OGC database keyed by the item
    uuid, in tables with no namespaced column, so they are matched on
    `collections_details.title`/`description` LIKE '<title_prefix>%', on the
    owner's Keycloak id in `ri_details.role_id`, and on exact
    (title, description) pairs for collections onboarded before titles
    carried a namespace. A pair matches only when both halves do: a title
    alone is not specific enough to delete on;

  * the STAC children first — `stac_items_assets`, `stac_collections_assets`
    and `stac_collections_part` reference the collection and do not cascade,
    so a raster collection whose items were never deleted through the API
    fails the DELETE with a foreign-key violation and the whole transaction
    rolls back. They are cleared over a separate autocommit connection, one
    table at a time, so a deployment missing one of them loses nothing else;

  * the `roles` row a run may have inserted for its throwaway provider
    (target.remove_roles_rows_for), after the collections that reference it.

The files in S3 are not touched here — `<id>.gpkg` for a vector collection,
the `<id>/` folder for a raster one. `--list` prints each collection's kind so
Extra_To_Clean_S3/s3_deleteion.py can be pointed at it.

Exit codes:
    0  every matched collection removed, or nothing matched
    1  at least one deletion failed or a collection survived
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

try:
    import psycopg2
    from psycopg2 import sql
except ModuleNotFoundError:  # allows --help before install
    psycopg2 = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]

LOG = logging.getLogger("ogc_collection_sweep")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

# Children of collections_details that do not cascade. Cleared before the
# collection so its DELETE cannot fail on a foreign key.
BLOCKING_CHILDREN = (
    ("stac_items_assets", "collection_id"),
    ("stac_collections_assets", "stac_collections_id"),
    ("stac_collections_part", "collection_id"),
)

DEFAULT_CONFIG: dict[str, Any] = {
    "postgres": {
        "host": "",
        "port": 5432,
        # The OGC server's own database — not the ControlPlane one.
        "database": "ogc_rs_v2",
        # Only when the OGC tables live outside the default search_path.
        "schema": "",
        "user": "",
        "password": "",
        "sslmode": "prefer",
        "connect_timeout_seconds": 15,
    },
    "target": {
        "collection_ids": [],
        "title_prefix": "",
        "owner_user_ids": [],
        "labels": [],
        # Owners whose collections are never deleted, whatever matched them.
        "protected_owner_ids": [],
        # Keycloak ids whose `roles` row should go once their collections have.
        "remove_roles_rows_for": [],
    },
    "delete": {
        "clear_stac_children": True,
        "drop_table": True,
        # Re-query each id afterwards and fail if the row is still there.
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


def validate(config: dict[str, Any], collection_ids: list[str]) -> None:
    pg = config["postgres"]
    for key in ("host", "database", "user", "password"):
        require(pg.get(key), f"postgres.{key}")
    target = config["target"]
    pairs = [p for p in target.get("labels") or [] if p.get("title") and p.get("description")]
    roles_only = bool(target.get("remove_roles_rows_for"))
    if not (collection_ids or target.get("title_prefix") or target.get("owner_user_ids") or pairs or roles_only):
        raise ConfigError(
            "nothing to match on: set target.collection_ids, target.title_prefix, "
            "target.owner_user_ids, target.labels, target.remove_roles_rows_for, or pass --collection-id"
        )
    prefix = target.get("title_prefix")
    if prefix and len(prefix) < 3:
        raise ConfigError(f"target.title_prefix {prefix!r} is too short to sweep on safely")
    if prefix and "%" in prefix:
        raise ConfigError("target.title_prefix must be a literal prefix — the LIKE wildcard is added by the script")
    if psycopg2 is None:
        raise ConfigError("psycopg2 is not installed: pip install psycopg2-binary")


# ------------------------------------------------------------------- database

def connect(config: dict[str, Any], autocommit: bool = False):
    pg = config["postgres"]
    connection = psycopg2.connect(
        host=pg["host"], port=int(pg["port"]), dbname=pg["database"], user=pg["user"],
        password=pg["password"], sslmode=pg["sslmode"],
        options=f"-c search_path={pg['schema']}" if pg.get("schema") else None,
        connect_timeout=int(pg["connect_timeout_seconds"]),
    )
    connection.autocommit = autocommit
    return connection


def find_collections(connection, config: dict[str, Any], explicit_ids: list[str]) -> list[tuple[str, str, str | None, bool]]:
    """[(id, title, owner_id, has_stac)] for everything the target names."""
    target = config["target"]
    clauses, params = [], {}
    if explicit_ids:
        clauses.append("cd.id = ANY(%(ids)s::uuid[])")
        params["ids"] = explicit_ids
    if target.get("title_prefix"):
        clauses += ["cd.title LIKE %(pattern)s", "cd.description LIKE %(pattern)s"]
        params["pattern"] = f"{target['title_prefix']}%"
    owners = [str(u) for u in target.get("owner_user_ids") or [] if u]
    if owners:
        clauses.append("rd.role_id = ANY(%(owners)s::uuid[])")
        params["owners"] = owners
    for index, pair in enumerate(target.get("labels") or []):
        if not (pair.get("title") and pair.get("description")):
            continue
        clauses.append(f"(cd.title = %(title{index})s AND cd.description = %(description{index})s)")
        params[f"title{index}"] = pair["title"]
        params[f"description{index}"] = pair["description"]

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT cd.id::text, cd.title, rd.role_id::text "
            "FROM collections_details cd LEFT JOIN ri_details rd ON rd.id = cd.id "
            "WHERE " + " OR ".join(clauses),
            params,
        )
        found = cursor.fetchall()
        cursor.execute("SELECT to_regclass('stac_collections_part') IS NOT NULL")
        has_stac_table = cursor.fetchone()[0]
        rows = []
        for collection_id, title, owner_id in found:
            stac = False
            if has_stac_table:
                cursor.execute(
                    "SELECT EXISTS (SELECT 1 FROM stac_collections_part WHERE collection_id = %s::uuid)",
                    (collection_id,),
                )
                stac = bool(cursor.fetchone()[0])
            rows.append((collection_id, title, owner_id, stac))
    connection.rollback()  # read-only so far; leave no transaction open
    return rows


def clear_stac_children(config: dict[str, Any], collection_id: str) -> dict[str, int]:
    """Delete the STAC rows that would fail the collection delete. {table: rows}."""
    removed = {}
    connection = connect(config, autocommit=True)
    try:
        with connection.cursor() as cursor:
            for table, column in BLOCKING_CHILDREN:
                try:
                    cursor.execute(
                        sql.SQL("DELETE FROM {} WHERE {} = %s::uuid").format(
                            sql.Identifier(table), sql.Identifier(column)
                        ),
                        (collection_id,),
                    )
                except psycopg2.Error:
                    continue  # not every deployment carries every STAC table
                if cursor.rowcount:
                    removed[table] = cursor.rowcount
    finally:
        connection.close()
    return removed


def delete_collection(connection, config: dict[str, Any], collection_id: str) -> None:
    """The same four steps as vector_deletion.py, in one transaction."""
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM ri_details WHERE id = %s::uuid", (collection_id,))
        LOG.info("  ri_details: %d row(s)", cursor.rowcount)
        cursor.execute("DELETE FROM collections_enclosure WHERE collections_id = %s::uuid", (collection_id,))
        LOG.info("  collections_enclosure: %d row(s)", cursor.rowcount)
        cursor.execute("DELETE FROM collections_details WHERE id = %s::uuid", (collection_id,))
        LOG.info("  collections_details: %d row(s)", cursor.rowcount)
        if config["delete"]["drop_table"]:
            cursor.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(collection_id)))
            LOG.info("  table %s dropped if it existed", collection_id)
    connection.commit()


def still_exists(connection, collection_id: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM collections_details WHERE id = %s::uuid", (collection_id,))
        found = cursor.fetchone() is not None
    connection.rollback()
    return found


def remove_roles_rows(connection, user_ids: list[str], protected: set[str], dry_run: bool) -> None:
    for user_id in user_ids:
        if user_id.lower() in protected:
            LOG.info("protected owner, roles row kept: %s", user_id)
            continue
        if dry_run:
            LOG.info("DRY RUN — would delete roles row for %s", user_id)
            continue
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM roles WHERE user_id = %s::uuid", (user_id,))
            LOG.info("roles row(s) for %s: %d removed", user_id, cursor.rowcount)
        connection.commit()


# ---------------------------------------------------------------------- sweep

def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(str(args.log_level or config["logging"]["level"]).upper())

    explicit = [str(i) for i in (config["target"].get("collection_ids") or []) if i]
    explicit += list(args.collection_id or [])
    validate(config, explicit)

    protected = {str(i).lower() for i in config["target"].get("protected_owner_ids") or [] if i}
    connection = connect(config)
    problems: list[str] = []
    try:
        pairs = [p for p in config["target"].get("labels") or [] if p.get("title") and p.get("description")]
        has_clauses = bool(explicit or config["target"].get("title_prefix") or config["target"].get("owner_user_ids") or pairs)
        rows = find_collections(connection, config, explicit) if has_clauses else []
        found_ids = {row[0] for row in rows}
        for collection_id in explicit:
            if collection_id not in found_ids:
                LOG.info("collection already gone: %s", collection_id)
        if not rows:
            LOG.info("nothing matched")
        else:
            LOG.info("%d collection(s) matched", len(rows))

        if args.list:
            for collection_id, title, owner_id, has_stac in rows:
                kind = "raster" if has_stac else "vector"
                flag = "  [protected owner]" if owner_id and owner_id.lower() in protected else ""
                print(f"{collection_id}\t{kind}\t{owner_id or '-'}\t{title}{flag}")
            return EXIT_OK

        for collection_id, title, owner_id, has_stac in rows:
            kind = "raster" if has_stac else "vector"
            if owner_id and owner_id.lower() in protected:
                LOG.info("protected owner, collection left alone: %s (%s)", collection_id, title)
                continue
            if args.dry_run:
                LOG.info("DRY RUN — would delete %s collection %s (%s)", kind, collection_id, title)
                continue
            LOG.info("deleting %s collection %s (%s)", kind, collection_id, title)
            if config["delete"]["clear_stac_children"]:
                try:
                    cleared = clear_stac_children(config, collection_id)
                    if cleared:
                        LOG.info("  cleared %s", ", ".join(f"{n} from {t}" for t, n in sorted(cleared.items())))
                except psycopg2.Error as exc:
                    LOG.warning("  could not clear STAC rows: %s", exc)
            try:
                delete_collection(connection, config, collection_id)
            except psycopg2.Error as exc:
                connection.rollback()
                problems.append(f"delete {collection_id}: {exc} — transaction rolled back")
                LOG.error("%s", problems[-1])
                continue
            if config["delete"]["verify"] and still_exists(connection, collection_id):
                problems.append(f"collection {collection_id} still exists after delete")
            else:
                LOG.info("  s3 objects to remove by hand: %s", f"{collection_id}/ (tiffs)" if has_stac else f"{collection_id}.gpkg")

        roles_for = [str(u) for u in config["target"].get("remove_roles_rows_for") or [] if u]
        if roles_for:
            try:
                remove_roles_rows(connection, roles_for, protected, args.dry_run)
            except psycopg2.Error as exc:
                connection.rollback()
                problems.append(f"roles rows: {exc}")
    finally:
        connection.close()

    if problems:
        for problem in problems:
            LOG.error("%s", problem)
        return EXIT_FAILED
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config", nargs="?", type=Path,
        default=Path(__file__).with_name("ogc_collection_sweep_config.json"),
        help="JSON config path (default: ogc_collection_sweep_config.json beside this script)",
    )
    parser.add_argument("--collection-id", action="append", help="a collection to delete, in addition to target.collection_ids")
    parser.add_argument("--list", action="store_true", help="print what matches (id, kind, owner, title) and exit")
    parser.add_argument("--dry-run", action="store_true", help="show what would be deleted, delete nothing")
    parser.add_argument("--log-level", help="override logging.level from the config")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        return run(args.config, args)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001 - a connection failure, most likely
        LOG.error("%s", exc)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
