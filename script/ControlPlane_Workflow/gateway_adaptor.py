#!/usr/bin/env python3
"""
Run the gateway adaptor for as long as the gateway is being read.

The gateway resource server does not serve from storage. It publishes the
request onto a queue named with the item id and waits for a reply on the
message's reply_to. If nothing is consuming that queue the request is never
answered and the caller eventually times out — which is exactly what a read
timeout on `/dataplane/rsp/...` is, and why it looks like a slow server rather
than a missing component.

`script/GATEWAY_Automation_Script/gateway.py` is that consumer: it takes each
request as a trigger, calls an upstream API, and publishes `{"results": [...]}`
back to reply_to. Unlike the publish and teardown scripts it does not run and
exit — it consumes until it is stopped — so it is started before the gateway
calls and stopped afterwards, rather than being run to completion.

Two details of that script shape this module:

  * It reads `./config.ini` — the path is hardcoded, relative to the working
    directory. So it runs with cwd set to a temporary directory holding the
    config written for this run, and every path inside that config is absolute.

  * Its log goes to stderr and keeps coming for as long as it runs. It is
    captured to a file rather than a pipe, because a pipe nobody drains fills
    up and blocks the process it belongs to.
"""

import contextlib
import os
import subprocess
import sys
import tempfile
import time

from . import script_runner
from .config import resolve_path


def settings(config):
    """Adaptor settings, with the broker fallbacks applied."""
    adaptor = config["gateway_adaptor"]
    publish = config["ngsild_publish"]
    databroker = config["databroker"]

    def either(key):
        return adaptor.get(key) or publish.get(key) or databroker.get(key)

    return {
        "host": either("host"),
        "port": int(adaptor["port"]),
        "username": either("username"),
        "password": either("password"),
        "vhost": adaptor["vhost"],
        "cert_path": str(resolve_path(adaptor["cert_path"])),
        "check_hostname": bool(adaptor["check_hostname"]),
        "api_url": adaptor["api_url"],
        "results_key": adaptor["results_key"],
        # Absolute, like cert_path: the adaptor resolves it against its cwd,
        # which is the temporary directory rather than the checkout.
        "data_file": (
            str(resolve_path(adaptor["data_file"])) if adaptor.get("data_file") else ""
        ),
    }


def _write_config(directory, values, queue):
    """Write the config.ini the adaptor reads from its working directory."""
    sections = {
        "server_setup": {
            "username": values["username"],
            "password": values["password"],
            "host": values["host"],
            "port": values["port"],
            "vhost": values["vhost"],
            # Absolute: the script resolves this against its cwd, which is the
            # temporary directory, not the checkout.
            "cert_path": values["cert_path"],
            "check_hostname": values["check_hostname"],
        },
        # The queue the catalogue created for the item, named with the item id.
        "queue": {"name": queue},
        # Both may be blank, and that is meaningful: with no dataset and no
        # upstream the adaptor answers from its own SAMPLE_RECORDS. ini_text
        # would choke on None, so they are written blank.
        "api": {
            "url": values["api_url"] or "",
            "results_key": values["results_key"],
            "data_file": values["data_file"] or "",
        },
    }
    return script_runner.write_file(
        directory, "config.ini", script_runner.ini_text(sections)
    )


def _tail(path, limit=8000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()[-limit:]
    except OSError:
        return ""


@contextlib.contextmanager
def running(ctx, queue):
    """Run the adaptor against `queue` for the duration of the block.

    Raises AssertionError if it dies before it can consume — a gateway call made
    with no adaptor attached would otherwise fail as a timeout minutes later,
    naming the wrong culprit.
    """
    adaptor = ctx.config["gateway_adaptor"]
    values = settings(ctx.config)
    script = resolve_path(adaptor["script"])
    target = (
        f"amqps://{values['host']}:{values['port']}/{values['vhost']} ← {queue}"
    )

    directory = tempfile.mkdtemp(prefix="dx-e2e-gateway-")
    config_path = _write_config(directory, values, queue)
    log_path = os.path.join(directory, "gateway.log")
    started = time.monotonic()
    echoed = False

    with open(log_path, "w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=directory,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    try:
        delay = adaptor["startup_seconds"]
        print(f"    gateway adaptor: consuming from {queue} (pid {process.pid})", flush=True)
        if delay:
            time.sleep(delay)

        if process.poll() is not None:
            output = _tail(log_path)
            script_runner.echo_log(output, adaptor["verbose"])
            echoed = True
            ctx.recorder.add(
                f"start gateway adaptor ({queue})", "PROC", target,
                process.returncode, False, int((time.monotonic() - started) * 1000),
                detail="adaptor exited before it could consume",
                request={"script": str(script), "queue": queue},
                response={"exit_code": process.returncode, "log": output[-4000:]},
            )
            raise AssertionError(
                f"gateway adaptor exited {process.returncode} before it could "
                f"consume from {queue} — the gateway has nothing to answer with"
            )

        ctx.recorder.add(
            f"start gateway adaptor ({queue})", "PROC", target,
            200, True, int((time.monotonic() - started) * 1000),
            detail=f"consuming after {delay}s",
            request={"script": str(script), "queue": queue, "api_url": values["api_url"]},
        )
        yield process

    finally:
        if process.poll() is None:
            # SIGTERM first: the script stops consuming and closes its
            # connection on the way out, so the broker is not left holding one.
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        output = _tail(log_path)
        if not echoed:
            # Already printed when the adaptor died on startup; printing the
            # same log twice buries the reason it died.
            script_runner.echo_log(output, adaptor["verbose"])
            served = output.count("Response published")
            print(f"    gateway adaptor: stopped, answered {served} request(s)", flush=True)
        else:
            served = output.count("Response published")
        ctx.recorder.add(
            f"stop gateway adaptor ({queue})", "PROC", target,
            200, True, int((time.monotonic() - started) * 1000),
            detail=f"answered {served} request(s)",
            response={"log": output[-4000:]},
        )

        with contextlib.suppress(OSError):
            os.remove(config_path)
            os.remove(log_path)
            os.rmdir(directory)
