#!/usr/bin/env python3
"""Batch-delete files through the Files Connect owner-checked API.

Usage:
    python file_deletion.py file_deletion_config.json
    python file_deletion.py file_deletion_config.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

try:
    import requests
except ModuleNotFoundError:  # Allows --help and --dry-run before installation.
    requests = None  # type: ignore[assignment]


LOG = logging.getLogger("file_deletion")


class ConfigError(ValueError):
    """Raised when a required configuration value is absent or invalid."""


class ApiError(RuntimeError):
    """Raised when the file server rejects a request."""


def require_requests() -> None:
    if requests is None:
        raise ConfigError(
            "missing Python dependency 'requests'; run "
            "'python -m pip install -r scripts/requirements.txt'"
        )


def require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"'{field}' must be a non-empty string")
    return value.strip()


def response_detail(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:1_000] or "empty response"
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(body.get("message") or body)
    return str(body)


class TokenProvider:
    def __init__(self, config: dict[str, Any], verify_tls: bool, timeout: float):
        self.static_token = str(config.get("token", "")).strip()
        self.keycloak = config.get("keycloak") or {}
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.access_token = ""
        self.refresh_token = ""
        self.expires_at = datetime.min.replace(tzinfo=timezone.utc)

        if not self.static_token:
            if not isinstance(self.keycloak, dict):
                raise ConfigError("'keycloak' must be an object")
            for field in ("token_url", "client_id", "username", "password"):
                require_string(self.keycloak.get(field), f"keycloak.{field}")

    def get(self, force_refresh: bool = False) -> str:
        if self.static_token:
            return self.static_token
        now = datetime.now(timezone.utc)
        if not force_refresh and self.access_token and now < self.expires_at:
            return self.access_token

        if self.refresh_token:
            payload = {
                "grant_type": "refresh_token",
                "client_id": self.keycloak["client_id"],
                "refresh_token": self.refresh_token,
            }
        else:
            payload = {
                "grant_type": "password",
                "client_id": self.keycloak["client_id"],
                "username": self.keycloak["username"],
                "password": self.keycloak["password"],
            }
        client_secret = str(self.keycloak.get("client_secret", "")).strip()
        if client_secret:
            payload["client_secret"] = client_secret

        response = requests.post(
            self.keycloak["token_url"],
            data=payload,
            timeout=self.timeout,
            verify=self.verify_tls,
        )
        if response.status_code != 200 and payload["grant_type"] == "refresh_token":
            self.refresh_token = ""
            return self.get(force_refresh=True)
        if response.status_code != 200:
            raise ApiError(
                f"Keycloak authentication failed ({response.status_code}): "
                f"{response_detail(response)}"
            )
        data = response.json()
        self.access_token = require_string(data.get("access_token"), "access_token")
        self.refresh_token = str(data.get("refresh_token", self.refresh_token))
        lifetime = max(int(data.get("expires_in", 300)) - 60, 1)
        self.expires_at = now + timedelta(seconds=lifetime)
        return self.access_token


class FileServerClient:
    def __init__(self, config: dict[str, Any]):
        require_requests()
        base_url = require_string(config.get("base_url"), "base_url").rstrip("/")
        api_version = str(config.get("api_version", "v1")).strip("/")
        self.api_root = f"{base_url}/{api_version}" if api_version else base_url
        self.timeout = float(config.get("request_timeout_seconds", 120))
        self.verify_tls = bool(config.get("verify_tls", True))
        self.tokens = TokenProvider(config, self.verify_tls, self.timeout)
        self.session = requests.Session()

    def delete(self, databank_id: str, key: str) -> dict[str, Any]:
        databank = quote(databank_id, safe="")
        url = f"{self.api_root}/databanks/{databank}/files/delete"
        response: requests.Response | None = None
        for auth_attempt in range(2):
            response = self.session.post(
                url,
                json={"key": key},
                headers={"Authorization": f"Bearer {self.tokens.get(auth_attempt == 1)}"},
                timeout=self.timeout,
                verify=self.verify_tls,
            )
            if response.status_code != 401 or auth_attempt == 1:
                break
        assert response is not None
        if response.status_code != 200:
            raise ApiError(
                f"delete failed ({response.status_code}): {response_detail(response)}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ApiError("delete returned invalid JSON") from exc
        if not isinstance(body, dict) or body.get("success") is not True:
            raise ApiError(f"delete returned an unsuccessful response: {body}")
        data = body.get("data")
        return data if isinstance(data, dict) else {}


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigError("configuration root must be an object")
    return config


def parse_deletions(config: dict[str, Any]) -> list[tuple[str, str]]:
    entries = config.get("files")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("'files' must be a non-empty array")
    default_databank = str(config.get("databank_id", "")).strip()
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for index, entry in enumerate(entries):
        field = f"files[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"'{field}' must be an object")
        databank_id = require_string(
            entry.get("databank_id", default_databank), f"{field}.databank_id"
        )
        raw_key = require_string(entry.get("key"), f"{field}.key").replace("\\", "/")
        key_path = PurePosixPath(raw_key)
        key = str(key_path)
        if (
            key_path.is_absolute()
            or ".." in key_path.parts
            or (key_path.parts and key_path.parts[0].endswith(":"))
            or key in (".", "..")
        ):
            raise ConfigError(f"'{field}.key' must be a databank-relative object key")
        item = (databank_id, key)
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def run(config_path: Path, dry_run: bool) -> int:
    config = load_config(config_path)
    deletions = parse_deletions(config)
    if dry_run:
        for databank_id, key in deletions:
            LOG.info("Would delete %s/%s", databank_id, key)
        LOG.info("Dry-run summary: %d file(s), no changes made", len(deletions))
        return 0

    client = FileServerClient(config)
    failures = 0
    for databank_id, key in deletions:
        try:
            client.delete(databank_id, key)
            LOG.info("Deleted %s/%s", databank_id, key)
        except Exception as exc:
            failures += 1
            LOG.error("Failed to delete %s/%s: %s", databank_id, key, exc)
    LOG.info("Deletion summary: %d succeeded, %d failed", len(deletions) - failures, failures)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("file_deletion_config.json"),
        help="JSON config path (default: file_deletion_config.json beside this script)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show targets without deleting them"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    try:
        return run(args.config, args.dry_run)
    except (ConfigError, ApiError, OSError) as exc:
        LOG.error("Deletion failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
