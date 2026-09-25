#!/usr/bin/env python3
"""
Running the standalone NGSI-LD scripts as subprocesses.

`script/NGSILD_Automation_Script/` holds scripts that are run by hand against
real deployments — publishing records, tearing an exchange down. The harness
runs those same scripts rather than reimplementing what they do, so a bug found
by one path is fixed for both.

They share a shape: an INI for configuration, `--config` to point at it, and
documented exit codes (0 ok, 1 the work failed, 2 the configuration was
rejected). This module is that shape, once: it writes a temporary INI, runs the
script, records the attempt for the report, echoes the interesting part of the
log, and hands the exit code back for the caller to interpret. Callers differ on
what a failure means — a phase raises, teardown collects — so nothing here
raises.

The INI is written 0600 and deleted afterwards, because it carries broker and
Elasticsearch credentials.
"""

import configparser
import glob
import io
import os
import shutil
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .client import redact_text

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

# How much of a script's log to echo. The publisher logs one INFO line per
# packet, so a long run would otherwise bury the rest of the phase output.
TAIL_LINES = 12

_MISSING_MODULE = re.compile(r"ModuleNotFoundError: No module named '([^']+)'")


class ScriptResult:
    """Outcome of one run: the exit code, what it means, and the log."""

    def __init__(self, code, output, meaning):
        self.code = code
        self.output = output
        self.meaning = meaning

    @property
    def ok(self):
        return self.code == EXIT_OK

    def __repr__(self):
        return f"<ScriptResult {self.code} {self.meaning}>"


def ini_text(sections):
    """Render `{section: {key: value}}` as INI text. None values are dropped."""
    parser = configparser.ConfigParser(interpolation=None)
    for section, values in sections.items():
        parser[section] = {
            key: ("true" if value is True else "false" if value is False else str(value))
            for key, value in values.items()
            if value is not None
        }
    buffer = io.StringIO()
    parser.write(buffer)
    return buffer.getvalue()


def write_file(directory, filename, text):
    """Write one config file into the run directory, readable only by us."""
    path = os.path.join(directory, filename)
    # 0600 before a byte is written — these files carry credentials.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(text)
    return path


def echo_log(output, verbose):
    """Print the script's log, tail-only unless the run asked for all of it.

    Redacted first: a script that echoes its own config would otherwise print a
    broker password straight to the terminal.
    """
    lines = [line for line in redact_text(output).splitlines() if line.strip()]
    shown = lines if verbose else lines[-TAIL_LINES:]
    if not verbose and len(lines) > TAIL_LINES:
        print(f"      … {len(lines) - TAIL_LINES} earlier line(s) omitted", flush=True)
    for line in shown:
        print(f"      {line}", flush=True)


def _meaning(code, output, meanings):
    text = meanings.get(code, f"unknown exit code {code}")
    missing = _MISSING_MODULE.search(output or "")
    if missing:
        # The script runs as a subprocess, so its dependencies are not the
        # harness's — say so rather than leaving a traceback to read.
        text = f"{text}: {missing.group(1)} is not installed for {sys.executable}"
    return text


def run(ctx, script, meanings, *, label, target, files, args=(), config_arg=None,
        positional_config=None, cwd_in_temp=False, copy_script=False, collect=(),
        verbose=False, retries=0, retry_delay=5, request=None, env=None):
    """Run `script` in a temporary directory holding the files it needs.

    ctx        -- the run context, for the recorder
    script     -- Path to the script
    meanings   -- {exit code: human-readable meaning}
    files      -- {filename: text} written into the run directory
    config_arg -- filename from `files` to pass as `--config <path>`, for the
                  scripts that take one
    positional_config -- filename from `files` to pass as a bare argument, for
                  the scripts that take the config path positionally
    cwd_in_temp -- run with cwd set to the temporary directory, for the scripts
                  that read their config from `./<name>` with the path
                  hardcoded. Otherwise cwd is the script's own directory.
    copy_script -- run a copy of the script placed in the temporary directory,
                  for the ones that look for their config *beside themselves*.
                  The alternative would be writing credentials into the
                  checkout.
    collect    -- glob patterns for log files the script writes, appended to the
                  captured output so its own log is part of the failure report
    env        -- extra environment variables for the child, over the inherited
                  environment. PGOPTIONS is how a schema reaches a script whose
                  SQL is unqualified and whose config has no schema field.
    retries    -- retry a failed run this many times; a configuration error (2)
                  is never retried, since repeating it only repeats it

    Returns a ScriptResult. Never raises for a failing script.
    """
    attempts = retries + 1
    directory = tempfile.mkdtemp(prefix="dx-e2e-")
    try:
        for name, text in files.items():
            write_file(directory, name, text)

        target_script = script
        if copy_script:
            target_script = Path(shutil.copy2(script, directory))

        command = [sys.executable, str(target_script)]
        if config_arg:
            command += ["--config", os.path.join(directory, config_arg)]
        if positional_config:
            command.append(os.path.join(directory, positional_config))
        command += list(args)
        working_directory = directory if (cwd_in_temp or copy_script) else str(script.parent)

        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            completed = subprocess.run(
                command,
                cwd=working_directory,
                capture_output=True,
                text=True,
                check=False,
                env={**os.environ, **(env or {})} if env else None,
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            # These scripts log to stderr; stdout is normally empty.
            output = (completed.stdout or "") + (completed.stderr or "")
            for pattern in collect:
                for found in sorted(glob.glob(os.path.join(directory, pattern))):
                    output += f"\n--- {os.path.basename(found)} ---\n" + _read(found)
            code = completed.returncode
            meaning = _meaning(code, output, meanings)

            ctx.recorder.add(
                label + (f" (attempt {attempt})" if attempts > 1 else ""),
                "SCRIPT",
                target,
                code,
                code == EXIT_OK,
                elapsed_ms,
                detail=meaning,
                request=dict(request or {}, script=str(script)),
                response={"exit_code": code, "log": output[-4000:]},
            )
            echo_log(output, verbose)

            if code == EXIT_OK or code == EXIT_CONFIG or attempt == attempts:
                return ScriptResult(code, output, meaning)

            print(
                f"      {meaning}; retrying in {retry_delay}s "
                f"({attempt}/{attempts - 1})",
                flush=True,
            )
            time.sleep(retry_delay)
    finally:
        # These files hold credentials, so they do not outlive the call.
        shutil.rmtree(directory, ignore_errors=True)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""
