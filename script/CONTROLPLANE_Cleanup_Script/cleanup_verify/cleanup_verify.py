#!/usr/bin/env python3
"""Prove a teardown worked: list what is still there under a prefix or set of ids.

Usage:
    python cleanup_verify.py                        # cleanup_verify_config.json beside this script
    python cleanup_verify.py my_config.json
    python cleanup_verify.py --item-id <uuid> --user-id <uuid>

Deletes nothing. The harness ends every run with this check because a teardown
that reported success and left a broker user behind is worse than no teardown
at all — the report would say the opposite of the truth. This is the same
check, for after the standalone scripts have been run by hand.

Five places are looked at, each switched on by its own `enabled`:

    keycloak     users whose username starts with target.prefix
    postgres     the harness's VERIFY_STATEMENTS — organisations, users,
                 requests, memberships, leaderboards, policies, and the
                 compute/credit rows a granted compute role leaves — LIKE
                 '<prefix>%'
    rabbitmq     an exchange or a queue named after each target.item_ids entry,
                 and a broker user named after each target.user_ids entry
    kibana       an Elasticsearch index <index_prefix><item id> for each item
    ogc          collections_details rows whose title starts with target.prefix

Everything found is printed, one line each, and the exit code says whether the
deployment is clean.

Exit codes:
    0  nothing survived
    1  something is still there (listed), or a check could not be made
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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ControlPlane_Workflow.cleanup import VERIFY_STATEMENTS, _schema_columns, render_sql  # noqa: E402

try:
    import requests
except ModuleNotFoundError:  # allows --help before install
    requests = None  # type: ignore[assignment]

try:
    import psycopg2
except ModuleNotFoundError:
    psycopg2 = None  # type: ignore[assignment]

LOG = logging.getLogger("cleanup_verify")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

DEFAULT_CONFIG: dict[str, Any] = {
    "target": {
        "prefix": "",
        "item_ids": [],
        # Keycloak ids of the run's providers: the catalogue names each
        # provider's broker user after it.
        "user_ids": [],
    },
    "keycloak": {
        "enabled": True,
        "url": "",
        "realm": "",
        "admin_client_id": "",
        "admin_client_secret": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    "postgres": {
        "enabled": True,
        "host": "",
        "port": 5432,
        "database": "",
        "schema": "aaa",
        "user": "",
        "password": "",
        "sslmode": "prefer",
    },
    "rabbitmq": {
        "enabled": False,
        "mgmt_url": "",
        "username": "",
        "password": "",
        "vhost": "",
        "verify_tls": True,
        "timeout_seconds": 20,
    },
    "kibana": {
        "enabled": False,
        "host": "",
        "username": "",
        "password": "",
        "index_prefix": "iudx-v2__",
        "timeout_seconds": 60,
        "verify_tls": True,
    },
    "ogc": {
        "enabled": False,
        "host": "",
        "port": 5432,
        "database": "ogc_rs_v2",
        "schema": "",
        "user": "",
        "password": "",
        "sslmode": "prefer",
    },
    "logging": {"level": "INFO"},
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
    target = config["target"]
    prefix = target.get("prefix") or ""
    if not (prefix or target["item_ids"] or target["user_ids"]):
        raise ConfigError("nothing to look for: set target.prefix, target.item_ids or target.user_ids")
    if prefix and len(prefix) < 3:
        raise ConfigError(f"target.prefix {prefix!r} is too short to be meaningful")
    enabled = [name for name in ("keycloak", "postgres", "rabbitmq", "kibana", "ogc") if config[name]["enabled"]]
    if not enabled:
        raise ConfigError("every check is disabled; enable at least one")
    needs = {
        "keycloak": ("url", "realm", "admin_client_id", "admin_client_secret"),
        "postgres": ("host", "database", "schema", "user", "password"),
        "rabbitmq": ("mgmt_url", "username", "password", "vhost"),
        "kibana": ("host", "username", "password", "index_prefix"),
        "ogc": ("host", "database", "user", "password"),
    }
    for name in enabled:
        for key in needs[name]:
            require(config[name].get(key), f"{name}.{key}")
    if any(config[n]["enabled"] for n in ("keycloak", "rabbitmq", "kibana")) and requests is None:
        raise ConfigError("the 'requests' package is not installed: pip install -r ../requirements.txt")
    if any(config[n]["enabled"] for n in ("postgres", "ogc")) and psycopg2 is None:
        raise ConfigError("psycopg2 is not installed: pip install -r ../requirements.txt")


# --------------------------------------------------------------------- checks

def check_keycloak(config: dict[str, Any], prefix: str, found: list[str]) -> None:
    kc = config["keycloak"]
    base = kc["url"].rstrip("/")
    if not prefix:
        LOG.info("keycloak: no prefix given, skipped")
        return
    try:
        token = requests.post(
            f"{base}/realms/{kc['realm']}/protocol/openid-connect/token",
            data={"grant_type": "client_credentials", "client_id": kc["admin_client_id"],
                  "client_secret": kc["admin_client_secret"]},
            timeout=kc["timeout_seconds"], verify=kc["verify_tls"],
        )
        token.raise_for_status()
        users = requests.get(
            f"{base}/admin/realms/{kc['realm']}/users",
            params={"username": prefix, "max": 500},
            headers={"Authorization": f"Bearer {token.json()['access_token']}"},
            timeout=kc["timeout_seconds"], verify=kc["verify_tls"],
        )
        users.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        found.append(f"could not check keycloak: {exc}")
        return
    hits = [u["username"] for u in users.json() if str(u.get("username", "")).startswith(prefix)]
    for name in hits:
        found.append(f"keycloak user {name}")
    LOG.info("keycloak: %d user(s) under %s*", len(hits), prefix)


def check_postgres(config: dict[str, Any], prefix: str, found: list[str]) -> None:
    pg = config["postgres"]
    if not prefix:
        LOG.info("postgres: no prefix given, skipped")
        return
    try:
        connection = psycopg2.connect(
            host=pg["host"], port=int(pg["port"]), dbname=pg["database"], user=pg["user"],
            password=pg["password"], sslmode=pg["sslmode"], connect_timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        found.append(f"could not check postgres: {exc}")
        return
    schema, pattern, hits = pg["schema"], f"{prefix}%", 0
    try:
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
                    LOG.info("  -> %d row(s) in %s", count, table)
                except Exception as exc:  # noqa: BLE001
                    connection.rollback()
                    found.append(f"could not check {schema}.{table}: {exc}")
                    continue
                if count:
                    hits += 1
                    found.append(f"{count} row(s) in {schema}.{table}")
    finally:
        connection.close()
    LOG.info("postgres: %d table(s) with rows under %s", hits, pattern)


def check_rabbitmq(config: dict[str, Any], item_ids: list[str], user_ids: list[str], found: list[str]) -> None:
    mq = config["rabbitmq"]
    base, vhost = mq["mgmt_url"].rstrip("/"), quote(mq["vhost"], safe="")
    auth = (mq["username"], mq["password"])

    def exists(path: str, what: str) -> None:
        try:
            response = requests.get(f"{base}/{path}", auth=auth, timeout=mq["timeout_seconds"], verify=mq["verify_tls"])
        except requests.RequestException as exc:
            found.append(f"could not check {what}: {exc}")
            return
        if response.status_code == 200:
            found.append(what)
        elif response.status_code not in (404,):
            found.append(f"could not check {what}: management API answered {response.status_code}")

    for item_id in item_ids:
        exists(f"api/exchanges/{vhost}/{item_id}", f"rabbitmq exchange {item_id}")
        exists(f"api/queues/{vhost}/{item_id}", f"rabbitmq queue {item_id}")
    for user_id in user_ids:
        exists(f"api/users/{user_id}", f"rabbitmq user {user_id}")
    LOG.info("rabbitmq: checked %d item(s), %d user(s)", len(item_ids), len(user_ids))


def check_kibana(config: dict[str, Any], item_ids: list[str], found: list[str]) -> None:
    kb = config["kibana"]
    if not item_ids:
        LOG.info("kibana: no item ids given, skipped")
        return
    prefix = kb["index_prefix"]
    path = quote(f"_cat/indices/{prefix}*?format=json&h=index,docs.count", safe="")
    try:
        response = requests.post(
            f"{kb['host'].rstrip('/')}/api/console/proxy?path={path}&method=GET",
            headers={"kbn-xsrf": "true"}, auth=(kb["username"], kb["password"]),
            timeout=kb["timeout_seconds"], verify=kb["verify_tls"],
        )
        rows = response.json()
    except Exception as exc:  # noqa: BLE001
        found.append(f"could not check kibana: {exc}")
        return
    if not isinstance(rows, list):
        found.append(f"could not check kibana: {str(rows)[:200]}")
        return
    wanted = {f"{prefix}{item}" for item in item_ids}
    hits = [row for row in rows if isinstance(row, dict) and row.get("index") in wanted]
    for row in hits:
        found.append(f"elasticsearch index {row['index']} ({row.get('docs.count', '?')} doc(s))")
    LOG.info("kibana: %d of %d index/indices still present", len(hits), len(wanted))


def check_ogc(config: dict[str, Any], prefix: str, item_ids: list[str], found: list[str]) -> None:
    ogc = config["ogc"]
    try:
        connection = psycopg2.connect(
            host=ogc["host"], port=int(ogc["port"]), dbname=ogc["database"], user=ogc["user"],
            password=ogc["password"], sslmode=ogc["sslmode"],
            options=f"-c search_path={ogc['schema']}" if ogc.get("schema") else None, connect_timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        found.append(f"could not check ogc: {exc}")
        return
    clauses, params = [], {}
    if prefix:
        clauses.append("title LIKE %(pattern)s")
        params["pattern"] = f"{prefix}%"
    if item_ids:
        clauses.append("id = ANY(%(ids)s::uuid[])")
        params["ids"] = item_ids
    if not clauses:
        LOG.info("ogc: nothing to look for, skipped")
        connection.close()
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id::text, title FROM collections_details WHERE " + " OR ".join(clauses), params)
            rows = cursor.fetchall()
    except Exception as exc:  # noqa: BLE001
        found.append(f"could not check ogc: {exc}")
        return
    finally:
        connection.close()
    for collection_id, title in rows:
        found.append(f"ogc collection {collection_id} ({title})")
    LOG.info("ogc: %d collection(s) still present", len(rows))


# ------------------------------------------------------------------------ run

def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(str(args.log_level or config["logging"]["level"]).upper())
    target = config["target"]
    target["item_ids"] = [str(i) for i in (target.get("item_ids") or []) if i] + list(args.item_id or [])
    target["user_ids"] = [str(i) for i in (target.get("user_ids") or []) if i] + list(args.user_id or [])
    validate(config)
    prefix = (target.get("prefix") or "").strip()

    found: list[str] = []
    if config["keycloak"]["enabled"]:
        check_keycloak(config, prefix, found)
    if config["postgres"]["enabled"]:
        check_postgres(config, prefix, found)
    if config["rabbitmq"]["enabled"]:
        check_rabbitmq(config, target["item_ids"], target["user_ids"], found)
    if config["kibana"]["enabled"]:
        check_kibana(config, target["item_ids"], found)
    if config["ogc"]["enabled"]:
        check_ogc(config, prefix, target["item_ids"], found)

    if found:
        LOG.error("%d survivor(s) / failed check(s):", len(found))
        for line in found:
            print(f"  {line}")
        return EXIT_FAILED
    LOG.info("clean: nothing survived")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config", nargs="?", type=Path,
        default=Path(__file__).with_name("cleanup_verify_config.json"),
        help="JSON config path (default: cleanup_verify_config.json beside this script)",
    )
    parser.add_argument("--item-id", action="append", help="an item id to check for, in addition to target.item_ids")
    parser.add_argument("--user-id", action="append", help="a provider's Keycloak id to check the broker for")
    parser.add_argument("--log-level", help="override logging.level from the config")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        return run(args.config, args)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001
        LOG.error("%s", exc)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
