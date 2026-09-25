#!/usr/bin/env python3
"""Delete the Elasticsearch data indices of catalogue items that no longer exist.

Usage:
    python es_index_sweep.py                       # es_index_sweep_config.json beside this script
    python es_index_sweep.py my_config.json
    python es_index_sweep.py --item-id <uuid> [--item-id <uuid> ...]
    python es_index_sweep.py --list                # show every index under the prefix, delete nothing
    python es_index_sweep.py --dry-run

ngsild_delete_v1.py removes one item's index, exchange and broker user, and
needs the item to still be known. This is for what that cannot reach: an index
named `<index_prefix><item id>` whose catalogue item is already gone. Nothing
on the platform names such an index any more — it carries no prefix, no owner,
no label — so the item ids have to be supplied from outside:

    target.item_ids     pasted in (the harness prints them in its report)
    target.index_names  full index names, for the odd one out
    postgres.enabled    read them from the ControlPlane database instead:
                        `request.item_id` for every access request whose
                        consumer email starts with postgres.prefix. Run this
                        before database_sweep, which deletes those rows.

An index is deleted only when its uuid is in that set. There is deliberately
no "everything under the prefix" mode: `iudx-v2__*` is every item's data on
the deployment.

Elasticsearch is reached through the Kibana console proxy, which answers HTTP
200 whatever Elasticsearch said — the real status is in the body. Same rule as
ngsild_delete_v1.py.

Exit codes:
    0  every matched index deleted, or nothing matched
    1  at least one index could not be deleted
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
from urllib.parse import quote

try:
    import requests
except ModuleNotFoundError:  # allows --help before install
    requests = None  # type: ignore[assignment]

LOG = logging.getLogger("es_index_sweep")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

DEFAULT_CONFIG: dict[str, Any] = {
    "kibana": {
        "host": "",
        "username": "",
        "password": "",
        "timeout_seconds": 60,
        "verify_tls": True,
    },
    "index_prefix": "iudx-v2__",
    "target": {
        "item_ids": [],
        "index_names": [],
    },
    # Optional: resolve item ids from the ControlPlane database.
    "postgres": {
        "enabled": False,
        "host": "",
        "port": 5432,
        "database": "",
        "schema": "aaa",
        "user": "",
        "password": "",
        "sslmode": "prefer",
        "prefix": "",
    },
    "logging": {
        "level": "INFO",
    },
}


class ConfigError(ValueError):
    """A required configuration value is absent or invalid."""


class KibanaError(RuntimeError):
    """Kibana was unreachable or refused the credentials."""


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
    for key in ("host", "username", "password"):
        require(config["kibana"].get(key), f"kibana.{key}")
    require(config["index_prefix"], "index_prefix")
    if requests is None:
        raise ConfigError("the 'requests' package is not installed: pip install requests")
    pg = config["postgres"]
    if pg.get("enabled"):
        for key in ("host", "database", "user", "password", "schema", "prefix"):
            require(pg.get(key), f"postgres.{key}")
        if len(pg["prefix"]) < 3:
            raise ConfigError(f"postgres.prefix {pg['prefix']!r} is too short to match on safely")


# --------------------------------------------------------------------- kibana

class Elasticsearch:
    """Elasticsearch through the Kibana console proxy."""

    def __init__(self, config: dict[str, Any]):
        self.host = config["host"].rstrip("/")
        self.auth = (config["username"], config["password"])
        self.timeout = config["timeout_seconds"]
        self.verify = config["verify_tls"]

    def call(self, method: str, path: str) -> tuple[int, Any]:
        """(elasticsearch status, parsed body). Raises when Kibana itself fails."""
        url = f"{self.host}/api/console/proxy?path={quote(path, safe='')}&method={method}"
        try:
            response = requests.post(
                url, headers={"kbn-xsrf": "true", "Content-Type": "application/json"},
                auth=self.auth, timeout=self.timeout, verify=self.verify,
            )
        except requests.RequestException as exc:
            raise KibanaError(f"Kibana unreachable: {exc}") from exc
        if response.status_code == 401:
            raise KibanaError(f"Kibana rejected credentials for {self.auth[0]!r}")
        raw = response.text or ""
        try:
            parsed = response.json() if raw else None
        except ValueError:
            parsed = None
        status = response.status_code
        text = raw.strip()
        if text[:3].isdigit() and not text.startswith("{"):
            status = int(text[:3])
        elif isinstance(parsed, dict) and "status" in parsed and "error" in parsed:
            status = int(parsed["status"])
        return status, parsed

    def list_indices(self, prefix: str) -> dict[str, str]:
        """{index: document count} for every index under the prefix."""
        status, rows = self.call("GET", f"_cat/indices/{prefix}*?format=json&h=index,docs.count")
        if not isinstance(rows, list):
            raise KibanaError(f"_cat/indices returned {status}: {str(rows)[:300]}")
        return {row["index"]: row.get("docs.count", "?") for row in rows if isinstance(row, dict) and row.get("index")}

    def delete_index(self, index: str) -> None:
        status, body = self.call("DELETE", index)
        if status == 404:
            return  # already gone
        if isinstance(body, dict) and body.get("acknowledged"):
            return
        raise KibanaError(f"delete {index}: elasticsearch returned {status}: {str(body)[:300]}")


# ------------------------------------------------------------------- postgres

def item_ids_from_postgres(config: dict[str, Any]) -> set[str]:
    pg = config["postgres"]
    try:
        import psycopg2
    except ModuleNotFoundError as exc:
        raise ConfigError("psycopg2 is not installed: pip install psycopg2-binary") from exc
    connection = psycopg2.connect(
        host=pg["host"], port=int(pg["port"]), dbname=pg["database"], user=pg["user"],
        password=pg["password"], sslmode=pg["sslmode"], connect_timeout=10,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT DISTINCT item_id FROM {pg['schema']}.request WHERE consumer_email_id LIKE %(pattern)s",
                {"pattern": f"{pg['prefix']}%"},
            )
            found = {str(row[0]) for row in cursor.fetchall() if row[0]}
    finally:
        connection.close()
    LOG.info("postgres: %d item id(s) under %s*", len(found), pg["prefix"])
    return found


# ---------------------------------------------------------------------- sweep

def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(str(args.log_level or config["logging"]["level"]).upper())
    validate(config)

    es = Elasticsearch(config["kibana"])
    prefix = config["index_prefix"]
    indices = es.list_indices(prefix)
    LOG.info("%d index/indices under %s*", len(indices), prefix)

    if args.list:
        for name, docs in sorted(indices.items()):
            print(f"{name}\t{docs} doc(s)")
        return EXIT_OK

    wanted = {str(i) for i in (config["target"].get("item_ids") or []) if i}
    wanted |= set(args.item_id or [])
    if config["postgres"].get("enabled"):
        wanted |= item_ids_from_postgres(config)
    names = {str(n) for n in (config["target"].get("index_names") or []) if n}
    if not (wanted or names):
        raise ConfigError("nothing to match on: set target.item_ids, target.index_names, postgres.enabled, or pass --item-id")

    matched = [
        (name, docs) for name, docs in sorted(indices.items())
        if name in names or (name.startswith(prefix) and name[len(prefix):] in wanted)
    ]
    missing = [n for n in names if n not in indices]
    for name in missing:
        LOG.info("index already gone: %s", name)
    if not matched:
        LOG.info("no index matches the %d id(s) given — nothing to do", len(wanted) + len(names))
        return EXIT_OK

    problems = []
    for name, docs in matched:
        if args.dry_run:
            LOG.info("DRY RUN — would delete %s (%s doc(s))", name, docs)
            continue
        try:
            es.delete_index(name)
            LOG.info("deleted %s (%s doc(s))", name, docs)
        except KibanaError as exc:
            problems.append(str(exc))
            LOG.error("%s", exc)

    if problems:
        return EXIT_FAILED
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config", nargs="?", type=Path,
        default=Path(__file__).with_name("es_index_sweep_config.json"),
        help="JSON config path (default: es_index_sweep_config.json beside this script)",
    )
    parser.add_argument("--item-id", action="append", help="an item whose index to delete, in addition to target.item_ids")
    parser.add_argument("--list", action="store_true", help="print every index under the prefix and exit")
    parser.add_argument("--dry-run", action="store_true", help="show what would be deleted, delete nothing")
    parser.add_argument("--log-level", help="override logging.level from the config")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        return run(args.config, args)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return EXIT_CONFIG
    except (KibanaError, OSError) as exc:
        LOG.error("%s", exc)
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 - a postgres failure, most likely
        LOG.error("%s", exc)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
