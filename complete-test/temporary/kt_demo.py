#!/usr/bin/env python3
"""Run the KT/demo flow: the cut-down ControlPlane collection, then the data plane.

TEMPORARY, 2026-09-10. The restructured ControlPlane collection is not finished,
so the walkthrough runs on the small onboarding collection under
resource/Controlplane_temporary_check_1/ instead — sign in, onboard an
organisation, publish a catalogue item, request and grant access, read the audit
trail, tear it down — with the data plane behind it exactly as it runs today.

Nothing here forks the harness. It is complete_test.py, run on
temporary/config.kt.json, which is a diff of ../config.json: the deployment, the
credentials and the resource servers all stay in one place. Delete this
directory and the settled setup is what runs.

    python3 temporary/kt_demo.py                 # the whole demo
    python3 temporary/kt_demo.py --list          # what it would run
    python3 temporary/kt_demo.py --only '00 users,01'   # accounts + the collection
    python3 temporary/kt_demo.py --rebuild-reports

Every flag complete_test.py takes works here — they are passed straight through.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS_DIR = HERE.parent
HARNESS = HARNESS_DIR / "complete_test.py"
CONFIG = HERE / "config.kt.json"

sys.path.insert(0, str(HARNESS_DIR))

import complete_test  # noqa: E402
from ControlPlane_Workflow import config as config_module  # noqa: E402


def preflight():
    """What has to be in place before a demo run, checked while it is still
    cheap to fix — a missing collection file at request 1 of 24 is a bad way to
    open a session."""
    problems = []
    if not CONFIG.is_file():
        problems.append(f"the demo config is missing: {CONFIG}")
        return problems

    configuration = config_module.load(str(CONFIG))
    for key, entry in configuration["postman"]["collections"].items():
        if not entry.get("enabled"):
            continue
        for field in ("collection", "environment"):
            value = entry.get(field)
            if value and not Path(config_module.resolve_path(value)).is_file():
                problems.append(f"{key}.{field} does not exist: {value}")

    if complete_test.newman_html_reporter_missing(configuration):
        problems.append(
            "newman's HTML reporter is not installed, and the demo config asks "
            "for it:\n      npm install --prefix "
            f"{complete_test.install_dir(configuration)} newman-reporter-htmlextra"
        )
    return problems


def main():
    problems = preflight()
    if problems:
        print("cannot run the demo:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    print(__doc__.split("\n\n")[1].strip())
    print()

    # The harness resolves its reports and its default config against the entry
    # point, and the entry point is complete_test.py — this file only points it
    # at the demo config. Without this, a run started here would write its
    # reports into temporary/ and a run started there would not, which is a
    # confusing difference to explain in the middle of a walkthrough.
    sys.argv = [str(HARNESS), str(CONFIG)] + sys.argv[1:]
    return complete_test.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
