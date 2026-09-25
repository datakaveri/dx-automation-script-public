#!/usr/bin/env python3
"""
HTTP plumbing shared by every phase.

Every call the harness makes is recorded, so a failed run can say exactly which
request broke and what came back, and so a report can be emitted afterwards
without the phases having to thread logging through themselves.
"""

import json
import re
import time

import requests


class ApiError(Exception):
    """A request that did not return the status the caller required."""

    def __init__(self, label, method, url, status, expected, body):
        self.label = label
        self.status = status
        self.expected = expected
        self.body = body
        want = " or ".join(str(s) for s in expected)
        super().__init__(f"{label}: {method} {url} returned {status}, expected {want}\n{body}")


class Recorder:
    """Flat log of every call, in order, for the end-of-run report.

    What a call *carried* is deliberately not kept: not the request, not the
    response, not an excerpt of a failed one. A report is a file that gets
    shared, and bodies are where credentials, tokens and generated configs live.
    Dropping them at capture time is an absence rather than a filter, so no
    rendering path can surface them later and no unanticipated shape can slip
    through redaction.

    A run's terminal output is unaffected: phases print the responses they read,
    and a failure still raises with the body that caused it.
    """

    def __init__(self):
        self.entries = []
        self.phase = "-"

    def enter(self, phase):
        self.phase = phase

    def add(self, label, method, url, status, ok, elapsed_ms, detail="",
            request=None, response=None):
        # request, response and detail are accepted so callers read naturally,
        # and are discarded here.
        self.entries.append(
            {
                "phase": self.phase,
                "label": label,
                "method": method,
                "url": url,
                "status": status,
                "ok": ok,
                "ms": elapsed_ms,
            }
        )

    def failures(self):
        return [e for e in self.entries if not e["ok"]]


_SECRET_KEYS = ("password", "secret", "token", "access_token", "client_secret",
                "credentials", "authorization")

# Values that must never reach a report, a log line or a terminal, whatever
# shape they arrive in. Registered from the resolved config at startup, so a
# credential is masked even when it turns up inside a script's raw output or a
# URL the platform handed back — places no key-based rule can see it.
_SECRET_VALUES = set()

# Credentials that are not in the config because the platform minted them.
_SECRET_PATTERNS = (
    # JWTs, including ones echoed inside a larger blob of text
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    # Pre-signed S3 URLs: the signature is the credential, and the key id
    # travels beside it
    re.compile(r"(?i)(X-Amz-Signature=)[0-9a-f]{16,}"),
    re.compile(r"(?i)(X-Amz-Credential=)[^&\s\"']+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # password=… / "secret": "…" / token: … in free text, which is how a
    # script's own log leaks one
    re.compile(r"(?i)\b(password|passwd|secret|client_secret|api[_-]?key|token)"
               r"(\s*[=:]\s*\"?)([^\s,;&\"'}]{4,})"),
)


def register_secrets(values):
    """Register credential values to mask wherever they appear."""
    for value in values:
        if isinstance(value, str) and len(value.strip()) >= 4:
            _SECRET_VALUES.add(value.strip())


def redact_text(text):
    """Mask credentials in free text — script output, logs, URLs, error bodies.

    Key-based scrubbing only reaches structured fields. A broker password
    echoed by a script, or a signature inside a URL the platform returned, is
    just text, and text is what ends up in the report.
    """
    if not isinstance(text, str) or not text:
        return text
    for secret in _SECRET_VALUES:
        if secret in text:
            text = text.replace(secret, "***")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 3:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
        elif pattern.groups == 1:
            text = pattern.sub(lambda m: f"{m.group(1)}***", text)
        else:
            text = pattern.sub("***", text)
    return text


def scrub(value, _depth=0):
    """Copy a payload with credential-bearing fields masked.

    Reports are written to a file and shared, and these payloads include
    Keycloak passwords, client secrets and access tokens. Recording them raw
    would turn a debugging aid into a credential leak.
    """
    if _depth > 6:
        return "..."
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if any(hint in str(key).lower() for hint in _SECRET_KEYS):
                out[key] = "***"
            else:
                out[key] = scrub(item, _depth + 1)
        return out
    if isinstance(value, list):
        return [scrub(v, _depth + 1) for v in value[:25]]
    if isinstance(value, str):
        if value.count(".") == 2 and len(value) > 100:
            return "***jwt***"  # bare token echoed outside a named field
        return redact_text(value)
    return value


def _short(body, limit=600):
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


class ApiClient:
    """Thin wrapper over requests for one host.

    Responses are unwrapped from the platform's {type,title,result} envelope, so
    callers work with the payload rather than the envelope.
    """

    def __init__(self, base_url, recorder, timeout=30, verify_tls=True):
        self.base_url = base_url.rstrip("/")
        self.recorder = recorder
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify_tls

    def request(
        self,
        method,
        path,
        label,
        token=None,
        json_body=None,
        params=None,
        headers=None,
        expect=(200,),
    ):
        url = f"{self.base_url}/{path.lstrip('/')}"
        all_headers = {"Accept": "application/json"}
        if token:
            all_headers["Authorization"] = f"Bearer {token}"
        if json_body is not None:
            all_headers["Content-Type"] = "application/json"
        if headers:
            all_headers.update(headers)

        started = time.monotonic()
        try:
            response = self.session.request(
                method,
                url,
                json=json_body,
                params=params,
                headers=all_headers,
                timeout=self.timeout,
            )
        except requests.RequestException as err:
            elapsed = int((time.monotonic() - started) * 1000)
            self.recorder.add(label, method, url, 0, False, elapsed, str(err))
            raise ApiError(label, method, url, 0, expect, str(err)) from err

        elapsed = int((time.monotonic() - started) * 1000)
        try:
            payload = response.json()
        except ValueError:
            payload = response.text

        ok = response.status_code in expect
        self.recorder.add(
            label,
            method,
            url if not params else f"{url}?" + "&".join(f"{k}={v}" for k, v in params.items()),
            response.status_code,
            ok,
            elapsed,
            "" if ok else _short(payload),
            request=json_body,
            response=payload,
        )
        if not ok:
            raise ApiError(label, method, url, response.status_code, expect, _short(payload))

        if isinstance(payload, dict) and "result" in payload:
            return payload["result"]
        return payload

    def get(self, path, label, **kw):
        return self.request("GET", path, label, **kw)

    def post(self, path, label, **kw):
        return self.request("POST", path, label, **kw)

    def put(self, path, label, **kw):
        return self.request("PUT", path, label, **kw)

    def patch(self, path, label, **kw):
        return self.request("PATCH", path, label, **kw)

    def delete(self, path, label, **kw):
        return self.request("DELETE", path, label, **kw)


def field(row, *names, default=None):
    """Read a field from an API row, whether it is flat or nested one level.

    ACL endpoints group related fields into sub-objects — an access request
    carries the item id at `asset.itemId` and the consumer at `user.consumerId`
    — while other endpoints return the same information flat. Callers should
    not have to track which shape each endpoint uses, and a response gaining or
    losing a grouping level should not break the flow.

    Names are tried in order at the top level first, then one level down.
    """
    if not isinstance(row, dict):
        return default
    for name in names:
        if row.get(name) is not None:
            return row[name]
    for value in row.values():
        if isinstance(value, dict):
            for name in names:
                if value.get(name) is not None:
                    return value[name]
    return default


def rows_of(payload):
    """The list of records in a response, paginated or not."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "result", "items"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def try_delete(fn, description, problems):
    """Run one teardown step, recording rather than raising on failure.

    Teardown must keep going after a step fails — one undeletable artefact
    should not strand every artefact after it.
    """
    try:
        fn()
        return True
    except Exception as err:  # noqa: BLE001 - teardown never propagates
        problems.append(f"{description}: {err}")
        return False