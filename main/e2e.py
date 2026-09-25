#!/usr/bin/env python3
"""
ControlPlane onboarding E2E harness — the entry point.

    python3 main/e2e.py
    python3 main/e2e.py --set run.cleanup=false
    python3 main/e2e.py --sweep-only              # reap old orphans
    python3 main/e2e.py --report run.html

Everything the harness does lives in `script/ControlPlane_Workflow`; this file
only makes that package importable regardless of where the run starts from, and
hands over to it. `config.json` is read from beside this script, and reports are
written to `main/reports` unless the config says otherwise.
"""

import sys
from pathlib import Path

# script/ holds the package, so that directory goes on the path rather than the
# repository root — the package is imported by its own name, not through a
# `script.` prefix that would need script/ to be a package too.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "script"))

from ControlPlane_Workflow.run import main  # noqa: E402 - path set up first

if __name__ == "__main__":
    sys.exit(main())
