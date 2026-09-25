#!/usr/bin/env python3
"""Unwind one consumer's compute role and credit state, by consumer.

Usage:
    python compute_credit_cleanup.py                       # compute_credit_cleanup_config.json beside this script
    python compute_credit_cleanup.py my_config.json
    python compute_credit_cleanup.py --username someone@example.invalid
    python compute_credit_cleanup.py --inventory-only      # read everything, change nothing
    python compute_credit_cleanup.py --dry-run             # print every call it would make
    python compute_credit_cleanup.py --revoke-role --delete-user

Granting the compute role and adding credits is the one journey whose teardown
the other scripts do not cover: `consumer_deletion.py` deletes the *account*,
and the account is not what is left behind. What is left behind is the money and
the role — so this script takes the consumer, not a list of ids, and walks every
API the platform offers over that account's compute/credit state:

    what                        endpoint                                          actor
    ----                        --------                                          -----
    balance                     GET    /iudx/v2/auth/user/credit/balance          consumer
    balance (any user)          GET    /iudx/v2/auth/admin/user/credit/balance/{id}  cos_admin
    zero the balance            PUT    /iudx/v2/auth/admin/user/credit/deduct     cos_admin
    own credit requests         GET    /iudx/v2/auth/user/credit/request          consumer
    delete a credit request     DELETE /iudx/v2/auth/user/credit/request/{id}     consumer
    own compute request         GET    /iudx/v2/auth/user/compute/requests        consumer
    ... any status, directly    DELETE FROM compute_role WHERE id AND user_id     postgres
    delete the compute request  DELETE /iudx/v2/auth/user/compute/requests/{id}   consumer
    the `compute` realm role    DELETE role-mappings/realm  (Keycloak Admin API)  admin client
    the account                 DELETE /iudx/v2/auth/user/delete                  consumer

`PUT /admin/user/credit/deduct` is the exact inverse of the add call — same
body, `{"user_id", "amount", "requested_at"}`, same cos_admin token — so zeroing
a balance is one deduct for whatever `balance` reports. `requested_at` doubles
as the idempotency key: the same triple twice answers 409 Duplicate transaction
request, which is why the timestamp defaults to now and is retried on 409.

What the API cannot do, and this script therefore only reports:

  * **A granted or rejected compute request** cannot be deleted through the
    API, and no endpoint takes the grant back (the cos_admin PUT accepts only
    granted or rejected). With the `postgres` block filled in
    (`clean.compute_via_database`, on by default) the script does what the
    DELETE endpoint does — one row out of compute_role — for any status, and
    needs no password for it.
  * **A granted or rejected credit request cannot be deleted.** The DELETE
    answers 400 "Only pending credit requests can be deleted". The row stays.
  * **Deducting does not remove the credit rows.** Each add and each deduct
    writes `credit_transactions`, and `user_credits` holds the balance — zeroed,
    but still there.
  * **The compute_role row.** Whether the DELETE removes it once the request is
    granted is a per-deployment question this script answers out loud, because
    `compute_role.user_id` is UNIQUE: while the row survives, that account can
    never request the compute role again, so a test account that keeps its row
    is burnt for the next run even though it looks clean.
  * **`kyc_transactions`** and the `kyc_verified` attribute the compute request
    needed in the first place (`--clear-kyc` handles the attribute).

Everything in that list is `database_sweep.py`'s job, and the run ends by
printing the `target.user_ids` to paste into its config.

Exit codes:
    0  every step the API can do is done (leftovers are reported, not failed)
    1  a step failed
    2  configuration error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# The sweep SQL lives with the harness, as for database_sweep.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ControlPlane_Workflow.cleanup import (  # noqa: E402
    SWEEP_STATEMENTS,
    _adapt_to_schema,
    _schema_columns,
)

try:
    import requests
except ModuleNotFoundError:  # allows --help before install
    requests = None  # type: ignore[assignment]

try:
    import psycopg2
except ModuleNotFoundError:  # only the compute request needs it
    psycopg2 = None  # type: ignore[assignment]

LOG = logging.getLogger("compute_credit_cleanup")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

# The tables that outlive every endpoint above, in the order database_sweep
# deletes them. Printed at the end so the leftovers are named, not implied.
DB_ONLY_TABLES = (
    "credit_transactions",
    "credit_requests",
    "user_credits",
    "kyc_transactions",
)

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
        # Only the role revoke, the KYC attribute and the username -> user_id
        # lookup need the Admin API. Leave them empty otherwise.
        "admin_client_id": "",
        "admin_client_secret": "",
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    # The consumer being cleaned up. A token skips the sign-in, but the
    # self-delete and the sign-in both need the password.
    "target": {
        "username": "",
        "password": "",
        "user_id": "",
        "token": "",
        # The record consumer_creation.py wrote: username, user_id, password
        # and compute_request_id, so a teardown needs no ids typed out by hand.
        "input_file": "",
        "require_input_file": False,
    },
    # Only the balance endpoints need this one.
    "actors": {
        "cos_admin": {"username": "", "password": "", "token": ""},
    },
    "clean": {
        # Deduct the whole balance through the admin endpoint.
        "zero_balance": True,
        # Delete every credit request the account owns that is still pending.
        "credit_requests": True,
        # Delete the compute request — pending, or granted where the platform
        # allows it.
        "compute_requests": True,
        # List and delete the compute request in compute_role directly — what
        # the DELETE endpoint does, for a granted or rejected row too, and
        # without the account's password. Needs the `postgres` block; with it
        # empty, the API is used and only a pending request can go.
        "compute_via_database": True,
        # Then delete what no endpoint removes — credit_transactions,
        # credit_requests, user_credits, kyc_transactions — for this user only,
        # with database_sweep's own SQL. Needs the `postgres` block.
        "sweep_credit_rows": True,
        # Keycloak: take the `compute` realm role back off the account. Off by
        # default because it changes an account that is otherwise untouched.
        "revoke_compute_role": False,
        "compute_role_name": "compute",
        # Keycloak: unset the attribute the KYC gate reads.
        "clear_kyc_attribute": False,
        "kyc_attribute": "kyc_verified",
        # Delete the account itself. consumer_deletion.py is the fuller tool —
        # it verifies, and falls back to the Admin API when the platform
        # refuses — so this is here for a one-command teardown, not instead.
        "delete_user": False,
        # `requested_at` on the deduct. Empty means now. The platform treats
        # (user, amount, requested_at) as an idempotency key and answers 409 to
        # a repeat, so a fixed value here can only be used once.
        "requested_at": "",
        "deduct_retries": 2,
        # The own-credit-request listing is paged; phase-1 collections start at
        # 0, later ones at 1.
        "list_page_start": 1,
        "list_page_size": 50,
        "list_max_pages": 20,
        # Read everything back once the deletes are done.
        "verify": True,
    },
    # Only the compute request needs the database — database_sweep's settings.
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
    "endpoints": {
        "kc_token": "/realms/{realm}/protocol/openid-connect/token",
        "kc_users": "/admin/realms/{realm}/users",
        "kc_user": "/admin/realms/{realm}/users/{user_id}",
        "kc_realm_role": "/admin/realms/{realm}/roles/{role}",
        "kc_role_mappings": "/admin/realms/{realm}/users/{user_id}/role-mappings/realm",
        "cp_user_info": "/iudx/v2/auth/user",
        "cp_own_credit_balance": "/iudx/v2/auth/user/credit/balance",
        "cp_admin_credit_balance": "/iudx/v2/auth/admin/user/credit/balance/{user_id}",
        "cp_admin_credit_deduct": "/iudx/v2/auth/admin/user/credit/deduct",
        "cp_own_credit_requests": "/iudx/v2/auth/user/credit/request",
        "cp_credit_request": "/iudx/v2/auth/user/credit/request/{req_id}",
        "cp_own_compute_requests": "/iudx/v2/auth/user/compute/requests",
        "cp_compute_request": "/iudx/v2/auth/user/compute/requests/{req_id}",
        "cp_user_delete": "/iudx/v2/auth/user/delete",
    },
    "logging": {
        "level": "INFO",
        "print_responses": False,
        "response_preview_chars": 1000,
    },
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


def resolve_path(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def endpoint(config: dict[str, Any], name: str, **fields: str) -> str:
    template = config["endpoints"].get(name)
    if not isinstance(template, str) or not template:
        raise ConfigError(f"'endpoints.{name}' must be a non-empty string")
    return template.format(**fields)


# ----------------------------------------------------------------------- http

class Http:
    """One host, JSON in and JSON out, with the status handed back."""

    def __init__(self, base_url: str, timeout: float, verify_tls: bool, log_conf: dict[str, Any]):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_tls
        self.log_conf = log_conf

    def request(self, method: str, path: str, label: str, token: str = "",
                json_body: Any = None, params: dict[str, Any] | None = None,
                form: dict[str, str] | None = None,
                expect: tuple[int, ...] = (200,)) -> tuple[int, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        LOG.debug("%s: %s %s %s", label, method, url, json_body or params or "")
        try:
            response = self.session.request(
                method, url, json=json_body, data=form, params=params,
                headers=headers, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(f"{label}: {method} {url} failed: {exc}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = response.text
        if self.log_conf.get("print_responses"):
            text = payload if isinstance(payload, str) else json.dumps(payload)
            LOG.info("%s -> %s %s", label, response.status_code,
                     text[: int(self.log_conf.get("response_preview_chars") or 1000)])
        if response.status_code not in expect:
            text = payload if isinstance(payload, str) else json.dumps(payload)
            raise ApiError(
                f"{label}: {method} {url} returned {response.status_code}, "
                f"expected {' or '.join(str(code) for code in expect)}: {text[:400]}"
            )
        return response.status_code, payload


def detail_of(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload.get("title") or "")
    return str(payload or "")


def result_of(payload: Any) -> Any:
    return payload.get("result") if isinstance(payload, dict) else None


# ------------------------------------------------------------------- keycloak

class Keycloak:
    """Token grants, plus the few Admin API calls the optional steps need."""

    def __init__(self, config: dict[str, Any]):
        block = config["keycloak"]
        self.config = config
        self.realm = str(block.get("realm") or "")
        self.user_client_id = str(block.get("user_client_id") or "")
        self.user_client_secret = str(block.get("user_client_secret") or "")
        self.admin_client_id = str(block.get("admin_client_id") or "")
        self.admin_client_secret = str(block.get("admin_client_secret") or "")
        self.http = (
            Http(str(block.get("url") or ""), float(block.get("timeout_seconds") or 30),
                 bool(block.get("verify_tls", True)), config["logging"])
            if block.get("url") else None
        )
        self._admin_token = ""

    @property
    def available(self) -> bool:
        return self.http is not None and bool(self.realm)

    @property
    def admin_available(self) -> bool:
        return self.available and bool(self.admin_client_id)

    def path(self, name: str, **fields: str) -> str:
        return endpoint(self.config, name, realm=self.realm, **fields)

    def user_token(self, username: str, password: str) -> str:
        if not self.available:
            raise ConfigError("keycloak.url and keycloak.realm are needed to sign in")
        form = {
            "grant_type": "password",
            "client_id": self.user_client_id,
            "username": username,
            "password": password,
        }
        if self.user_client_secret:
            form["client_secret"] = self.user_client_secret
        _, payload = self.http.request(  # type: ignore[union-attr]
            "POST", self.path("kc_token"), f"token for {username}", form=form,
        )
        return payload["access_token"]

    def admin_token(self) -> str:
        if not self.admin_available:
            raise ConfigError(
                "keycloak.admin_client_id / admin_client_secret are needed for this step"
            )
        if self._admin_token:
            return self._admin_token
        _, payload = self.http.request(  # type: ignore[union-attr]
            "POST", self.path("kc_token"), "keycloak admin token",
            form={
                "grant_type": "client_credentials",
                "client_id": self.admin_client_id,
                "client_secret": self.admin_client_secret,
            },
        )
        self._admin_token = payload["access_token"]
        return self._admin_token

    def admin(self, method: str, path: str, label: str, json_body: Any = None,
              params: dict[str, Any] | None = None,
              expect: tuple[int, ...] = (200,)) -> tuple[int, Any]:
        return self.http.request(  # type: ignore[union-attr]
            method, path, label, token=self.admin_token(),
            json_body=json_body, params=params, expect=expect,
        )

    def find_user(self, username: str) -> dict[str, Any] | None:
        _, matches = self.admin(
            "GET", self.path("kc_users"), f"find user {username}",
            params={"username": username, "exact": "true"},
        )
        return matches[0] if isinstance(matches, list) and matches else None

    def realm_roles(self, user_id: str) -> list[str]:
        _, mapped = self.admin(
            "GET", self.path("kc_role_mappings", user_id=user_id), "read realm roles"
        )
        return sorted(role["name"] for role in mapped) if isinstance(mapped, list) else []

    def remove_realm_role(self, user_id: str, role_name: str) -> None:
        _, role = self.admin(
            "GET", self.path("kc_realm_role", role=role_name), f"read role {role_name}"
        )
        self.admin(
            "DELETE", self.path("kc_role_mappings", user_id=user_id),
            f"revoke {role_name}", json_body=[{"id": role["id"], "name": role["name"]}],
            expect=(204,),
        )

    def clear_attribute(self, user_id: str, name: str) -> bool:
        """Drop one attribute, carrying the rest of the representation back.

        Keycloak replaces the whole attribute map on write, and this realm's
        user profile makes email required — so the profile fields go back
        unchanged and the server-managed ones are left out, the same way
        consumer_creation.py sets the attribute in the first place.
        """
        _, record = self.admin(
            "GET", self.path("kc_user", user_id=user_id), "read the account to update"
        )
        attributes = dict(record.get("attributes") or {})
        if name not in attributes:
            return False
        attributes.pop(name)
        body = {
            key: record[key]
            for key in ("username", "email", "firstName", "lastName",
                        "enabled", "emailVerified", "requiredActions")
            if key in record
        }
        body["attributes"] = attributes
        self.admin(
            "PUT", self.path("kc_user", user_id=user_id), f"unset {name}",
            json_body=body, expect=(200, 204),
        )
        return True


# -------------------------------------------------------------------- target

def resolve_target(config: dict[str, Any], config_path: Path,
                   args: argparse.Namespace) -> dict[str, str]:
    """Who to clean up: the command line, then the config, then the handoff."""
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

    username = (args.username or str(target.get("username") or "")).strip()
    user_id = (args.user_id or str(target.get("user_id") or "")).strip()

    # The record describes one account; if either identifier given here names a
    # different one, the rest of that file belongs to somebody else.
    recorded_user = str(record.get("username") or "").strip()
    recorded_id = str(record.get("user_id") or "").strip()
    if record and ((username and recorded_user and username != recorded_user)
                   or (user_id and recorded_id and user_id != recorded_id)):
        LOG.warning(
            "%s describes %s (%s) — ignoring it and using what was asked for",
            input_file, recorded_user or "?", recorded_id or "?",
        )
        record = {}

    resolved = {
        "username": username or str(record.get("username") or "").strip(),
        "user_id": user_id or str(record.get("user_id") or "").strip(),
        "password": (args.password or str(target.get("password") or "")
                     or str(record.get("password") or "")).strip(),
        "token": str(target.get("token") or "").strip(),
        "compute_request_id": str(record.get("compute_request_id") or "").strip(),
    }
    if not resolved["username"] and not resolved["user_id"]:
        raise ConfigError(
            "no consumer to clean up: pass --username, set 'target.username' or "
            "'target.user_id', or point 'target.input_file' at the file "
            "consumer_creation.py wrote"
        )
    return resolved


def consumer_token(config: dict[str, Any], kc: Keycloak, target: dict[str, str]) -> str:
    """The account's own token, or "" when there are no credentials for it.

    Half the run needs it — the credit and compute request listings are the
    account's own, and only the account may delete them or delete itself. The
    other half does not: reading a balance and deducting it are the cos_admin's
    endpoints, keyed on a user id, and so are the Keycloak steps. Cleaning up
    an account whose password nobody has is the common case after a hand-made
    credit add, so that half still runs and the rest is reported.
    """
    if target["token"]:
        return target["token"]
    if not target["username"] or not target["password"]:
        LOG.warning(
            "No password for the account: the balance and the Keycloak steps "
            "still run, the account's own requests cannot be listed or deleted"
        )
        return ""
    try:
        return kc.user_token(target["username"], target["password"])
    except ApiError as exc:
        # The database steps and the cos_admin steps do not need it; only the
        # credit-request API and the self-delete are lost.
        LOG.warning("Could not sign in as the account (%s) — carrying on without it", exc)
        return ""


def cos_admin_token(config: dict[str, Any], kc: Keycloak) -> str:
    block = config["actors"].get("cos_admin") or {}
    if block.get("token"):
        return str(block["token"])
    username = require(block.get("username"), "actors.cos_admin.username")
    password = require(block.get("password"), "actors.cos_admin.password")
    return kc.user_token(username, password)


# --------------------------------------------------------------------- state

class Run:
    """What the run found, what it changed, and what it could not reach."""

    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.done: list[str] = []
        self.leftovers: list[str] = []
        self.problems: list[str] = []
        # Ids already named as leftovers, so the verify pass reports what the
        # delete pass did not already say out loud.
        self.named: set[str] = set()

    def did(self, message: str) -> None:
        self.done.append(message)
        LOG.info("%s%s", "DRY RUN — would " if self.dry_run else "", message)

    def left(self, message: str, key: str = "") -> None:
        if key:
            if key in self.named:
                return
            self.named.add(key)
        self.leftovers.append(message)
        LOG.warning("Left behind: %s", message)

    def failed(self, message: str) -> None:
        self.problems.append(message)
        LOG.error("%s", message)


# --------------------------------------------------------------------- steps

def read_balance(config: dict[str, Any], cp: Http, run: Run, user_id: str,
                 own_token: str, admin_token: str) -> float | None:
    """The balance, read as the cos_admin when there is one, else as the account."""
    try:
        if admin_token and user_id:
            _, payload = cp.request(
                "GET", endpoint(config, "cp_admin_credit_balance", user_id=user_id),
                "read the balance (cos_admin)", token=admin_token,
            )
        elif own_token:
            _, payload = cp.request(
                "GET", endpoint(config, "cp_own_credit_balance"),
                "read the balance (own)", token=own_token,
            )
        else:
            return None
    except ApiError as exc:
        run.failed(f"could not read the balance: {exc}")
        return None
    result = result_of(payload) or {}
    balance = result.get("balance")
    if not isinstance(balance, (int, float)):
        run.failed(f"the balance response carried no numeric 'balance': {payload}")
        return None
    LOG.info("Balance is %s (isValid=%s)", balance, result.get("isValid"))
    return float(balance)


def stamp(offset_seconds: int = 0) -> str:
    """`requested_at` in the shape the credit endpoints take: no timezone."""
    moment = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=offset_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def zero_balance(config: dict[str, Any], cp: Http, run: Run, user_id: str,
                 balance: float, admin_token: str) -> None:
    """One deduct for the whole balance — the inverse of the add call.

    409 means the platform already has a transaction with this
    (user, amount, requested_at); a fresh timestamp is a different transaction,
    so the retry is the fix rather than a repeat.
    """
    clean = config["clean"]
    if balance <= 0:
        LOG.info("Balance is already %s — nothing to deduct", balance)
        return
    fixed = str(clean.get("requested_at") or "").strip()
    retries = max(int(clean.get("deduct_retries") or 0), 0)
    for attempt in range(retries + 1):
        requested_at = fixed if (fixed and attempt == 0) else stamp(attempt)
        body = {"user_id": user_id, "amount": float(balance), "requested_at": requested_at}
        if run.dry_run:
            run.did(f"deduct {balance} from {user_id} ({json.dumps(body)})")
            return
        status, payload = cp.request(
            "PUT", endpoint(config, "cp_admin_credit_deduct"),
            f"deduct {balance}", token=admin_token, json_body=body,
            expect=(200, 400, 409),
        )
        if status == 200:
            result = result_of(payload) or {}
            run.did(
                f"deducted {balance} — updatedBalance={result.get('updatedBalance')}, "
                f"transaction {result.get('id')}"
            )
            return
        if status == 409:
            LOG.warning(
                "Deduct answered 409 (%s) for requested_at=%s — retrying with a new timestamp",
                detail_of(payload), requested_at,
            )
            continue
        run.failed(f"deduct refused: 400 {detail_of(payload)}")
        return
    run.failed(f"deduct kept answering 409 after {retries + 1} attempts")


def own_credit_requests(config: dict[str, Any], cp: Http, token: str) -> list[dict[str, Any]]:
    """Every credit request the account owns, paged to the end."""
    clean = config["clean"]
    size = int(clean.get("list_page_size") or 50)
    start = int(clean.get("list_page_start") or 1)
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(start, start + int(clean.get("list_max_pages") or 20)):
        # An account with no pending request answers 404 "No pending credit
        # request found" rather than an empty page.
        status, payload = cp.request(
            "GET", endpoint(config, "cp_own_credit_requests"),
            f"list own credit requests (page {page})", token=token,
            params={"page": page, "size": size, "sort": "createdAt", "status": ""},
            expect=(200, 404),
        )
        if status == 404:
            break
        rows = result_of(payload)
        if not isinstance(rows, list) or not rows:
            break
        fresh = [row for row in rows if str(row.get("id") or "") not in seen]
        for row in fresh:
            seen.add(str(row.get("id") or ""))
        found.extend(fresh)
        if len(rows) < size or not fresh:
            break
    return found


def own_compute_requests(config: dict[str, Any], cp: Http, token: str) -> list[dict[str, Any]]:
    """The account's compute request.

    `compute_role.user_id` is UNIQUE, so this answers with one object rather
    than a list — but a deployment that ever relaxes that would answer with a
    list, and both are accepted here.
    """
    status, payload = cp.request(
        "GET", endpoint(config, "cp_own_compute_requests"),
        "list own compute requests", token=token, expect=(200, 404),
    )
    if status == 404:
        return []
    result = result_of(payload)
    if isinstance(result, dict):
        return [result]
    return [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []


def database_ready(config: dict[str, Any]) -> bool:
    """True when the `postgres` block is filled in and psycopg2 is installed."""
    pg = config["postgres"]
    return psycopg2 is not None and all(
        str(pg.get(key) or "").strip() for key in ("host", "database", "user", "password")
    )


def pg_schema(config: dict[str, Any]) -> str:
    schema = str(config["postgres"].get("schema") or "aaa")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ConfigError(f"postgres.schema {schema!r} is not a plain identifier")
    return schema


def pg_connect(config: dict[str, Any]):
    pg = config["postgres"]
    return psycopg2.connect(
        host=pg["host"], port=int(pg.get("port") or 5432), dbname=pg["database"],
        user=pg["user"], password=pg["password"], sslmode=pg.get("sslmode") or "prefer",
        connect_timeout=int(pg.get("connect_timeout_seconds") or 10),
    )


def db_compute_requests(config: dict[str, Any], user_id: str) -> list[dict[str, Any]]:
    """The account's compute_role rows, read straight from the table."""
    connection = pg_connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT id, status FROM {pg_schema(config)}.compute_role WHERE user_id = %s",
                (user_id,),
            )
            return [{"id": str(row[0]), "status": row[1]} for row in cursor.fetchall()]
    finally:
        connection.close()


def db_delete_compute_requests(config: dict[str, Any], run: Run, user_id: str,
                               rows: list[dict[str, Any]]) -> None:
    """What DELETE /user/compute/requests/{id} does, for any status.

    The handler (ComputeRoleHandler.deletePendingComputeRequests) checks the
    row is the caller's and pending, then deletes it — computeRoleDAO.delete,
    one row from compute_role — and emits an audit event. The ownership check
    is kept here as `AND user_id`; the pending check is what this skips, since
    no endpoint takes a grant back. The audit event is a log, not state, and
    is not reproduced.
    """
    schema = pg_schema(config)
    for row in rows:
        req_id = str(row.get("id") or "")
        if not req_id:
            continue
        label = f"compute request {req_id} (status {row.get('status') or '?'})"
        if run.dry_run:
            run.did(f"delete {label} from {schema}.compute_role")
            continue
        try:
            connection = pg_connect(config)
            try:
                with connection, connection.cursor() as cursor:
                    cursor.execute(
                        f"DELETE FROM {schema}.compute_role WHERE id = %s AND user_id = %s",
                        (req_id, user_id),
                    )
                    deleted = cursor.rowcount
            finally:
                connection.close()
        except psycopg2.Error as exc:
            run.failed(f"{label}: {exc}")
            continue
        if deleted:
            run.did(f"deleted {label} from {schema}.compute_role")
        else:
            run.did(f"{label} was already gone")


def delete_requests(config: dict[str, Any], cp: Http, run: Run, token: str,
                    rows: list[dict[str, Any]], kind: str, path_name: str) -> None:
    """DELETE each request, and report the ones the platform will not delete.

    A 400 here is not a failure: the platform only deletes a *pending* request,
    and a granted one staying put is the documented behaviour. It is a leftover
    for database_sweep, and for the compute request it is also what burns the
    account for the next run.
    """
    for row in rows:
        req_id = str(row.get("id") or "")
        state = str(row.get("status") or "?")
        if not req_id:
            continue
        label = f"{kind} {req_id} (status {state})"
        if run.dry_run:
            run.did(f"delete {label}")
            continue
        try:
            status, payload = cp.request(
                "DELETE", endpoint(config, path_name, req_id=req_id),
                f"delete {label}", token=token, expect=(200, 204, 400, 403, 404),
            )
        except ApiError as exc:
            run.failed(f"{label}: {exc}")
            continue
        if status in (200, 204):
            run.did(f"deleted {label}")
        elif status == 404:
            run.did(f"{label} was already gone")
        else:
            run.left(
                f"{label} — the API refused to delete it ({status} "
                f"{detail_of(payload) or 'no detail'}); the row stays until database_sweep",
                key=req_id,
            )


def revoke_role(config: dict[str, Any], kc: Keycloak, run: Run, user_id: str) -> None:
    role = str(config["clean"].get("compute_role_name") or "compute")
    if run.dry_run:
        run.did(f"revoke the {role} realm role from {user_id}")
        return
    try:
        roles = kc.realm_roles(user_id)
        if role not in roles:
            LOG.info("The account does not carry the %s role (has: %s)", role, ", ".join(roles))
            return
        kc.remove_realm_role(user_id, role)
    except (ApiError, ConfigError) as exc:
        run.failed(f"could not revoke the {role} role: {exc}")
        return
    run.did(f"revoked the {role} realm role")


def clear_kyc(config: dict[str, Any], kc: Keycloak, run: Run, user_id: str) -> None:
    name = str(config["clean"].get("kyc_attribute") or "kyc_verified")
    if run.dry_run:
        run.did(f"unset the {name} attribute on {user_id}")
        return
    try:
        cleared = kc.clear_attribute(user_id, name)
    except (ApiError, ConfigError) as exc:
        run.failed(f"could not unset {name}: {exc}")
        return
    if cleared:
        run.did(f"unset the {name} attribute")
    else:
        LOG.info("The account carries no %s attribute", name)


def delete_account(config: dict[str, Any], cp: Http, run: Run,
                   token: str, username: str) -> None:
    if run.dry_run:
        run.did(f"self-delete {username or 'the account'}")
        return
    try:
        status, payload = cp.request(
            "DELETE", endpoint(config, "cp_user_delete"),
            f"self-delete {username or 'the account'}", token=token,
            expect=(200, 204, 400, 403, 404),
        )
    except ApiError as exc:
        run.failed(f"self-delete failed: {exc}")
        return
    if status in (200, 204):
        run.did(f"deleted the account {username or ''}".strip())
    elif status == 404:
        run.did("the account was already gone")
    else:
        run.left(
            f"the account — self-delete answered {status} "
            f"({detail_of(payload) or 'no detail'}); the platform refuses org admins "
            f"and platform admins, so use user_creation/consumer/deletion or "
            f"keycloak_user_sweep"
        )


def sweep_credit_rows(config: dict[str, Any], run: Run, user_id: str) -> bool | None:
    """database_sweep's statements for DB_ONLY_TABLES, keyed on this one user.

    Same SQL and the same children-first order as database_sweep.py, imported
    from ControlPlane_Workflow/cleanup.py, but only these tables — the
    account's policies, requests and memberships are not this script's.
    Returns True when every statement ran, None when there was nothing to run.
    """
    schema = pg_schema(config)
    params = {"user_ids": [user_id], "org_ids": [], "item_ids": [], "pattern": ""}
    statements = [(table, sql) for table, sql in SWEEP_STATEMENTS if table in DB_ONLY_TABLES]
    connection = pg_connect(config)
    ok = True
    try:
        with connection.cursor() as cursor:
            columns = _schema_columns(cursor, schema)
            for table, template in statements:
                sql = template.format(schema=schema)
                if columns is not None:
                    sql, _ = _adapt_to_schema(sql, schema, columns)
                    if sql is None:
                        LOG.info("%s: not on this deployment, skipped", table)
                        continue
                cursor.execute("SAVEPOINT stmt")
                try:
                    cursor.execute(sql, params)
                    count = cursor.rowcount
                except psycopg2.Error as exc:
                    cursor.execute("ROLLBACK TO SAVEPOINT stmt")
                    run.failed(f"sweep {table}: {exc}")
                    ok = False
                    continue
                cursor.execute("RELEASE SAVEPOINT stmt")
                if count:
                    run.did(f"{'sweep' if run.dry_run else 'swept'} {count} row(s) "
                            f"from {schema}.{table}")
                else:
                    LOG.info("%s: nothing to sweep", table)
        # A dry run executes every DELETE in one transaction for the counts,
        # then rolls it back.
        if run.dry_run:
            connection.rollback()
        else:
            connection.commit()
    finally:
        connection.close()
    return ok


# ----------------------------------------------------------------------- run

def report(run: Run, user_id: str, username: str, compute_left: bool | None,
           swept: bool) -> None:
    """compute_left: True/False once compute_role was read back, None when it was not."""
    LOG.info("%s", "-" * 70)
    if run.done:
        LOG.info("Done (%d):", len(run.done))
        for line in run.done:
            LOG.info("  - %s", line)
    if run.leftovers:
        LOG.info("The API could not remove (%d):", len(run.leftovers))
        for line in run.leftovers:
            LOG.info("  - %s", line)
    if not swept:
        LOG.info(
            "Rows no endpoint touches — %s — were not swept (clean.sweep_credit_rows "
            "and the postgres block). database_sweep.py takes them with:\n"
            "  \"target\": { \"user_ids\": [\"%s\"] }",
            ", ".join(DB_ONLY_TABLES), user_id or "<user id>",
        )
    who = username or user_id or "the account"
    if compute_left:
        LOG.warning(
            "compute_role still holds a row for %s: that account cannot request "
            "the compute role again — compute_role.user_id is UNIQUE.", who,
        )
    elif compute_left is False:
        LOG.info("No compute_role row left for %s — it can request the compute role again.", who)


def run_cleanup(config_path: Path, args: argparse.Namespace) -> int:
    config = load_config(config_path)
    logging.getLogger().setLevel(
        str(args.log_level or config["logging"].get("level") or "INFO").upper()
    )
    if requests is None:
        raise ConfigError(
            "the 'requests' package is not installed: pip install -r ../requirements.txt"
        )

    clean = config["clean"]
    for flag, key in (("revoke_role", "revoke_compute_role"), ("clear_kyc", "clear_kyc_attribute"),
                      ("delete_user", "delete_user")):
        if getattr(args, flag, False):
            clean[key] = True
    if args.inventory_only:
        for key in ("zero_balance", "credit_requests", "compute_requests", "sweep_credit_rows",
                    "revoke_compute_role", "clear_kyc_attribute", "delete_user",
                    "verify"):
            clean[key] = False

    target = resolve_target(config, config_path, args)
    cp_conf = config["control_plane"]
    cp = Http(require(cp_conf.get("base_url"), "control_plane.base_url"),
              float(cp_conf.get("timeout_seconds") or 30),
              bool(cp_conf.get("verify_tls", True)), config["logging"])
    kc = Keycloak(config)
    run = Run(bool(args.dry_run))

    token = consumer_token(config, kc, target)
    LOG.info("Cleaning up %s", target["username"] or target["user_id"])

    # The user id is what every admin endpoint and the database sweep key on.
    user_id = target["user_id"]
    if not user_id and token:
        try:
            _, info = cp.request(
                "GET", endpoint(config, "cp_user_info"), "read own user info", token=token
            )
            result = result_of(info) or (info if isinstance(info, dict) else {})
            user_id = str(result.get("sub") or result.get("userId")
                          or result.get("user_id") or "")
        except ApiError as exc:
            LOG.debug("could not read own user info: %s", exc)
    if not user_id and kc.admin_available and target["username"]:
        found = kc.find_user(target["username"])
        user_id = str((found or {}).get("id") or "")
    if user_id:
        LOG.info("User id is %s", user_id)

    # ---------------------------------------------------------- inventory
    admin_token = ""
    cos_admin = config["actors"].get("cos_admin") or {}
    if cos_admin.get("token") or cos_admin.get("username"):
        try:
            admin_token = cos_admin_token(config, kc)
        except (ApiError, ConfigError) as exc:
            LOG.warning("No cos_admin token (%s) — the balance is read as the account", exc)

    balance = read_balance(config, cp, run, user_id, token, admin_token)

    # compute_role straight from the table when the database is configured:
    # no password needed, and a granted row can be deleted too.
    compute_in_db = bool(clean.get("compute_via_database", True)) and database_ready(config)
    if compute_in_db and not user_id:
        LOG.warning("compute_via_database needs the user id — using the API instead")
        compute_in_db = False

    credits: list[dict[str, Any]] = []
    computes: list[dict[str, Any]] = []
    if token:
        try:
            credits = own_credit_requests(config, cp, token)
        except ApiError as exc:
            run.failed(f"could not list the credit requests: {exc}")
    if compute_in_db:
        try:
            computes = db_compute_requests(config, user_id)
        except psycopg2.Error as exc:
            run.failed(f"could not read compute_role: {exc}")
    elif token:
        try:
            computes = own_compute_requests(config, cp, token)
        except ApiError as exc:
            run.failed(f"could not list the compute requests: {exc}")
    if target["compute_request_id"] and not any(
        str(row.get("id") or "") == target["compute_request_id"] for row in computes
    ):
        computes.append({"id": target["compute_request_id"], "status": "?"})

    LOG.info(
        "Found: balance=%s, %d credit request(s) %s, %d compute request(s) %s",
        "unknown" if balance is None else balance,
        len(credits), [str(row.get("status")) for row in credits],
        len(computes), [str(row.get("status")) for row in computes],
    )
    if kc.admin_available and user_id:
        try:
            LOG.info("Realm roles: %s", ", ".join(kc.realm_roles(user_id)) or "none")
        except (ApiError, ConfigError) as exc:
            LOG.debug("could not read the realm roles: %s", exc)

    # -------------------------------------------------------------- clean
    if clean.get("zero_balance", True) and balance is not None and balance > 0:
        if not admin_token:
            run.left(
                f"a balance of {balance} — only the cos_admin may deduct it; fill "
                f"actors.cos_admin in the config"
            )
        elif not user_id:
            run.left(f"a balance of {balance} — the deduct needs the user id")
        else:
            zero_balance(config, cp, run, user_id, balance, admin_token)

    sweep_in_db = bool(clean.get("sweep_credit_rows", True)) and database_ready(config) and bool(user_id)
    # The sweep below deletes every credit_requests row for the user, so the
    # API listing is only missed when there is no sweep.
    if not token and ((clean.get("credit_requests", True) and not sweep_in_db)
                      or (clean.get("compute_requests", True) and not compute_in_db)):
        run.left(
            "the account's "
            + " and ".join(kind for kind, db in (("credit", sweep_in_db), ("compute", compute_in_db))
                           if not db)
            + " requests — listing and deleting them is the account's own "
            "endpoint. Give it a password or a token, or delete known ids with "
            "artefact_deletion.py"
        )
    if clean.get("credit_requests", True) and credits:
        delete_requests(config, cp, run, token, credits, "credit request", "cp_credit_request")
    if clean.get("compute_requests", True) and computes:
        if compute_in_db:
            db_delete_compute_requests(config, run, user_id, computes)
        else:
            delete_requests(config, cp, run, token, computes, "compute request",
                            "cp_compute_request")

    # Last of the database work: after the deduct, which itself writes a
    # credit_transactions row, and after the requests are gone.
    swept = False
    if clean.get("sweep_credit_rows", True):
        if not user_id:
            run.failed("sweep_credit_rows needs the user id")
        elif not database_ready(config):
            LOG.warning("sweep_credit_rows needs the postgres block — the credit rows stay")
        else:
            try:
                swept = bool(sweep_credit_rows(config, run, user_id))
            except psycopg2.Error as exc:
                run.failed(f"could not sweep the credit rows: {exc}")

    if clean.get("revoke_compute_role", False):
        if user_id and kc.admin_available:
            revoke_role(config, kc, run, user_id)
        else:
            run.failed("revoke_compute_role needs the user id and keycloak.admin_client_id")
    if clean.get("clear_kyc_attribute", False):
        if user_id and kc.admin_available:
            clear_kyc(config, kc, run, user_id)
        else:
            run.failed("clear_kyc_attribute needs the user id and keycloak.admin_client_id")

    # ------------------------------------------------------------- verify
    compute_left: bool | None = None
    if clean.get("verify", True) and not run.dry_run and (token or user_id):
        left_balance = read_balance(config, cp, run, user_id, token, admin_token)
        if left_balance not in (None, 0, 0.0):
            run.left(f"a balance of {left_balance}")
        try:
            still_credits = own_credit_requests(config, cp, token) if token else []
            if compute_in_db:
                still_computes = db_compute_requests(config, user_id)
            else:
                still_computes = own_compute_requests(config, cp, token) if token else []
        except (ApiError, psycopg2.Error if psycopg2 else ApiError) as exc:
            run.failed(f"could not read the requests back: {exc}")
        else:
            for row in still_credits:
                run.left(
                    f"credit_requests row {row.get('id')} (status {row.get('status')})",
                    key=str(row.get("id") or ""),
                )
            if compute_in_db or token:
                compute_left = bool(still_computes)
            for row in still_computes:
                run.left(
                    f"compute_role row {row.get('id')} (status {row.get('status')})",
                    key=str(row.get("id") or ""),
                )

    # The account goes last: once it is deleted nothing above can be read.
    if clean.get("delete_user", False):
        if token:
            delete_account(config, cp, run, token, target["username"])
        else:
            run.failed("delete_user needs the account's own password or token")

    report(run, user_id, target["username"], compute_left, swept)
    return EXIT_FAILED if run.problems else EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "config", nargs="?", type=Path,
        default=Path(__file__).with_name("compute_credit_cleanup_config.json"),
        help="JSON config path (default: compute_credit_cleanup_config.json beside this script)",
    )
    parser.add_argument("--username", help="the consumer to clean up")
    parser.add_argument("--password", help="that account's password")
    parser.add_argument("--user-id", dest="user_id", help="its Keycloak user id")
    parser.add_argument("--inventory-only", action="store_true",
                        help="read the balance and the requests, change nothing")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every call that would be made")
    parser.add_argument("--revoke-role", dest="revoke_role", action="store_true",
                        help="also take the compute realm role off the account")
    parser.add_argument("--clear-kyc", dest="clear_kyc", action="store_true",
                        help="also unset the kyc_verified attribute")
    parser.add_argument("--delete-user", dest="delete_user", action="store_true",
                        help="also self-delete the account when everything else is done")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR")
    args = parser.parse_args()

    logging.basicConfig(format="%(asctime)s %(levelname)-7s %(message)s", level=logging.INFO)
    try:
        return run_cleanup(args.config, args)
    except ConfigError as exc:
        LOG.error("Configuration error: %s", exc)
        return EXIT_CONFIG
    except ApiError as exc:
        LOG.error("%s", exc)
        return EXIT_FAILED
    except KeyboardInterrupt:
        LOG.error("Interrupted")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
