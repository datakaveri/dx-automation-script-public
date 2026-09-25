#!/usr/bin/env python3
"""Delete organisations — through the API where it can, the database where it must.

Usage:
    python organisation_deletion.py                          # organisation_deletion_config.json beside this script
    python organisation_deletion.py my_config.json
    python organisation_deletion.py --org-id <uuid> [--org-id <uuid> ...]
    python organisation_deletion.py --request-id <uuid>      # the org-create request the approval answered
    python organisation_deletion.py --dry-run

An organisation can be named by its id, or by the id of the organisation-create
request that was approved (`req_id` in POST /organisations/requests/approve).
Approval copies the request's name into a new organizations row and makes the
requester its admin, and nothing links the two afterwards. A request id is
resolved to its organisation through the name, or — once PUT /organisations
has renamed the org, which the collection does every run — through the admin
membership row (requested_by / manager_email). The request row goes too.

`DELETE /iudx/v2/auth/organisations/{id}` is a bare delete with no cascade. It
fails with a foreign-key violation while any `organization_users` row still
references the organisation, and the org admin's own row can never be removed
by the API (AdminHandler refuses org admins). So for an approved organisation
the API call is a guaranteed 500, and the rows have to go from the database.

`delete.mode` decides:

    auto      try the API first, fall back to the database for what it refuses  (default)
    api       API only; report the refusal
    postgres  database only — the mode for an organisation whose admin is gone

The database pass runs every organisation-keyed statement of the harness's own
sweep, imported from script/ControlPlane_Workflow/cleanup.py, with the
organisation id as the only anchor: every term keyed on a user, an item or the
name prefix matches nothing, so what goes is exactly the rows that name this
organisation — memberships, join and provider requests, leaderboards, access
rule grants, policies and access requests made against its items, the create
request, the organisation row itself. `delete.audit_rows` adds the activity
log tables, which are append-only on the platform side; leave it off on a
shared stack. Afterwards every one of those tables is re-counted, and a
surviving row fails the run.

Users are not deleted; keycloak_user_sweep and consumer_deletion do that. But
approval also stamped the admin's Keycloak account with an `organisation_id`
attribute and the org_admin/provider roles, and an account left pointing at a
deleted organisation breaks its next sign-in. When `keycloak.admin_client_*`
is set, every account carrying the organisation's id has that attribute and
those roles removed (`delete.detach_keycloak_users`).

`user_creation/org_admin/deletion/org_admin_deletion.py` does this too, but
only alongside deleting the admin account it belongs to. This is for the case
where you have an id and nothing else.

Exit codes:
    0  every organisation is gone (or was already)
    1  at least one could not be removed
    2  configuration error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ControlPlane_Workflow.cleanup import (  # noqa: E402
    AUDIT_SWEEP_STATEMENTS,
    SWEEP_STATEMENTS,
    _adapt_to_schema,
    _schema_columns,
    render_sql,
)

try:
    import requests
except ModuleNotFoundError:  # allows --help before install
    requests = None  # type: ignore[assignment]

try:
    import psycopg2
except ModuleNotFoundError:
    psycopg2 = None  # type: ignore[assignment]

LOG = logging.getLogger("organisation_deletion")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

# Roles the org-create approval assigns (OrganizationLifecycleServiceImpl).
# Same tuple as ControlPlane_Workflow.kc.ROLES_AFTER_ORG_APPROVAL; repeated
# here so this script does not import that module's `requests` dependency
# just to read two strings.
ORG_ROLES = ("org_admin", "provider")
ORG_ATTRIBUTE = "organisation_id"
# Approval writes the name beside the id; it means nothing without the id.
ORG_NAME_ATTRIBUTE = "organisation_name"

DEFAULT_CONFIG: dict[str, Any] = {
    "control_plane": {
        "base_url": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    "keycloak": {
        "url": "",
        "realm": "",
        "user_client_id": "postman-client",
        "user_client_secret": "",
        # The Admin API client. Only needed for delete.detach_keycloak_users.
        "admin_client_id": "",
        "admin_client_secret": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    # The account the API call is made as — the organisation's admin, or a
    # cos_admin. Only needed for delete.mode auto/api.
    "actor": {"username": "", "password": "", "token": ""},
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
    "target": {
        "org_ids": [],
        # Ids of approved organisation-create requests; resolved to their
        # organisations through the name or the admin membership, and
        # deleted themselves.
        "request_ids": [],
    },
    "delete": {
        "mode": "auto",
        # Re-count every table afterwards and fail if a row is left.
        "verify": True,
        # Also clear the activity/audit log rows that name the organisation.
        "audit_rows": False,
        # Strip the organisation attribute and org roles from the Keycloak
        # accounts that carry the organisation's id. Needs keycloak.admin_client_*.
        "detach_keycloak_users": True,
    },
    "endpoints": {
        "kc_token": "/realms/{realm}/protocol/openid-connect/token",
        "kc_users": "/admin/realms/{realm}/users",
        "kc_user": "/admin/realms/{realm}/users/{user_id}",
        "kc_user_realm_roles": "/admin/realms/{realm}/users/{user_id}/role-mappings/realm",
        "cp_organisation": "/iudx/v2/auth/organisations/{org_id}",
    },
    "logging": {"level": "INFO"},
}


class ConfigError(ValueError):
    """A required configuration value is absent or invalid."""


class ApiError(RuntimeError):
    """Keycloak or ControlPlane rejected a request."""


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


def _check_uuids(values: list[str], what: str) -> None:
    for value in values:
        try:
            uuid.UUID(value)
        except ValueError as exc:
            raise ConfigError(f"{what} {value!r} is not a uuid") from exc


def validate(config: dict[str, Any], org_ids: list[str], request_ids: list[str]) -> None:
    if not org_ids and not request_ids:
        raise ConfigError(
            "nothing to delete: set target.org_ids / target.request_ids or pass --org-id / --request-id"
        )
    _check_uuids(org_ids, "org id")
    _check_uuids(request_ids, "request id")
    mode = str(config["delete"]["mode"]).lower()
    if mode not in ("auto", "api", "postgres"):
        raise ConfigError("delete.mode must be auto, api or postgres")
    if mode in ("auto", "api"):
        require(config["control_plane"]["base_url"], "control_plane.base_url")
        if not config["actor"].get("token"):
            require(config["actor"].get("username"), "actor.username")
            require(config["actor"].get("password"), "actor.password")
            require(config["keycloak"]["url"], "keycloak.url")
            require(config["keycloak"]["realm"], "keycloak.realm")
        if requests is None:
            raise ConfigError("the 'requests' package is not installed: pip install -r ../requirements.txt")
    # A request id can only be resolved in the database, whatever the mode.
    if mode in ("auto", "postgres") or request_ids:
        for key in ("host", "database", "user", "password", "schema"):
            require(config["postgres"].get(key), f"postgres.{key}")
        if psycopg2 is None:
            raise ConfigError("psycopg2 is not installed: pip install -r ../requirements.txt")
    if config["delete"]["detach_keycloak_users"] and keycloak_admin_configured(config):
        require(config["keycloak"]["url"], "keycloak.url")
        require(config["keycloak"]["realm"], "keycloak.realm")
        if requests is None:
            raise ConfigError("the 'requests' package is not installed: pip install -r ../requirements.txt")


def keycloak_admin_configured(config: dict[str, Any]) -> bool:
    kc = config["keycloak"]
    return bool(kc.get("admin_client_id") and kc.get("admin_client_secret"))


# ----------------------------------------------------------------------- api

def _post_form(url: str, form: dict[str, str], timeout: int, verify: bool, label: str) -> dict[str, Any]:
    try:
        response = requests.post(url, data=form, timeout=timeout, verify=verify)
    except requests.RequestException as exc:
        raise ApiError(f"{label}: {exc}") from exc
    if response.status_code != 200:
        raise ApiError(f"{label}: {response.status_code} {response.text[:300]}")
    return response.json()


def actor_token(config: dict[str, Any]) -> str:
    actor = config["actor"]
    if actor.get("token"):
        return actor["token"]
    kc = config["keycloak"]
    form = {
        "grant_type": "password",
        "client_id": kc["user_client_id"],
        "username": actor["username"],
        "password": actor["password"],
    }
    if kc.get("user_client_secret"):
        form["client_secret"] = kc["user_client_secret"]
    url = kc["url"].rstrip("/") + config["endpoints"]["kc_token"].format(realm=kc["realm"])
    return _post_form(url, form, kc["timeout_seconds"], kc["verify_tls"], f"token for {actor['username']}")["access_token"]


def delete_via_api(config: dict[str, Any], token: str, org_id: str) -> tuple[bool, str]:
    """(gone, detail). 200 and 404 are gone; anything else is a refusal."""
    cp = config["control_plane"]
    url = cp["base_url"].rstrip("/") + config["endpoints"]["cp_organisation"].format(org_id=org_id)
    try:
        response = requests.delete(
            url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=cp["timeout_seconds"], verify=cp["verify_tls"],
        )
    except requests.RequestException as exc:
        return False, f"request failed: {exc}"
    if response.status_code in (200, 204):
        return True, "deleted through the API"
    if response.status_code == 404:
        return True, "already gone"
    return False, f"API answered {response.status_code}: {response.text[:200]}"


# ------------------------------------------------------------------ keycloak

class KeycloakAdmin:
    """The few Admin API calls detaching a user from an organisation needs."""

    def __init__(self, config: dict[str, Any]):
        self.kc = config["keycloak"]
        self.endpoints = config["endpoints"]
        self._token = ""

    def _url(self, name: str, **fields: str) -> str:
        return self.kc["url"].rstrip("/") + self.endpoints[name].format(realm=self.kc["realm"], **fields)

    def token(self) -> str:
        if not self._token:
            payload = _post_form(
                self._url("kc_token"),
                {
                    "grant_type": "client_credentials",
                    "client_id": self.kc["admin_client_id"],
                    "client_secret": self.kc["admin_client_secret"],
                },
                self.kc["timeout_seconds"], self.kc["verify_tls"], "keycloak admin token",
            )
            self._token = payload["access_token"]
        return self._token

    def _request(self, method: str, url: str, label: str, *, expect=(200, 204), **kwargs: Any) -> Any:
        headers = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
        try:
            response = requests.request(
                method, url, headers=headers, timeout=self.kc["timeout_seconds"],
                verify=self.kc["verify_tls"], **kwargs,
            )
        except requests.RequestException as exc:
            raise ApiError(f"{label}: {exc}") from exc
        if response.status_code not in expect:
            raise ApiError(f"{label}: {response.status_code} {response.text[:300]}")
        if not response.text:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def users_of_organisation(self, org_id: str) -> list[dict[str, Any]]:
        """Every account whose organisation attribute names this org.

        `q` searches custom attributes; the value is re-checked on each hit
        because the search is a prefix match on some Keycloak versions.
        """
        matches = self._request(
            "GET", self._url("kc_users"), f"find users of organisation {org_id}",
            params={"q": f"{ORG_ATTRIBUTE}:{org_id}", "max": 500},
        ) or []
        hits = []
        for user in matches:
            values = (user.get("attributes") or {}).get(ORG_ATTRIBUTE) or []
            if org_id in [str(v) for v in values]:
                hits.append(user)
        return hits

    def detach(self, user: dict[str, Any], org_id: str) -> list[str]:
        """Drop the org attribute and roles. Returns what was removed."""
        user_id, username = user["id"], user.get("username", user["id"])
        removed: list[str] = []

        attributes = dict(user.get("attributes") or {})
        values = [str(v) for v in attributes.get(ORG_ATTRIBUTE) or [] if str(v) != org_id]
        if values:
            attributes[ORG_ATTRIBUTE] = values
        else:
            attributes.pop(ORG_ATTRIBUTE, None)
            attributes.pop(ORG_NAME_ATTRIBUTE, None)
        # PUT wants the whole representation; sending the fetched one back with
        # only the attributes changed leaves everything else as it was.
        self._request(
            "PUT", self._url("kc_user", user_id=user_id), f"update {username}",
            json={**user, "attributes": attributes},
        )
        removed.append(f"attribute {ORG_ATTRIBUTE}")
        if not values:
            removed.append(f"attribute {ORG_NAME_ATTRIBUTE}")

        roles = self._request(
            "GET", self._url("kc_user_realm_roles", user_id=user_id), f"read roles of {username}",
        ) or []
        to_remove = [r for r in roles if r.get("name") in ORG_ROLES]
        if to_remove:
            self._request(
                "DELETE", self._url("kc_user_realm_roles", user_id=user_id),
                f"remove roles from {username}", json=to_remove,
            )
            removed.extend(f"role {r['name']}" for r in to_remove)
        return removed


def detach_keycloak_users(config: dict[str, Any], org_ids: list[str], dry_run: bool) -> list[str]:
    """Unhook every account still carrying one of these organisations."""
    if not config["delete"]["detach_keycloak_users"]:
        LOG.info("delete.detach_keycloak_users is off — Keycloak accounts left as they are")
        return []
    if not keycloak_admin_configured(config):
        LOG.warning(
            "keycloak.admin_client_id/secret not set — accounts that carry the organisation's "
            "id keep their %s attribute and %s roles", ORG_ATTRIBUTE, "/".join(ORG_ROLES),
        )
        return []

    kc = KeycloakAdmin(config)
    problems: list[str] = []
    for org_id in org_ids:
        try:
            users = kc.users_of_organisation(org_id)
        except ApiError as exc:
            problems.append(f"keycloak: {exc}")
            continue
        if not users:
            LOG.info("keycloak: no account carries organisation %s", org_id)
            continue
        for user in users:
            username = user.get("username", user["id"])
            if dry_run:
                LOG.info("DRY RUN — would detach %s from organisation %s", username, org_id)
                continue
            try:
                removed = kc.detach(user, org_id)
                LOG.info("keycloak: detached %s from %s (%s)", username, org_id, ", ".join(removed))
            except ApiError as exc:
                problems.append(f"keycloak: could not detach {username} from {org_id}: {exc}")
    return problems


# ------------------------------------------------------------------ postgres

def connect(config: dict[str, Any]):
    pg = config["postgres"]
    return psycopg2.connect(
        host=pg["host"], port=int(pg["port"]), dbname=pg["database"], user=pg["user"],
        password=pg["password"], sslmode=pg["sslmode"],
        connect_timeout=int(pg["connect_timeout_seconds"]),
    )


def resolve_requests(connection, config: dict[str, Any], request_ids: list[str]) -> list[str]:
    """Organisation ids behind approved create requests.

    Approval copies the request's name into the organizations row, so the name
    is the first key. It stops being one as soon as PUT /organisations/{id}
    renames the org — which the collection's "update org details" test does
    every run — so the second key is the admin membership approval also wrote:
    organization_users has UNIQUE (user_id) and UNIQUE (official_email), and
    the request's requested_by / manager_email are exactly those two columns.
    A request that matches on neither is one whose approval never completed
    (or whose org is already gone); only its own row goes.
    """
    schema = config["postgres"]["schema"]
    found: list[str] = []
    with connection.cursor() as cursor:
        statement = (
            f"SELECT r.id::text, r.name, r.status, "
            f"  o.id::text AS by_name, "
            f"  u.organization_id::text AS by_admin, u.role, o2.name AS current_name "
            f"FROM {schema}.organization_create_requests r "
            f"LEFT JOIN {schema}.organizations o ON o.name = r.name "
            f"LEFT JOIN {schema}.organization_users u "
            f"  ON u.user_id = r.requested_by OR u.official_email = r.manager_email "
            f"LEFT JOIN {schema}.organizations o2 ON o2.id = u.organization_id "
            f"WHERE r.id = ANY(%(ids)s::uuid[])"
        )
        LOG.info("SQL: %s", render_sql(cursor, statement, {"ids": request_ids}))
        cursor.execute(statement, {"ids": request_ids})
        rows = cursor.fetchall()
    connection.rollback()
    seen = {row[0] for row in rows}
    for request_id in request_ids:
        if request_id not in seen:
            LOG.info("request %s: no such row — already gone", request_id)
    for request_id, name, status, by_name, by_admin, role, current_name in rows:
        if by_name:
            LOG.info("request %s (%s, %s) -> organisation %s (by name)", request_id, name, status, by_name)
            found.append(by_name)
        if by_admin and by_admin != by_name:
            # The requester is the admin of an org under another name: the
            # org this request created, renamed since. A 'member' row is a
            # join elsewhere and says nothing about this request.
            if role == "admin":
                LOG.info("request %s (%s, %s) -> organisation %s (by admin membership; now named %r)",
                         request_id, name, status, by_admin, current_name)
                found.append(by_admin)
            else:
                LOG.info("request %s (%s, %s): requester is only a member of %s (%r), not its admin — ignored",
                         request_id, name, status, by_admin, current_name)
        if not by_name and not (by_admin and role == "admin"):
            LOG.info("request %s (%s, %s): no organisation by that name or admin — only the request row goes",
                     request_id, name, status)
    # One request can match both ways to the same org; keep order, drop repeats.
    return list(dict.fromkeys(found))


def _statements(config: dict[str, Any]) -> list[tuple[str, str]]:
    """The harness statements this script may run: everything keyed on an org."""
    chosen = []
    if config["delete"]["audit_rows"]:
        chosen += [(t, s) for t, s in AUDIT_SWEEP_STATEMENTS if "%(org_ids)s" in s]
    chosen += [(t, s) for t, s in SWEEP_STATEMENTS if "%(org_ids)s" in s]
    # The request rows named directly. Runs after the harness's own
    # organization_create_requests statement, so a request whose organisation
    # was never created (or is already gone) still goes.
    chosen.append((
        "organization_create_requests (by request id)",
        "DELETE FROM {schema}.organization_create_requests WHERE id = ANY(%(request_ids)s::uuid[])",
    ))
    return chosen


def _anchors(org_ids: list[str], request_ids: list[str]) -> dict[str, Any]:
    # Every other anchor is made unmatchable, so only the org/request terms can hit.
    return {
        "pattern": f"zz-{uuid.uuid4()}",  # a literal with no wildcard: matches only itself
        "user_ids": [],
        "item_ids": [],
        "org_ids": org_ids,
        "cos_admin_ids": [],
        "request_ids": request_ids,
    }


def _adapted(cursor, config: dict[str, Any]) -> list[tuple[str, str]]:
    """(table, sql) for this deployment, with missing tables/columns pruned."""
    schema = config["postgres"]["schema"]
    columns = _schema_columns(cursor, schema)
    out = []
    for table, template in _statements(config):
        statement = template.format(schema=schema)
        if columns is not None:
            statement, dropped = _adapt_to_schema(statement, schema, columns)
            if statement is None:
                LOG.debug("%s: not on this deployment, skipped", table)
                continue
            if dropped:
                LOG.info("%s: no %s column on this deployment, matched on the rest",
                         table, ", ".join(sorted(set(dropped))))
        out.append((table, statement))
    return out


def delete_via_postgres(
    connection, config: dict[str, Any], org_ids: list[str], request_ids: list[str], dry_run: bool
) -> list[str]:
    """Run the org-keyed part of the harness sweep. Returns problem strings."""
    anchors = _anchors(org_ids, request_ids)
    problems: list[str] = []
    total = 0
    with connection.cursor() as cursor:
        for table, statement in _adapted(cursor, config):
            LOG.info("SQL: %s", render_sql(cursor, statement, anchors))
            if dry_run:
                cursor.execute("SAVEPOINT stmt")
            try:
                cursor.execute(statement, anchors)
                LOG.info("  -> %s %d row(s) from %s", "would delete" if dry_run else "deleted", cursor.rowcount, table)
                if cursor.rowcount:
                    total += cursor.rowcount
                if dry_run:
                    cursor.execute("RELEASE SAVEPOINT stmt")
                else:
                    connection.commit()
            except Exception as exc:  # noqa: BLE001 - one failed table must not stop the rest
                if dry_run:
                    cursor.execute("ROLLBACK TO SAVEPOINT stmt")
                else:
                    connection.rollback()
                problems.append(f"{table}: {exc}")
    LOG.info("%s: %d row(s) in total", "DRY RUN" if dry_run else "database", total)
    if dry_run:
        connection.rollback()
    return problems


_DELETE_HEAD = re.compile(r"^\s*DELETE\s+FROM\s+", re.I)


def survivors(
    connection, config: dict[str, Any], org_ids: list[str], request_ids: list[str], org_names: list[str]
) -> list[str]:
    """Re-count every table the delete touched; a row left anywhere is a problem.

    The counts are the DELETE statements turned into SELECTs, so they cannot
    drift from what was deleted. The create-request statement reaches the
    request through the organizations row, which is gone by now — so that
    table is re-checked by the names captured before anything was deleted.
    """
    schema = config["postgres"]["schema"]
    anchors = _anchors(org_ids, request_ids)
    anchors["org_names"] = org_names
    left: list[str] = []
    with connection.cursor() as cursor:
        checks = [(t, _DELETE_HEAD.sub("SELECT count(*) FROM ", s)) for t, s in _adapted(cursor, config)]
        checks.append((
            "organization_create_requests (by name)",
            f"SELECT count(*) FROM {schema}.organization_create_requests WHERE name = ANY(%(org_names)s::text[])",
        ))
        for table, statement in checks:
            LOG.info("SQL: %s", render_sql(cursor, statement, anchors))
            try:
                cursor.execute(statement, anchors)
                count = cursor.fetchone()[0]
                LOG.info("  -> %d row(s) left in %s", count, table)
            except Exception as exc:  # noqa: BLE001
                connection.rollback()
                left.append(f"could not verify {table}: {exc}")
                continue
            if count:
                left.append(f"{count} row(s) still in {table}")
    connection.rollback()
    return left


def organisation_names(connection, config: dict[str, Any], org_ids: list[str]) -> list[str]:
    schema = config["postgres"]["schema"]
    with connection.cursor() as cursor:
        statement = f"SELECT name FROM {schema}.organizations WHERE id = ANY(%(ids)s::uuid[])"
        LOG.info("SQL: %s", render_sql(cursor, statement, {"ids": org_ids}))
        cursor.execute(statement, {"ids": org_ids})
        names = [row[0] for row in cursor.fetchall()]
    connection.rollback()
    return names


# ------------------------------------------------------------------------ run

def run(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(str(args.log_level or config["logging"]["level"]).upper())
    target = config["target"]
    org_ids = [str(i) for i in (target.get("org_ids") or []) if i] + list(args.org_id or [])
    request_ids = [str(i) for i in (target.get("request_ids") or []) if i] + list(args.request_id or [])
    validate(config, org_ids, request_ids)
    mode = str(config["delete"]["mode"]).lower()
    needs_db = mode in ("auto", "postgres") or bool(request_ids)

    problems: list[str] = []
    connection = connect(config) if needs_db else None
    try:
        if request_ids:
            for org_id in resolve_requests(connection, config, request_ids):
                if org_id not in org_ids:
                    org_ids.append(org_id)
        # Captured now: after the delete nothing maps an org id back to its name.
        org_names = organisation_names(connection, config, org_ids) if connection and org_ids else []

        pending = list(org_ids)
        if mode in ("auto", "api") and pending:
            if args.dry_run:
                for org_id in pending:
                    LOG.info("DRY RUN — would DELETE %s", config["endpoints"]["cp_organisation"].format(org_id=org_id))
            else:
                token = actor_token(config)
                still = []
                for org_id in pending:
                    gone, detail = delete_via_api(config, token, org_id)
                    LOG.info("%s: %s", org_id, detail)
                    if gone:
                        continue
                    if mode == "api":
                        problems.append(f"{org_id}: {detail}")
                    else:
                        still.append(org_id)
                pending = still if mode == "auto" else []

        # The database pass also runs when the API took the organisation: the
        # API's delete is the bare row, and everything keyed on the org stays.
        if connection and (mode in ("auto", "postgres") or request_ids):
            LOG.info("%s: %d organisation(s), %d request(s) through the database",
                     "DRY RUN" if args.dry_run else "deleting", len(org_ids), len(request_ids))
            problems += delete_via_postgres(connection, config, org_ids, request_ids, args.dry_run)
            if config["delete"]["verify"] and not args.dry_run:
                problems += survivors(connection, config, org_ids, request_ids, org_names)
        elif pending:
            LOG.warning("delete.mode is api: rows keyed on %s stay in the database", ", ".join(pending))
    finally:
        if connection:
            connection.close()

    if org_ids:
        problems += detach_keycloak_users(config, org_ids, args.dry_run)

    if problems:
        for problem in problems:
            LOG.error("%s", problem)
        return EXIT_FAILED
    LOG.info("%d organisation(s), %d request(s) %s", len(org_ids), len(request_ids),
             "would be handled" if args.dry_run else "handled")
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config", nargs="?", type=Path,
        default=Path(__file__).with_name("organisation_deletion_config.json"),
        help="JSON config path (default: organisation_deletion_config.json beside this script)",
    )
    parser.add_argument("--org-id", action="append", help="an organisation to delete, in addition to target.org_ids")
    parser.add_argument("--request-id", action="append",
                        help="an approved org-create request whose organisation (and the request row) to delete")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen; the database pass runs and rolls back")
    parser.add_argument("--log-level", help="override logging.level from the config")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    try:
        return run(args.config, args)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001 - API refusals, connection failures
        LOG.error("%s", exc)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
