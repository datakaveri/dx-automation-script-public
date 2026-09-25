#!/usr/bin/env python3
"""Batch-onboard files through the Files Connect multipart-upload API.

Usage:
    python file_creation.py file_creation_config.json

The script uploads each configured file and can optionally start and monitor a
zip/report processing job for every affected databank. It never sends the API
bearer token to a storage-provider presigned URL.
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

try:
    import requests
except ModuleNotFoundError:  # Allows --help/config validation before installation.
    requests = None  # type: ignore[assignment]


LOG = logging.getLogger("file_creation")
MIB = 1024 * 1024
MAX_MULTIPART_PARTS = 10_000
MIN_MULTIPART_PART_SIZE = 5 * MIB
TERMINAL_JOB_STATUSES = {"completed", "failed"}


class ConfigError(ValueError):
    """Raised when a required configuration value is absent or invalid."""


class ApiError(RuntimeError):
    """Raised when the file server or a presigned URL rejects a request."""


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
    """Provides either a configured bearer token or a refreshable Keycloak token."""

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

        payload: dict[str, str]
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
        self.max_retries = max(int(config.get("max_retries", 3)), 1)
        self.tokens = TokenProvider(config, self.verify_tls, self.timeout)
        self.session = requests.Session()

    def api_request(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.api_root}/{path.lstrip('/')}"
        response: requests.Response | None = None
        for auth_attempt in range(2):
            response = self.session.request(
                method,
                url,
                json=payload,
                headers={"Authorization": f"Bearer {self.tokens.get(auth_attempt == 1)}"},
                timeout=self.timeout,
                verify=self.verify_tls,
            )
            if response.status_code != 401 or auth_attempt == 1:
                break

        assert response is not None
        if response.status_code not in expected:
            raise ApiError(
                f"{method} {path} failed ({response.status_code}): "
                f"{response_detail(response)}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ApiError(f"{method} {path} returned invalid JSON") from exc
        if not isinstance(body, dict) or body.get("success") is not True:
            raise ApiError(f"{method} {path} returned an unsuccessful response: {body}")
        data = body.get("data")
        if not isinstance(data, dict):
            raise ApiError(f"{method} {path} response has no data object")
        return data

    def initiate_upload(
        self, databank_id: str, key: str, part_count: int, content_type: str
    ) -> dict[str, Any]:
        databank = quote(databank_id, safe="")
        return self.api_request(
            "POST",
            f"databanks/{databank}/uploads",
            expected=(200,),
            payload={"key": key, "numParts": part_count, "contentType": content_type},
        )

    def upload_part(self, url: str, content: bytes, content_type: str) -> str:
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.put(
                    url,
                    data=content,
                    headers={"Content-Type": content_type},
                    timeout=self.timeout,
                    verify=self.verify_tls,
                )
                if response.status_code in (200, 201, 204):
                    etag = response.headers.get("ETag") or response.headers.get("etag")
                    if not etag:
                        raise ApiError("part upload succeeded but returned no ETag header")
                    return etag.strip('"')
                last_error = f"HTTP {response.status_code}: {response_detail(response)}"
            except requests.RequestException as exc:
                last_error = str(exc)
            if attempt < self.max_retries:
                time.sleep(2 ** (attempt - 1))
        raise ApiError(f"part upload failed after {self.max_retries} attempt(s): {last_error}")

    def complete_upload(
        self,
        databank_id: str,
        upload_id: str,
        key: str,
        parts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        databank = quote(databank_id, safe="")
        upload = quote(upload_id, safe="")
        return self.api_request(
            "PUT",
            f"databanks/{databank}/uploads/{upload}",
            expected=(200,),
            payload={"key": key, "parts": parts},
        )

    def cancel_upload(self, databank_id: str, upload_id: str, key: str) -> None:
        databank = quote(databank_id, safe="")
        upload = quote(upload_id, safe="")
        self.api_request(
            "POST",
            f"databanks/{databank}/uploads/{upload}/cancel",
            expected=(200,),
            payload={"key": key},
        )

    def create_processing_job(
        self, databank_id: str, job_type: str, options: dict[str, Any] | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": job_type}
        if options:
            payload["options"] = options
        databank = quote(databank_id, safe="")
        return self.api_request(
            "POST",
            f"databanks/{databank}/process",
            expected=(200, 202),
            payload=payload,
        )

    def get_job(self, databank_id: str, job_id: str) -> dict[str, Any]:
        databank = quote(databank_id, safe="")
        job = quote(job_id, safe="")
        return self.api_request(
            "GET", f"databanks/{databank}/process/{job}", expected=(200,)
        )


@dataclass(frozen=True)
class FileSpec:
    path: Path
    databank_id: str
    key: str
    content_type: str


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


def parse_files(config: dict[str, Any], config_dir: Path) -> list[FileSpec]:
    entries = config.get("files")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("'files' must be a non-empty array")
    default_databank = str(config.get("databank_id", "")).strip()
    result: list[FileSpec] = []
    seen: set[tuple[str, str]] = set()

    for index, entry in enumerate(entries):
        field = f"files[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"'{field}' must be an object")
        raw_path = Path(require_string(entry.get("file_path"), f"{field}.file_path"))
        path = raw_path if raw_path.is_absolute() else config_dir / raw_path
        path = path.resolve()
        if not path.is_file():
            raise ConfigError(f"'{field}.file_path' is not a file: {path}")
        databank_id = require_string(
            entry.get("databank_id", default_databank), f"{field}.databank_id"
        )
        raw_key = str(entry.get("key", path.name)).replace("\\", "/").strip()
        key_path = PurePosixPath(raw_key)
        key = str(key_path)
        if (
            not raw_key
            or key_path.is_absolute()
            or ".." in key_path.parts
            or (key_path.parts and key_path.parts[0].endswith(":"))
            or key in (".", "..")
        ):
            raise ConfigError(f"'{field}.key' must be a databank-relative object key")
        identity = (databank_id, key)
        if identity in seen:
            raise ConfigError(f"duplicate destination: databank={databank_id}, key={key}")
        seen.add(identity)
        guessed_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        content_type = str(entry.get("content_type", guessed_type)).strip()
        result.append(FileSpec(path, databank_id, key, content_type))
    return result


def calculate_parts(file_size: int, requested_part_size_mb: int) -> tuple[int, int]:
    part_size = max(requested_part_size_mb * MIB, MIN_MULTIPART_PART_SIZE)
    part_count = max(1, (file_size + part_size - 1) // part_size)
    if part_count > MAX_MULTIPART_PARTS:
        part_size = max(
            MIN_MULTIPART_PART_SIZE,
            (file_size + MAX_MULTIPART_PARTS - 1) // MAX_MULTIPART_PARTS,
        )
        part_count = max(1, (file_size + part_size - 1) // part_size)
    return part_count, part_size


def upload_file(
    client: FileServerClient, spec: FileSpec, requested_part_size_mb: int
) -> dict[str, Any]:
    file_size = spec.path.stat().st_size
    part_count, part_size = calculate_parts(file_size, requested_part_size_mb)
    LOG.info(
        "Uploading %s to %s/%s (%d bytes, %d part(s))",
        spec.path,
        spec.databank_id,
        spec.key,
        file_size,
        part_count,
    )
    session = client.initiate_upload(
        spec.databank_id, spec.key, part_count, spec.content_type
    )
    upload_id = require_string(session.get("uploadId"), "response.data.uploadId")
    remote_parts = session.get("parts")
    if not isinstance(remote_parts, list) or len(remote_parts) != part_count:
        raise ApiError(
            f"initiation returned {len(remote_parts) if isinstance(remote_parts, list) else 0} "
            f"part URL(s); expected {part_count}"
        )

    completed_parts: list[dict[str, Any]] = []
    try:
        with spec.path.open("rb") as handle:
            for index, remote_part in enumerate(remote_parts, start=1):
                if not isinstance(remote_part, dict):
                    raise ApiError(f"invalid part descriptor at position {index}")
                part_number = int(remote_part.get("partNumber", index))
                presigned_url = require_string(
                    remote_part.get("presignedUrl"), f"parts[{index - 1}].presignedUrl"
                )
                content = handle.read(part_size)
                etag = client.upload_part(presigned_url, content, spec.content_type)
                completed_parts.append({"partNumber": part_number, "eTag": etag})
                LOG.info("Uploaded %s part %d/%d", spec.key, index, part_count)
        result = client.complete_upload(
            spec.databank_id, upload_id, spec.key, completed_parts
        )
    except Exception:
        try:
            client.cancel_upload(spec.databank_id, upload_id, spec.key)
            LOG.info("Cancelled incomplete upload %s", upload_id)
        except Exception as cancel_error:  # best effort cleanup
            LOG.warning("Could not cancel upload %s: %s", upload_id, cancel_error)
        raise
    LOG.info("Onboarded %s successfully", spec.key)
    return result


def extract_job_ids(data: dict[str, Any]) -> list[str]:
    if isinstance(data.get("jobId"), str):
        return [data["jobId"]]
    job_ids = data.get("jobIds")
    if isinstance(job_ids, dict):
        return [value for value in job_ids.values() if isinstance(value, str) and value]
    return []


def wait_for_jobs(
    client: FileServerClient,
    databank_id: str,
    job_ids: list[str],
    poll_interval: float,
    timeout: float,
) -> bool:
    pending = set(job_ids)
    deadline = time.monotonic() + timeout
    all_successful = True
    while pending:
        if time.monotonic() >= deadline:
            raise ApiError(f"processing timed out with pending jobs: {sorted(pending)}")
        for job_id in list(pending):
            data = client.get_job(databank_id, job_id)
            status = str(data.get("status", "")).lower()
            LOG.info("Job %s status: %s", job_id, status or "unknown")
            if status in TERMINAL_JOB_STATUSES:
                pending.remove(job_id)
                all_successful = all_successful and status == "completed"
        if pending:
            time.sleep(poll_interval)
    return all_successful


def process_databanks(
    client: FileServerClient,
    processing: dict[str, Any],
    uploaded_keys: dict[str, list[str]],
) -> bool:
    if not processing.get("enabled", False):
        return True
    job_type = str(processing.get("type", "all")).lower()
    if job_type not in {"zip", "report", "all"}:
        raise ConfigError("'processing.type' must be 'zip', 'report', or 'all'")
    wait = bool(processing.get("wait_for_completion", True))
    poll_interval = max(float(processing.get("poll_interval_seconds", 5)), 0.1)
    timeout = max(float(processing.get("timeout_seconds", 3600)), 1)
    success = True

    for databank_id, keys in uploaded_keys.items():
        options = processing.get("options")
        if options is not None and not isinstance(options, dict):
            raise ConfigError("'processing.options' must be an object")
        options = dict(options or {})
        if processing.get("include_uploaded_files_only", False):
            options["include"] = keys
        result = client.create_processing_job(databank_id, job_type, options or None)
        job_ids = extract_job_ids(result)
        if not job_ids:
            raise ApiError(f"processing response has no job ID(s): {result}")
        LOG.info("Created %s processing job(s) for %s: %s", job_type, databank_id, job_ids)
        if wait:
            success = wait_for_jobs(
                client, databank_id, job_ids, poll_interval, timeout
            ) and success
    return success


def run(config_path: Path, dry_run: bool = False) -> int:
    config = load_config(config_path)
    files = parse_files(config, config_path.resolve().parent)
    if dry_run:
        for spec in files:
            LOG.info(
                "Would upload %s (%d bytes) to %s/%s as %s",
                spec.path,
                spec.path.stat().st_size,
                spec.databank_id,
                spec.key,
                spec.content_type,
            )
        processing = config.get("processing") or {}
        if isinstance(processing, dict) and processing.get("enabled", False):
            LOG.info(
                "Would start %s processing after successful upload(s)",
                processing.get("type", "all"),
            )
        LOG.info("Dry-run summary: %d file(s), no changes made", len(files))
        return 0

    client = FileServerClient(config)
    part_size_mb = max(int(config.get("part_size_mb", 100)), 5)
    uploaded: dict[str, list[str]] = {}
    failed_databanks: set[str] = set()
    upload_failures = 0
    processing_failures = 0

    for spec in files:
        try:
            upload_file(client, spec, part_size_mb)
            uploaded.setdefault(spec.databank_id, []).append(spec.key)
        except Exception as exc:
            upload_failures += 1
            failed_databanks.add(spec.databank_id)
            LOG.error("Failed to onboard %s: %s", spec.path, exc)

    for databank_id in failed_databanks:
        if databank_id in uploaded:
            LOG.warning(
                "Skipping processing for %s because one or more uploads failed",
                databank_id,
            )
            uploaded.pop(databank_id)

    processing = config.get("processing") or {}
    if not isinstance(processing, dict):
        raise ConfigError("'processing' must be an object")
    if uploaded:
        try:
            if not process_databanks(client, processing, uploaded):
                processing_failures += 1
        except Exception as exc:
            processing_failures += 1
            LOG.error("Post-upload processing failed: %s", exc)

    LOG.info(
        "Creation summary: %d upload(s) succeeded, %d upload(s) failed, "
        "%d processing failure(s)",
        len(files) - upload_failures,
        upload_failures,
        processing_failures,
    )
    return 1 if upload_failures or processing_failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "config",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("file_creation_config.json"),
        help="JSON config path (default: file_creation_config.json beside this script)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and show upload targets without changing remote data",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    try:
        return run(args.config, args.dry_run)
    except (ConfigError, ApiError, OSError) as exc:
        LOG.error("Creation failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
