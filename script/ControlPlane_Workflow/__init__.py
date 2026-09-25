"""
ControlPlane onboarding E2E harness.

The runnable entry point is `main/e2e.py`; everything here is the harness
itself, imported as a package so the modules can refer to each other without
depending on the working directory a run starts from.

Two of the servers a run checks are not part of this package, because they are
not part of the ControlPlane workflow: the sandbox and the community layer are
separate deployments with their own repositories, so their checks live beside
the other per-server automation in `script/sandbox/` and
`script/community-layer/`.

Those directories are put on the import path here rather than in the entry
point, so the package resolves them however it is entered — `main/e2e.py`, or
`python3 -m ControlPlane_Workflow.config` from the repository root. The folder
names are free of the constraint Python puts on module names, which matters:
`community-layer` carries a hyphen and could never be imported as a package,
whereas the module inside it can be imported once its directory is on the path.
"""

import sys
from pathlib import Path

# script/, which holds this package and the per-server directories beside it.
_SCRIPT_DIR = Path(__file__).resolve().parent.parent

for _server_dir in ("sandbox", "community-layer"):
    _path = str(_SCRIPT_DIR / _server_dir)
    if _path not in sys.path:
        sys.path.insert(0, _path)
