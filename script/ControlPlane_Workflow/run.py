#!/usr/bin/env python3
"""
ControlPlane onboarding E2E harness.

Driven by main/e2e.py:

    python3 main/e2e.py
    python3 main/e2e.py --set run.cleanup=false
    python3 main/e2e.py --sweep-only              # reap old orphans
    python3 main/e2e.py --report run.html

Teardown runs in a finally block, so artefacts are removed even when a phase
fails — which is precisely when leaving them behind hurts most.
"""

import argparse
import json
import os
import html
import sys
import time

from . import cleanup
from . import config as config_module
from . import client as client_module
from .client import Recorder
from .flow import PHASES, RunContext


def _banner(text):
    print(f"\n{text}", flush=True)


def _run_phases(ctx, only):
    """Run the phases, returning the name of the one that failed, or None."""
    for name, phase in PHASES:
        if only and not name.startswith(only) and only not in name:
            continue
        ctx.recorder.enter(name)
        _banner(f"[{name}]")
        started = time.monotonic()
        try:
            phase(ctx)
        except Exception as err:  # noqa: BLE001 - reported, then teardown runs
            print(f"    FAILED: {err}", flush=True)
            return name, err
        print(f"    ok ({time.monotonic() - started:.1f}s)", flush=True)
    return None, None


def _artefacts_table(ctx):
    """What the run created, with the identifiers needed to check the backend."""
    rows = [("namespace", ctx.namespace)]
    for key, user in ctx.users.items():
        rows.append((f"user: {key}", f"{user.username} &nbsp; <code>{user.user_id}</code>"))
    if not ctx.owns_cos_admin:
        rows.append(("cos admin", f"{ctx.config['cos_admin']['username']} (existing, not swept)"))
    # One block per lane. A run with a single provider prints exactly the rows
    # it always did; a split one prints the gateway lane's item, organisation and
    # provider under their own headings.
    for lane in ctx.lanes:
        rows += [
            (f"organisation{lane.suffix}", f"{lane.org_name} &nbsp; <code>{lane.org_id}</code>"),
            (f"org create request{lane.suffix}", f"<code>{lane.org_request_id}</code>"),
            (
                f"catalogue item{lane.suffix}",
                f"{lane.item_name} &nbsp; <code>{lane.item_id}</code> &nbsp; "
                f"({', '.join(lane.server_keys()) or 'no resource server'}, "
                f"provider {html.escape(lane.provider.key)})",
            ),
            (f"access request{lane.suffix}", f"<code>{lane.access_request_id}</code>"),
            (
                f"policies{lane.suffix}",
                ", ".join(f"<code>{p}</code>" for p in lane.policy_ids) or "—",
            ),
        ]
    body = "".join(
        f"<tr><th>{html.escape(name)}</th><td>{value}</td></tr>"
        for name, value in rows
        if value not in (None, "None", "")
    )
    return f"<table class='kv'>{body}</table>"


def _write_report(path, ctx, failed_phase, survivors):
    rows = []
    for index, e in enumerate(ctx.recorder.entries):
        rows.append(
            "<tr class='{cls}'>"
            "<td>{n}</td><td>{phase}</td><td>{label}</td>"
            "<td class='m'>{method}</td><td class='u'>{url}</td>"
            "<td class='s'>{status}</td><td>{ms}</td></tr>".format(
                cls="bad" if not e["ok"] else "good",
                n=index + 1,
                phase=html.escape(e["phase"]),
                label=html.escape(e["label"]),
                method=html.escape(e["method"]),
                url=html.escape(e["url"]),
                status=e["status"],
                ms=e["ms"],
            )
        )

    status = "FAILED" if failed_phase else "PASSED"
    survivor_html = (
        "<ul class='bad-list'>"
        + "".join(f"<li>{html.escape(s)}</li>" for s in survivors)
        + "</ul>"
        if survivors
        else "<p class='ok'>Nothing survived teardown.</p>"
    )
    if not ctx.config["run"]["cleanup"]:
        survivor_html = (
            "<p class='warn'>Cleanup was disabled for this run — every artefact "
            "below is still on the deployment. Remove them with "
            "<code>--sweep-only</code>.</p>"
        )

    document = f"""<!doctype html><meta charset="utf-8">
<title>ControlPlane E2E — {html.escape(ctx.namespace)}</title>
<style>
 body{{font:14px/1.55 system-ui,sans-serif;margin:2rem;color:#111;max-width:1500px}}
 table{{border-collapse:collapse;width:100%;margin-bottom:1.5rem}}
 td,th{{border-bottom:1px solid #e3e3e3;padding:.45rem .5rem;vertical-align:top;text-align:left}}
 th{{background:#f6f6f6;font-weight:600}}
 table.kv th{{width:170px;background:#fafafa}}
 tr.bad td{{background:#fff2f2}}
 td.m{{font-weight:600;white-space:nowrap}}
 td.s{{text-align:right;font-variant-numeric:tabular-nums}}
 td.u{{font:12px/1.4 ui-monospace,monospace;word-break:break-all;max-width:520px}}
 pre{{margin:.35rem 0 0;white-space:pre-wrap;word-break:break-word;font-size:12px;
      background:#fafafa;padding:.5rem;border-left:3px solid #ddd;max-height:340px;overflow:auto}}
 summary{{cursor:pointer;font-size:12px;color:#0645ad}}
 code{{background:#f2f2f2;padding:.05rem .3rem;border-radius:3px;font-size:12px}}
 .status{{font-size:1.1rem;font-weight:700}}
 .ok{{color:#0a7a2f}} .warn{{color:#8a5a00}} .bad-list li{{color:#a11}}
</style>
<h1>ControlPlane onboarding E2E</h1>
<p class="status {'bad-list' if failed_phase else 'ok'}">{status}</p>
<p>config <code>{html.escape(str(ctx.config.get('_config_file')))}</code> &middot;
   target <code>{html.escape(ctx.config['control_plane']['base_url'])}</code></p>

<h2>Artefacts</h2>
{_artefacts_table(ctx)}

<h2>Cleanup verification</h2>
{survivor_html}

<h2>Calls ({len(ctx.recorder.entries)})</h2>
<p>Request and response bodies are not recorded, so this report cannot carry a
   credential. The run's terminal output has them.</p>
<table>
<tr><th>#</th><th>Phase</th><th>Call</th><th>Method</th><th>URL</th>
    <th>Status</th><th>ms</th></tr>
{"".join(rows)}</table>
"""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(document)
    print(f"\nreport written to {path}")


def _report_path(explicit, configuration, ctx):
    """Where this run's report goes.

    --report wins, then run.report_file (one file, overwritten every run), then
    the run's namespace (one file per run, nothing overwritten).
    """
    if explicit:
        directory = os.path.dirname(os.path.abspath(explicit))
        os.makedirs(directory, exist_ok=True)
        return explicit
    directory = configuration["run"].get("report_dir")
    if not directory:
        return None
    os.makedirs(directory, exist_ok=True)
    name = configuration["run"].get("report_file") or f"{ctx.namespace}.html"
    return os.path.join(directory, name)


def main():
    parser = argparse.ArgumentParser(
        description="Run the ControlPlane onboarding workflow end to end."
    )
    parser.add_argument(
        "config_file", nargs="?",
        help="path to a config file (default: config.json beside this script)",
    )
    parser.add_argument(
        "--set", action="append", default=[], metavar="PATH=VALUE",
        help="override a config key, e.g. --set run.cleanup=false",
    )
    parser.add_argument("--only", metavar="PHASE", help="run only phases matching this text")
    parser.add_argument(
        "--sweep-only", action="store_true",
        help="skip the flow; only reap namespaced leftovers from earlier runs",
    )
    parser.add_argument("--report", metavar="FILE", help="write an HTML report")
    args = parser.parse_args()

    try:
        configuration = config_module.load(args.config_file, args.set)
    except config_module.ConfigError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    # Before anything is recorded: every credential the config holds is masked
    # wherever it later appears — a script's output, a URL, an error body.
    client_module.register_secrets(config_module.secret_values(configuration))

    recorder = Recorder()
    ctx = RunContext(configuration, recorder)

    print(f"config    {configuration['_config_file']}")
    print(f"target    {configuration['control_plane']['base_url']}")
    print(f"namespace {ctx.namespace}")
    for note in config_module.warnings(configuration):
        print(f"warning:  {note}")

    if args.sweep_only:
        _banner("[sweep only]")
        problems = cleanup.sweep_only(ctx)
        for problem in problems:
            print(f"    {problem}")
        print("\nsweep complete" if not problems else f"\nsweep finished with {len(problems)} problem(s)")
        return 1 if problems else 0

    failed_phase, failure = None, None
    survivors = []
    try:
        failed_phase, failure = _run_phases(ctx, args.only)
    finally:
        _banner("[teardown]")
        problems = cleanup.teardown(ctx)
        for problem in problems:
            print(f"    {problem}")

        if configuration["run"]["cleanup"] and configuration["run"]["verify_cleanup"]:
            _banner("[verify cleanup]")
            survivors = cleanup.verify(ctx)
            if survivors:
                for survivor in survivors:
                    print(f"    SURVIVED: {survivor}")
            else:
                print("    nothing survived")

        report_path = _report_path(args.report, configuration, ctx)
        if report_path:
            _write_report(report_path, ctx, failed_phase, survivors)

    _banner("=" * 60)
    calls = len(recorder.entries)
    failures = recorder.failures()
    print(f"calls        {calls} ({len(failures)} failed)")
    for key, value in ctx.results.items():
        print(f"{key:<12} {value}")

    if failed_phase:
        print(f"\nFAILED in {failed_phase}: {failure}")
        return 1
    if survivors:
        print(f"\nFAILED: {len(survivors)} artefact(s) survived teardown")
        return 1
    print("\nPASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())