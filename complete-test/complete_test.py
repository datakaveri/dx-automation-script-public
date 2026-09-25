#!/usr/bin/env python3
"""
The complete test: the published Postman collections, driven end to end.

    python3 complete-test/complete_test.py
    python3 complete-test/complete_test.py --list
    python3 complete-test/complete_test.py --only 02
    python3 complete-test/complete_test.py --set run.cleanup=false

`main/e2e.py` proves the onboarding workflow by calling the APIs itself. This
harness proves the same workflow *and* the API contract around it, by running
the collections the QA team maintains — every positive case and every negative
one, in an order that leaves a real, working chain behind: a provider, an
organisation, an item with data behind it, a policy, and an audit trail.

Two kinds of phase, listed in `postman.phases` and run in order:

    postman   a folder handed to newman, asserted by the collection's own tests
    script    work no collection can do — Keycloak users, RabbitMQ, S3, Postgres

So the split is not "some APIs here, some there". Every API call this harness
can make through a collection, it makes through a collection. The scripts fill
exactly the gaps a collection cannot reach, and say so in the report.

The same division decides where a value lives. Anything a collection can carry
is written into the *generated* Postman environment at run time — hosts,
personas, passwords, tokens, and every id the flow produces, all namespaced to
this run. Anything it cannot — broker credentials, S3 keys, the database, the
GeoPackage on disk — stays in config.json and is read straight from there by
the scripts. Nothing is configured twice.

Adding the next server's collection is a config edit, not a code change: name
it under `postman.collections`, then place its folders in `postman.phases`.

Teardown works the same way round. A collection that can delete what it created
is asked to first (`postman.teardown`); everything left — Keycloak accounts,
exchanges, queues, buckets, database rows, and every catalogue item a collection
has no delete for — falls to the same sweep `main/e2e.py` uses, which finds
artefacts by this run's namespace rather than by ids that go missing when a
phase dies partway through.

Reports: newman's own HTML per phase, and one report over the whole run that
links to them.
"""

import argparse
import contextlib
import copy
import fnmatch
import html
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The workflow package lives under script/, and is imported by its own name —
# the same path setup main/e2e.py does, for the same reason.
sys.path.insert(0, str(ROOT / "script"))

from ControlPlane_Workflow import cleanup  # noqa: E402
from ControlPlane_Workflow import client as client_module  # noqa: E402
from ControlPlane_Workflow import config as config_module  # noqa: E402
from ControlPlane_Workflow import flow  # noqa: E402
from ControlPlane_Workflow import ngsild_publish  # noqa: E402
from ControlPlane_Workflow.client import Recorder, field, redact_text, rows_of  # noqa: E402
from ControlPlane_Workflow.flow import ORG_APPROVAL_ROLES, RunContext, User  # noqa: E402

HERE = Path(__file__).resolve().parent

# Users this harness creates on top of the ones the workflow already needs.
# Every Postman persona has to resolve to an account this run owns and can
# delete afterwards — borrowing the dev accounts the shipped environment names
# would put test traffic on somebody's real history.
#
# Three consumers, because a collection that tests the user API *changes the
# account it is pointed at*: folder 03 changes a password and rewrites a
# profile, folder 04 revokes a KYC. Run against one shared consumer, those
# requests break the account the end-to-end chain depends on — the access
# request, the policy, the data-plane reads and the audit trail all belong to
# one consumer, and the audit trail in particular only means anything if it is
# the trail of the account that did the reading. So the consumers are given
# jobs instead of being interchangeable; see CONSUMER_ROLES.
EXTRA_USER_KEYS = ("member", "delegate", "consumer2", "consumer3")

# What each consumer is for. Enforced by the `personas` key on a phase, which
# points the collection's consumer variables at a different account for the
# length of that folder.
CONSUMER_ROLES = {
    "consumer": "the end-to-end chain — access request, policy, data-plane "
                "reads, audit trail. Nothing may change its credentials.",
    "consumer2": "a second consumer where a collection wants two, and the "
                 "target for folders that alter account state (KYC).",
    "consumer3": "the one folders may break — password changes and profile "
                 "rewrites are pointed here.",
}

# Postman personas, mapped onto this run's accounts:
#
#     (username variable, password variable, our user key)
#
# Several personas share one account on purpose. `new_user` creates the
# organisation and is granted org_admin and provider by the approval, which is
# exactly what `org_admin`, `admin_user` and `provider_user` then mean — one
# account, seen from four sides. A collection that later separates them only
# needs a new entry here.
PERSONA_ACCOUNTS = (
    ("cos_admin", "cos_admin_pass", "cosadmin"),
    ("cosadmin_user", "cosadmin_pass", "cosadmin"),
    ("new_user", "new_user_pass", "requester"),
    ("org_admin", "org_admin_pass", "requester"),
    ("orgadmin_user", "orgadmin_pass", "requester"),
    ("admin_user", "admin_pass", "requester"),
    ("provider_user", "provider_pass", "requester"),
    ("org_admin1", "org_admin_pass1", "requester"),
    ("consumer_user", "consumer_pass", "consumer"),
    ("user", "user_pass", "consumer"),
    ("consumer2_user", "consumer2_pass", "consumer2"),
    ("consumer3_user", "consumer3_pass", "consumer3"),
    ("member_user", "member_pass", "member"),
    ("org_mem", "org_mem_pass", "member"),
    ("no_org_user", "no_org_pass", "nopolicy"),
    ("delegate_user", "delegate_pass", "delegate"),
)

# Token variables the collections read, and whose token belongs in each. These
# are refreshed before every phase: a role granted mid-run does not appear in a
# token minted before the grant, and Keycloak's access tokens are short-lived.
PERSONA_TOKENS = {
    "cosadmin_token": "cosadmin",
    "cos_token": "cosadmin",
    # A second name for the same account, read by the compute-report requests.
    # Both are positive cases, so they want a cos_admin — not a *different* one.
    "cosadmin2_token": "cosadmin",
    "provider_token": "requester",
    "org_admin_token": "requester",
    "orgadmin_token": "requester",
    "new_user_token": "requester",
    "delegator_token": "requester",
    "orgmem_token": "member",
    "consumer_token": "consumer",
    # The data-plane collection spells the same thing in camel case, and its
    # pre-request scripts read it directly rather than through {{...}} — so it
    # has to be a name the run fills, in both scopes, or every request in every
    # data-plane folder goes out as "Bearer undefined".
    "consumerToken": "consumer",
    "user_token": "consumer",
    "consumer2_token": "consumer2",
    "consumer3_token": "consumer3",
    "delegate_token": "delegate",
}

# Identifiers the collections read, and whose account each names. Listed rather
# than written out inline because a phase can point any of them at a different
# account — `/admin/{{consumer_user_id}}/update` has to address the same
# consumer the rest of that folder signs in as, or it rewrites the wrong person.
PERSONA_IDS = (
    ("consumer_user_id", "consumer"),
    ("userId", "consumer"),
    ("ownerUserId", "requester"),
    ("delegate_id", "delegate"),
)

# How long a minted token is reused before a phase mints a fresh one. Keycloak
# hands out five-minute access tokens on the deployments this runs against, and
# a phase can take longer than what is left of one.
TOKEN_TTL_SECONDS = 120


def _log(message):
    print(f"    {message}", flush=True)


def _banner(text):
    print(f"\n{text}", flush=True)


# ------------------------------------------------------------------ context


class CompleteTestContext(RunContext):
    """The workflow's run context, plus everything the Postman side needs.

    Deliberately a subclass rather than a parallel structure: teardown, the
    namespace sweep, the data-plane scripts and the resource-server reads all
    take a RunContext, and they should keep working here unchanged. What is
    added is the Postman half — the personas the collections address, the
    variables handed to newman, and what came back.
    """

    def __init__(self, config, recorder):
        super().__init__(config, recorder)

        password = config["run"]["user_password"]
        domain = config["run"]["email_domain"]
        for key in EXTRA_USER_KEYS:
            username = f"{self.namespace}-{key}@{domain}"
            self.users[key] = User(key, username, username, password)

        # Postman state, carried between phases. The environment and the
        # collection are both chained: a collection's tests write to either,
        # and a phase that could not see the previous phase's writes would
        # break every chained request.
        self.env_values = {}
        self.collection_paths = {}
        self.env_path = None
        # The values the phase about to run was seeded with.
        self.seeded_values = {}
        # What that phase pinned for its own length — see phase_variables().
        self.pinned_variables = {}

        # When each persona's token was minted, so a phase re-mints only what
        # has gone stale.
        self.token_minted = {}

        # One entry per phase, for the report.
        self.phases = []
        # Catalogue items and organisations the collections created, over and
        # above this run's own — swept at teardown by the ids they announced.
        self.postman_items = []
        self.postman_orgs = []
        # Steps a script had to fill in because the collection's own flow did
        # not leave the platform in the state the next phase needed.
        self.gaps_filled = []
        # Things worth fixing in a collection, found by reading it rather than
        # by running it.
        self.collection_notes = []
        # What teardown could not remove, for the report.
        self.teardown_problems = []

    def token_for(self, key):
        """A usable token for one of this run's accounts, minted on demand."""
        user = self.users.get(key)
        if not user:
            return None
        minted = self.token_minted.get(key, 0)
        if user.token and time.monotonic() - minted <= TOKEN_TTL_SECONDS:
            return user.token

        try:
            user.token = self.kc.user_token(user.username, user.password)
        except Exception as err:  # noqa: BLE001 - recovered from just below
            if not self._recover_password(user, err):
                raise
            user.token = self.kc.user_token(user.username, user.password)
        self.token_minted[key] = time.monotonic()
        return user.token

    def _recover_password(self, user, err):
        """Give a persona a working password again after a collection changed it.

        `PUT /auth/user/password` is one of the cases under test, and the
        account it runs against is one of this run's. Once it succeeds the
        harness is holding a credential that no longer works, and every phase
        after it fails to sign that persona in — for a reason that has nothing
        to do with what those phases test.

        The new password is a fresh one rather than the original. Realms carry a
        password-history policy (dev does), and resetting an account back to the
        password it had ten seconds ago is exactly what that policy exists to
        refuse — it answers 400 and the recovery fails at the one moment it is
        needed. A password the account has never held is always acceptable.

        The rotated value is stored on the user, so it flows into the generated
        Postman environment on the next phase and the collections keep signing
        that persona in too.

        Anything that is not an invalid_grant — Keycloak unreachable, the
        account deleted — is left alone for the caller to raise.
        """
        if "invalid_grant" not in str(err) or not user.user_id:
            return False

        # Belt and braces. Only accounts this run created ever reach here —
        # they are the only ones in ctx.users — but resetting a password is
        # irreversible for whoever owns it, and "the data structure cannot
        # contain that account" is a weaker promise than refusing by name. A
        # borrowed cos_admin or OGC provider is a real person's login.
        usernames, ids = cleanup.protected_accounts(self)
        if (user.username or "").lower() in usernames or str(user.user_id).lower() in ids:
            _log(f"refusing to change the password of {user.username} — a borrowed account")
            return False

        _log(f"{user.key}'s password was changed by a collection; issuing a new one")

        # The original first, in case nothing changed it and this is a
        # different problem; then a fresh one, which no policy can refuse for
        # being a repeat.
        problems = []
        for candidate in (user.password, self._rotated_password()):
            try:
                self.kc.reset_password(user.user_id, candidate)
            except Exception as reset_err:  # noqa: BLE001 - try the next one
                problems.append(str(reset_err)[:200])
                continue
            user.password = candidate
            note = (
                f"password of {user.key} — a collection changed it through "
                "PUT /auth/user/password, so the harness issued a new one to "
                "keep the remaining phases signing in"
            )
            if note not in self.gaps_filled:
                self.gaps_filled.append(note)
            return True

        _log(f"could not reset it: {' | '.join(problems)}")
        return False

    def _rotated_password(self):
        """A password this account has never held, in the shape of the configured one.

        Keeping the configured password as the stem keeps whatever realm policy
        it was chosen to satisfy — length, case, digit, symbol — and the random
        tail is what makes it new.
        """
        return f"{self.config['run']['user_password']}{secrets.token_hex(3)}"

    def invalidate_tokens(self):
        """Force the next read of every token to mint a fresh one.

        Called after anything that changes a role: roles live in the token, and
        a token minted before a grant does not carry what the grant gave.
        """
        self.token_minted.clear()


# ------------------------------------------------------------------- newman


class NewmanMissing(RuntimeError):
    pass


class PhaseError(RuntimeError):
    """A phase that cannot run: a folder that is not there, a step that is not
    known. Raised rather than exiting, because teardown still has to happen —
    a run that abandons its artefacts because a folder was renamed is worse
    than the renamed folder."""


def newman_command(config):
    """How to invoke newman: an explicit path, then PATH, then a local install.

    A local install is preferred over asking everyone to keep a global newman at
    the right version — the harness pins it in complete-test/package.json, so a
    checkout runs the same runner the last person did.
    """
    postman = config["postman"]
    explicit = postman.get("newman_bin")
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return [str(path)]
        raise NewmanMissing(f"postman.newman_bin does not exist: {explicit}")

    local = install_dir(config) / "node_modules" / ".bin" / "newman"
    if local.is_file():
        return [str(local)]

    found = shutil.which("newman")
    if found:
        return [found]

    if postman.get("auto_install"):
        install_newman(config)
        if local.is_file():
            return [str(local)]

    raise NewmanMissing(
        "newman was not found. Install it beside the harness with\n"
        f"    npm install --prefix {install_dir(config)}\n"
        "or set postman.newman_bin to an existing newman."
    )


def install_dir(config):
    return Path(config["postman"].get("install_dir") or HERE).expanduser().resolve()


class RunLock:
    """One run at a time per work directory.

    Everything a phase hands to newman goes through fixed filenames in the work
    directory — `environment.json` above all, which is rewritten before every
    phase. Two runs sharing that directory therefore overwrite each other's
    environments mid-flight, and the symptom is not an error: it is a phase
    reading the *other* run's variables and failing on a URL that never resolved
    — a gateway read pointed at the control plane, an id left as a literal
    `{{name}}`. That is indistinguishable from a broken collection until the
    timestamps are compared, which is an evening nobody should repeat.

    So a run takes a lock and a second one refuses, naming what holds it. A lock
    whose process is gone is stale and is taken over — a killed run must not
    block the next one.
    """

    def __init__(self, config):
        self.path = work_dir(config) / "run.lock"
        self.held = False

    def held_by(self):
        """(pid, detail) of the live run holding this, or None."""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        pid = payload.get("pid")
        if not isinstance(pid, int):
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None  # stale: the process that wrote it is gone
        except PermissionError:
            pass  # alive, and somebody else's — which is exactly the case to refuse
        started = payload.get("started", "?")
        return pid, f"namespace {payload.get('namespace', '?')}, started {started}"

    def acquire(self, ctx):
        existing = self.held_by()
        if existing and not ctx.config["postman"].get("ignore_run_lock"):
            pid, detail = existing
            raise PhaseError(
                f"another run is using {self.path.parent} (pid {pid}, {detail}).\n"
                "Two runs in one work directory overwrite each other's Postman "
                "environment between phases, so this one would report failures "
                "that are not real. Wait for it, or give this run a directory of "
                "its own with --set run.report_dir=<dir>, or override with "
                "--set postman.ignore_run_lock=true."
            )
        if existing:
            _log(f"taking over the run lock from pid {existing[0]} — asked to ignore it")
        elif self.path.exists():
            _log("stale run lock (its process is gone) — taking it")
        _write_private_json(self.path, {
            "pid": os.getpid(),
            "namespace": ctx.namespace,
            "started": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        self.held = True

    def release(self):
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False


def newman_html_reporter_missing(config):
    """Whether newman's HTML reporter is asked for but not installed.

    newman resolves a reporter by name at run time and reports its absence per
    process, which would be one error line per phase and a run with no HTML at
    the end of it. Checked once instead, before anything runs.
    """
    if not config["postman"].get("newman_html_report"):
        return False
    local = install_dir(config) / "node_modules" / "newman-reporter-htmlextra"
    return not local.is_dir()


def install_newman(config):
    """npm install newman and its HTML reporter beside the harness."""
    target = install_dir(config)
    _log(f"installing newman into {target}")
    result = subprocess.run(
        ["npm", "install", "--prefix", str(target)],
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        raise NewmanMissing(
            "npm install failed:\n" + redact_text(result.stderr or result.stdout)[-2000:]
        )


# ------------------------------------------------------- collection loading


def load_collection(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise PhaseError(f"{path} is not a valid Postman collection: {err}")


def collection_for(ctx, phase):
    """The collection a phase names, or the only enabled one when it names none."""
    postman = ctx.config["postman"]
    collections = postman["collections"]
    enabled = {k: v for k, v in collections.items() if isinstance(v, dict) and v.get("enabled")}
    # A phase names a collection only when it is not the default one, so the
    # control plane's twenty-odd folders stay readable and a data-plane folder
    # says what it belongs to.
    key = phase.get("collection") or postman.get("default_collection")
    if key:
        if key not in enabled:
            raise PhaseError(
                f"phase {phase.get('name')!r} names collection {key!r}, which is "
                f"not enabled. Enabled: {', '.join(sorted(enabled)) or 'none'}"
            )
        return key, enabled[key]
    if len(enabled) == 1:
        key = next(iter(enabled))
        return key, enabled[key]
    raise PhaseError(
        f"phase {phase.get('name')!r} must name a collection — "
        f"{len(enabled)} are enabled"
    )


def find_folder(collection, wanted):
    """A folder in the collection, matched leniently.

    Exact name first, then the numeric prefix the folders are ordered by, then a
    case-insensitive substring. The prefix match is what matters in practice:
    the folders get renamed as the collection is restructured by phase, and a
    config that says "02" should keep finding organisation management when
    "02 – Organisation Management" becomes "02 – Organisations".
    """
    folders = _all_folders(collection)
    for folder in folders:
        if folder.get("name") == wanted:
            return folder
    for folder in folders:
        name = folder.get("name", "")
        if name.split()[0:1] == [wanted] or name.startswith(f"{wanted} ") or name.startswith(f"{wanted}-"):
            return folder
    lowered = str(wanted).lower()
    matches = [f for f in folders if lowered in f.get("name", "").lower()]
    if len(matches) == 1:
        return matches[0]
    raise PhaseError(
        f"no folder matching {wanted!r} in the collection. Folders: "
        + ", ".join(f.get("name", "?") for f in folders)
    )


def folder_for_phase(collection, key, phase):
    """What one phase hands to newman: a folder, or the whole collection.

    A phase names a folder because that is the unit the report is read in, and
    because a folder is where a collection's negative cases live together. But a
    collection whose chain runs *between* its folders cannot be split that way:
    each phase is its own newman process, and a token or a refresh token one
    folder stored is gone by the time the next process starts — only uuid-valued
    collection variables survive, through the mirror. Such a collection names no
    folder at all and runs in one process, exactly as pressing Run in Postman
    would.

    Returns (node, whole). The node is shaped like a folder either way, so
    everything downstream — filtering, the request count, the report — is
    unchanged; `whole` says whether its children are the collection's own top
    level, which are kept as top-level folders so the report still reads folder
    by folder.
    """
    wanted = phase.get("folder")
    if wanted not in (None, "", "*"):
        return find_folder(collection, wanted), False
    name = (collection.get("info") or {}).get("name") or key
    return {"name": name, "item": collection.get("item") or []}, True


def _all_folders(collection):
    """Every folder in the collection, at any depth, outermost first.

    Depth matters for the collections still to come: one that groups its
    folders under a server name would put "01 – Upload" a level down, and a
    lookup that only saw the top level would report it missing.
    """
    found = []

    def visit(items):
        for item in items or []:
            if "item" in item:
                found.append(item)
                visit(item["item"])

    visit(collection.get("item"))
    return found


def _leaf_requests(node):
    """Every request under a folder, depth first, in document order."""
    if "item" not in node:
        return [node]
    found = []
    for child in node["item"]:
        found.extend(_leaf_requests(child))
    return found


def _matches_any(name, patterns):
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


# A request whose own name says it expects a client error — "Delete 404",
# "Delete 401" — destroys nothing: it is refused or it addresses an id that was
# never there. Those stay, so switching the deletes off costs no negative-path
# coverage. Anything else is treated as destructive.
_NAME_EXPECTS_FAILURE = re.compile(r"\b[45]\d\d\b")


# Held back on top of `destructive_requests` when an as-shipped run keeps
# `as_shipped_skip_deletes` on. Only the verbs that remove something: a create
# or an update in this mode does what it would do if somebody pressed Run in
# Postman, which is the thing being reproduced, but a delete keyed on a shipped
# id removes a row nobody meant to lose.
_SHIPPED_DELETE_PATTERNS = ["*Delete*", "*DELETE*", "*Remove*", "*Revoke*", "*Deactivate*"]


def _is_destructive(item, extra_patterns):
    """Whether running this request would remove something the run created."""
    method = str(((item.get("request") or {}).get("method") or "")).upper()
    name = item.get("name", "")
    if method != "DELETE" and not _matches_any(name, extra_patterns or []):
        return False
    return not _NAME_EXPECTS_FAILURE.search(name)


def _filter_items(node, include, skip, destructive=None):
    """A copy of a folder holding only the requests a phase asked for.

    Folders keep their own scripts and auth, so the node is filtered rather than
    flattened — a folder-level pre-request script the collection relies on would
    otherwise silently stop running.
    """
    if "item" not in node:
        name = node.get("name", "")
        if include and not _matches_any(name, include):
            return None
        if skip and _matches_any(name, skip):
            return None
        if destructive is not None and _is_destructive(node, destructive):
            return None
        return copy.deepcopy(node)

    kept = [
        child
        for child in (_filter_items(c, include, skip, destructive) for c in node["item"])
        if child
    ]
    if not kept:
        return None
    out = {k: copy.deepcopy(v) for k, v in node.items() if k != "item"}
    out["item"] = kept
    return out


def namespace_bodies(collection, namespace, fields):
    """Stamp the run namespace onto the names a collection hard-codes.

    A collection creates artefacts under fixed names — "AutoTestOrg", the
    Postman weather databank — and a fixed name is not sweepable: teardown finds
    what a run created by matching this run's prefix, and a name shared with
    every other run cannot be matched without risking somebody else's data.

    Only the top-level name is rewritten. Names nested inside a body mean other
    things — a resourceServer entry's name is the server's, not the item's — and
    rewriting those would break the request rather than namespace it.

    Bodies a pre-request script builds at run time are out of reach here; those
    artefacts are torn down by the ids captured from the exported environment.
    Returns how many bodies were stamped.
    """
    stamped = 0

    def visit(node):
        nonlocal stamped
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if not isinstance(node, dict):
            return
        if "item" in node:
            visit(node["item"])
            return
        body = (node.get("request") or {}).get("body") or {}
        raw = body.get("raw")
        if body.get("mode") != "raw" or not isinstance(raw, str) or not raw.strip():
            return
        rewritten = _stamp_name(raw, namespace, fields)
        if rewritten is not None:
            body["raw"] = rewritten
            stamped += 1

    visit(collection.get("item", []))
    return stamped


def _stamp_name(raw, namespace, fields):
    """The body with its top-level name prefixed, or None if nothing changed."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Bodies in this collection carry // comments, which is not JSON. The
        # first name in the text is the top-level one in every body seen so far,
        # so it is rewritten in place rather than through a parse.
        for name in fields:
            pattern = re.compile(r'("%s"\s*:\s*")([^"]*)(")' % re.escape(name))
            match = pattern.search(raw)
            if match and not match.group(2).startswith(namespace):
                return pattern.sub(
                    lambda m: f"{m.group(1)}{namespace}-{m.group(2)}{m.group(3)}", raw, count=1
                )
        return None

    if not isinstance(parsed, dict):
        return None
    changed = False
    for name in fields:
        value = parsed.get(name)
        if isinstance(value, str) and value and not value.startswith(namespace):
            parsed[name] = f"{namespace}-{value}"
            changed = True
    return json.dumps(parsed, indent=2) if changed else None


# An id written into a request rather than taken from a variable. The all-zero
# uuid is excluded: it is how a collection deliberately asks for a 404.
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def _brace_error(text):
    """Whether a string has a brace that is not part of a well-formed {{var}}.

    Postman stores a URL twice — as `raw`, and parsed into host, path and query.
    Editing one without the other leaves them disagreeing, and newman sends the
    *parsed* one. So a URL can look right in the collection's raw text and still
    go out with `{base_url}}` as its hostname. Checking only `raw` misses exactly
    the case somebody has just tried to fix.
    """
    remainder = re.sub(r"\{\{[^{}]*\}\}", "", str(text or ""))
    return "{" in remainder or "}" in remainder


def malformed_urls(collection, key, notes):
    """URLs whose braces are broken, in whichever half of the URL holds them.

    Reported before anything is sent, because at run time this surfaces only as
    a DNS failure against a hostname that is really a placeholder — and the
    field that has to be edited is not the one the error names.
    """
    def visit(node, folder):
        if "item" in node:
            for child in node["item"]:
                visit(child, node.get("name", folder))
            return
        url = ((node.get("request") or {}).get("url")) or {}
        if isinstance(url, str):
            url = {"raw": url}
        broken = []
        if _brace_error(url.get("raw")):
            broken.append("raw")
        for component in ("host", "path"):
            value = url.get(component)
            parts = value if isinstance(value, list) else [value]
            if any(_brace_error(part) for part in parts if part is not None):
                broken.append(f"url.{component}")
        for entry in url.get("query") or []:
            if isinstance(entry, dict) and _brace_error(entry.get("value")):
                broken.append(f"url.query[{entry.get('key')}]")
        if not broken:
            return
        shown = url.get("host") if "url.host" in broken else url.get("raw")
        if isinstance(shown, list):
            shown = ".".join(str(x) for x in shown)
        notes.append(
            {
                "collection": key,
                "folder": folder,
                "request": node.get("name", "?"),
                "kind": "malformed URL",
                "detail": f"{', '.join(broken)} — {shown}",
            }
        )

    for folder in collection.get("item", []):
        visit(folder, folder.get("name", "?"))


def _live_lines(text):
    """A script or body with its commented-out lines removed.

    Commented-out lines are not sent, so an id in one is not a finding — and
    both collections carry plenty: a request's old URL, an alternative body,
    the tests somebody disabled while debugging.
    """
    return "\n".join(
        line for line in str(text or "").splitlines() if not line.strip().startswith("//")
    )


def _id_sources(node):
    """Every place in one request a hard-coded id can hide, as (where, text).

    The body was the only one worth reading while the control plane was the
    only collection: it addresses records by id in JSON. The data-plane
    collection addresses them in the URL instead — `?id=04bf3f4e-…`,
    `/…/51ef8ad1-…/download` — and rewrites that URL again from a pre-request
    script, so reading bodies alone would have found three of its twenty-odd.
    """
    request = node.get("request") or {}
    url = request.get("url") or {}
    if isinstance(url, str):
        url = {"raw": url}
    yield "url", url.get("raw")
    for entry in url.get("query") or []:
        if isinstance(entry, dict):
            yield f"url.query[{entry.get('key')}]", entry.get("value")
    yield "body", (request.get("body") or {}).get("raw")
    for event in node.get("event") or []:
        script = (event.get("script") or {}).get("exec") or []
        yield f"{event.get('listen')} script", "\n".join(script)


def hardcoded_ids(collection, key, notes):
    """Ids a collection carries that no run can satisfy.

    A uuid typed into a request is somebody's dev session preserved in amber:
    the row it names existed on their deployment on the day they wrote it, and
    every run since has been asking about a record that is not there. The
    request still goes out and still gets a response, so it is not a broken
    request — it is a test that cannot pass and cannot be fixed by the platform.

    A request whose own name says it expects a client error is skipped. "Get By
    ID 404" naming `a1b2c3d4-…` is not an oversight, it *is* the test: the id
    has to be one that does not resolve. Reporting those buried the handful
    that matter under a dozen that do not.

    Reported, never failed: a collection may hardcode an id on purpose, and only
    the person who wrote it knows which.
    """
    def visit(node, folder):
        if "item" in node:
            for child in node["item"]:
                visit(child, node.get("name", folder))
            return
        name = node.get("name", "?")
        if _NAME_EXPECTS_FAILURE.search(name):
            return
        # One line per id, however many places in the request it appears — the
        # same id in the URL and in the body is one thing to fix, not two.
        seen = {}
        for where, text in _id_sources(node):
            for found in _UUID_ANYWHERE.findall(_live_lines(text)):
                if found.lower() == _ZERO_UUID:
                    continue
                seen.setdefault(found, where)
        for found, where in seen.items():
            notes.append(
                {
                    "collection": key,
                    "folder": folder,
                    "request": name,
                    "kind": "hard-coded id",
                    "detail": f"{found} (in {where})",
                }
            )

    for folder in collection.get("item", []):
        visit(folder, folder.get("name", "?"))


def _rewrite_text(text, rewrites, hits):
    """One string with every configured literal replaced, counting what matched."""
    if not isinstance(text, str):
        return text, False
    changed = False
    for literal, replacement in rewrites.items():
        if literal in text:
            text = text.replace(literal, replacement)
            hits[literal] = hits.get(literal, 0) + 1
            changed = True
    return text, changed


def retarget_collection(collection, key, rewrites, notes):
    """Point a collection's shipped subjects at the ones this run created.

    A collection is written against a deployment somebody had in front of them,
    and the parts of that which live in variables are already replaced — but the
    resource id in `?id=04bf3f4e-…`, the one in `/…/51ef8ad1-…/download`, and
    the `timeAt`/`endTimeAt` of a `between` query are written into the URL
    itself. Nothing the environment carries can reach them, so those requests
    ask about somebody else's data no matter what the run has published.

    Each rewrite is one literal to one replacement, listed in config, and is
    applied to the URL, the body and the scripts alike — the collection builds
    URLs and bodies in pre-request scripts as often as it declares them, and a
    rewrite that reached only the declared half would leave the two disagreeing.

    Deliberately literal, never pattern-matched. The negative cases are built
    out of ids that look exactly like the positive ones — `…ea5c` is the
    resource the 200 reads and `…ea5a` is the one the 403 may not — and a rule
    clever enough to find both would destroy the distinction the folder exists
    to test. What is rewritten is what somebody named.

    Returns how many strings changed.
    """
    if not rewrites:
        return 0
    hits, changed = {}, 0
    # Which folders each literal turned up in, so one line in the report says
    # where a rewrite reached rather than one line per request saying nothing.
    where = {}

    def visit(node, folder):
        nonlocal changed
        if "item" in node:
            for child in node["item"]:
                visit(child, node.get("name", folder))
            return

        touched = False
        before = dict(hits)
        request = node.get("request") or {}

        url = request.get("url")
        if isinstance(url, str):
            request["url"], hit = _rewrite_text(url, rewrites, hits)
            touched |= hit
        elif isinstance(url, dict):
            # `raw` and the parsed halves are two copies of one URL and newman
            # sends the parsed one, so both are rewritten or neither is.
            for field in ("raw",):
                if field in url:
                    url[field], hit = _rewrite_text(url[field], rewrites, hits)
                    touched |= hit
            for field in ("host", "path"):
                parts = url.get(field)
                if isinstance(parts, list):
                    for index, part in enumerate(parts):
                        parts[index], hit = _rewrite_text(part, rewrites, hits)
                        touched |= hit
            for entry in url.get("query") or []:
                if isinstance(entry, dict) and "value" in entry:
                    entry["value"], hit = _rewrite_text(entry["value"], rewrites, hits)
                    touched |= hit

        body = request.get("body") or {}
        if "raw" in body:
            body["raw"], hit = _rewrite_text(body["raw"], rewrites, hits)
            touched |= hit

        for event in node.get("event") or []:
            # Pre-request scripts only. That is where a collection builds the
            # URL or the body it is about to send, so it is the third copy of
            # the same subject. A *test* script naming the id is asserting
            # something about it, and rewriting an assertion would be changing
            # what the suite claims rather than what it asks.
            if event.get("listen") != "prerequest":
                continue
            script = event.get("script") or {}
            lines = script.get("exec")
            if isinstance(lines, list):
                for index, line in enumerate(lines):
                    lines[index], hit = _rewrite_text(line, rewrites, hits)
                    touched |= hit

        if touched:
            changed += 1
            for literal in hits:
                if hits[literal] != before.get(literal):
                    where.setdefault(literal, []).append(folder)

    for folder in collection.get("item", []):
        visit(folder, folder.get("name", "?"))

    for literal, count in hits.items():
        folders = sorted(dict.fromkeys(where.get(literal, [])))
        _log(f"{key}: rewrote {literal} -> {rewrites[literal]} in {count} place(s)")
        notes.append(
            {
                "collection": key,
                "folder": ", ".join(folders) or "?",
                "request": f"{len(where.get(literal, []))} request(s)",
                "kind": "retargeted",
                "detail": f"{literal} -> {rewrites[literal]}",
            }
        )
    for literal in rewrites:
        if literal not in hits:
            # Worth saying, and worth a row: a rewrite that matches nothing is
            # usually a collection re-exported with the subject changed, and its
            # requests are quietly back to reading somebody else's data.
            _log(f"{key}: nothing to rewrite for {literal} — check the config against the collection")
            notes.append(
                {
                    "collection": key,
                    "folder": "—",
                    "request": "no request",
                    "kind": "rewrite matched nothing",
                    "detail": f"{literal} is configured but no longer appears in the collection",
                }
            )
    return changed


_READS_COLLECTION_VAR = re.compile(r"""pm\.collectionVariables\.get\(\s*["']([^"']+)["']""")


def declare_read_variables(collection, key, notes):
    """Declare the collection variables a script reads but nobody defined.

    Postman resolves `{{x}}` from the environment when the collection has no
    such variable. `pm.collectionVariables.get("x")` does not — it reads one
    scope and returns undefined, and the script goes on to build
    `"Bearer " + undefined` out of it. Every request in the folder then 401s,
    which reads as a credentials problem and is not one.

    The harness fills a collection variable only where the collection declares
    it (collection_variables_for_phase deliberately fills, never invents), so a
    name that is read but never declared is out of reach. Declaring it empty
    here is what puts it in reach: the run's own value is written into it before
    each phase, exactly as for every other one.

    Returns how many were added.
    """
    declared = {v.get("key") for v in collection.get("variable") or [] if v.get("key")}
    read = set()

    def visit(node):
        for event in node.get("event") or []:
            lines = (event.get("script") or {}).get("exec") or []
            read.update(_READS_COLLECTION_VAR.findall("\n".join(lines)))
        for child in node.get("item") or []:
            visit(child)

    visit(collection)
    missing = sorted(read - declared)
    for name in missing:
        collection.setdefault("variable", []).append({"key": name, "value": ""})
        notes.append(
            {
                "collection": key,
                "folder": "—",
                "request": "collection variables",
                "kind": "undeclared variable",
                "detail": f"{name} is read by a script but the collection defines no "
                          f"such variable; declared empty so this run can fill it",
            }
        )
    return len(missing)


def sanitise_request_auth(collection, key, notes):
    """Point a request's own Basic auth at this run's application.

    A collection variable is not the only place a credential hides. The
    data-plane collection's app-id request carries the pair in the request's
    `auth` block, as literals — somebody's real application on the deployment,
    with its real secret, typed in and saved.

    Emptying it the way a variable is emptied would send `Basic Og==` and prove
    nothing, so it is re-pointed instead: at `{{app_id}}` and `{{app_secret}}`,
    which the control-plane collection's app-management folder sets earlier in
    the run. The request then exercises the same endpoint with an application
    this run owns. Where no application was created the pair resolves empty and
    the request gets a 401 — the honest result, and a visible one.

    Only a Basic pair whose username is a uuid is touched: that is the shape of
    a DX application id, and it cannot be confused with a `user:password` a
    collection means literally.

    Returns how many were re-pointed.
    """
    swapped = 0

    def visit(node, folder):
        nonlocal swapped
        if "item" in node:
            for child in node["item"]:
                visit(child, node.get("name", folder))
            return
        auth = (node.get("request") or {}).get("auth") or {}
        if auth.get("type") != "basic":
            return
        entries = {e.get("key"): e for e in auth.get("basic") or [] if isinstance(e, dict)}
        username, password = entries.get("username"), entries.get("password")
        if not username or not _UUID.match(str(username.get("value", "")).strip()):
            return
        client_module.register_secrets(
            [str(e.get("value", "")).strip() for e in (username, password) if e]
        )
        username["value"] = "{{app_id}}"
        if password:
            password["value"] = "{{app_secret}}"
        swapped += 1
        notes.append(
            {
                "collection": key,
                "folder": folder,
                "request": node.get("name", "?"),
                "kind": "credential in request",
                "detail": "Basic auth held a literal application id and secret; "
                          "re-pointed at {{app_id}} / {{app_secret}}",
            }
        )

    for folder in collection.get("item", []):
        visit(folder, folder.get("name", "?"))
    return swapped


def sanitise_collection_variables(collection):
    """Strip ids, tokens and passwords out of the collection's own variables.

    The environment is not the only place a value can come from. Postman falls
    back to a *collection* variable when the environment does not define one, and
    a published collection carries a full set of them — `app_id`,
    `credit_request_id`, `item_id`, live bearer tokens — left over from whoever
    last ran it.

    Dropping them from the environment while leaving them here would be a hole
    in exactly the guard that matters: a folder whose DELETE resolves
    `{{app_id}}` before any test has set it would fall through to the shipped
    value and remove a real application on the deployment. Every id this run
    uses has to be one this run produced, whichever scope it is read from.

    Returns how many were dropped.
    """
    kept, dropped = [], 0
    for variable in collection.get("variable") or []:
        key, value = variable.get("key"), variable.get("value")
        if not key:
            continue
        text = value if isinstance(value, str) else ""
        # A multi-line value is code, not a credential. The collection keeps its
        # token-fetching helper in a variable called `getTokenFn`, which a
        # name-based rule would read as a secret.
        if _is_credential(key) and text.strip() and "\n" not in text:
            client_module.register_secrets([text.strip()])
            # Emptied, never removed. Requests read these directly —
            # `"Bearer " + pm.collectionVariables.get("cosadmin_token")` — so a
            # missing variable does not fall back to the environment, it becomes
            # the string "Bearer undefined" and every request in the folder gets
            # a 401. The value is replaced with this run's own before each
            # phase; see collection_variables_for_phase.
            variable = dict(variable, value="")
            dropped += 1
            kept.append(variable)
            continue
        # Only a value that really is an id gets neutralised. `client_id` ends
        # in `_id` but holds `frontend-client`, and zeroing it would replace a
        # setting with a uuid that means nothing.
        if _UUID.match(text.strip()):
            # Neutralised rather than removed. Removing it leaves a request that
            # reads the variable before any test has set it resolving to the
            # literal `{{rs_id}}` — a malformed URL, and a response the
            # collection's own assertions then crash on, turning a clean
            # negative case into a heap of TypeErrors. The all-zero uuid is what
            # the collection itself uses to ask for a 404, so those cases stay
            # meaningful while naming nothing that exists.
            variable = dict(variable, value=_ZERO_UUID)
            dropped += 1
        kept.append(variable)
    collection["variable"] = kept
    return dropped


# Mirrors every uuid-valued collection variable into the environment, after
# each request. Injected as a collection-level test script.
_MIRROR_SCRIPT = """
// Injected by complete_test.py — see mirror_collection_variables().
// newman exports the environment a run wrote, but not the collection variables
// it wrote, and a phase is one newman process. Anything a test stored with
// pm.collectionVariables.set() would therefore be lost the moment the folder
// ended. Copying the ids into the environment is what carries them to the next
// phase and to teardown.
(function () {
    var uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    try {
        pm.collectionVariables.toObject && Object.keys(
            pm.collectionVariables.toObject()
        ).forEach(function (key) {
            var value = pm.collectionVariables.get(key);
            if (typeof value === 'string' && uuid.test(value.trim())) {
                // Under a prefixed name, never the real one. Writing the real
                // name would change which id the *next request in this folder*
                // resolves — the environment outranks a collection variable —
                // and a negative case that stores a deliberately wrong id would
                // then poison every request after it. Prefixed, nothing in the
                // collection can resolve these; they are a record for teardown
                // and nothing else.
                pm.environment.set('__cv_' + key, value.trim());
            }
        });
    } catch (e) { /* never fail a request over bookkeeping */ }
})();
"""


def mirror_collection_variables(collection):
    """Make collection-variable writes survive the end of a phase.

    Each phase is its own newman process, and `--export-collection` writes back
    the collection as it was given — runtime `pm.collectionVariables.set()`
    writes are not in it. The environment export does carry runtime writes, so
    the ids are mirrored across into it after every request.

    Only uuid-valued variables are copied. That is the class this needs — the
    ids a `when` guard checks and a later folder chains on — and it cannot
    disturb a host, a token or a flag.
    """
    events = collection.setdefault("event", [])
    for event in events:
        if event.get("listen") == "test":
            script = event.setdefault("script", {})
            lines = script.setdefault("exec", [])
            if any("mirror_collection_variables" in line for line in lines):
                return
            lines.extend(_MIRROR_SCRIPT.splitlines())
            script.setdefault("type", "text/javascript")
            return
    events.append(
        {
            "listen": "test",
            "script": {"type": "text/javascript", "exec": _MIRROR_SCRIPT.splitlines()},
        }
    )


def as_shipped(config):
    """Is this a run of the collections exactly as they were handed over?

    Two different questions, and only one of them is what the harness normally
    asks. The default run points every collection at artefacts *this run*
    created — its own accounts, its own item, its own window — and answers "does
    the chain work end to end". This mode answers the other one: "does the
    collection pass against the deployment on its own terms", which is what
    somebody hitting Run in Postman sees, reproduced in CI.

    It is not a lighter version of the normal run. It is the same collections
    with every retargeting switched off, so the shipped environment file is the
    only source of ids and credentials.
    """
    return bool(config["postman"].get("as_shipped"))


def postman_enabled(config):
    """Is newman part of this run at all?

    Off is a mode, not a broken run. The script phases build the same chain —
    accounts, organisation, item, data, policy, audit trail — and prove the flow
    end to end without handing a single folder to newman. That is what you want
    when newman is not installed, when the collections are mid-edit, or when the
    question is about the platform rather than about the suite.

    Everything downstream of a folder therefore has to stand down together: the
    working copies are not written, every postman phase is skipped by name
    rather than failed, the teardown DELETE folders are skipped with them, and
    no newman report is produced — a report of nothing is worse than no report.
    What still runs is the run report and the exported Postman environment,
    because both describe what the run did, which is the whole point of leaving
    the flow itself intact.
    """
    return bool(config["postman"].get("enabled", True))


def prepare_collections(ctx):
    """Write one working copy of every enabled collection, namespaced.

    The working copy is what newman is given, and what each phase writes its
    collection variables back into — so the copy carries state forward while the
    file under resource/ is never touched.
    """
    work = work_dir(ctx.config)
    _clear_work_dir(work)
    postman = ctx.config["postman"]
    for key, entry in postman["collections"].items():
        if not isinstance(entry, dict) or not entry.get("enabled"):
            continue
        source = config_module.resolve_path(entry["collection"])
        collection = load_collection(source)
        mirror_collection_variables(collection)
        added = declare_read_variables(collection, key, ctx.collection_notes)
        if added:
            _log(f"{key}: declared {added} collection variable(s) that scripts read "
                 f"but the collection did not define")
        if as_shipped(ctx.config):
            # Everything below this line exists to point the collection at this
            # run's own artefacts. In as-shipped mode that is precisely what
            # must not happen: the shipped ids and credentials *are* the
            # subject. Left alone, deliberately and all together — sanitising
            # half of it would produce a collection that is neither.
            _log(f"{key}: left as shipped — no sanitising, no retargeting, no namespacing")
        else:
            dropped = sanitise_collection_variables(collection)
            if dropped:
                _log(f"{key}: dropped {dropped} shipped collection variable(s) pointing at existing platform data")
            swapped = sanitise_request_auth(collection, key, ctx.collection_notes)
            if swapped:
                _log(f"{key}: re-pointed {swapped} request-level credential(s) at this run's application")
            retargeted = retarget_collection(
                collection, key, entry.get("rewrite") or {}, ctx.collection_notes
            )
            if retargeted:
                _log(f"{key}: retargeted {retargeted} request(s) at this run's own subjects")
        malformed_urls(collection, key, ctx.collection_notes)
        hardcoded_ids(collection, key, ctx.collection_notes)
        broken_urls = [
            n for n in ctx.collection_notes
            if n["kind"] == "malformed URL" and n["collection"] == key
        ]
        if broken_urls:
            _log(f"{key}: {len(broken_urls)} request(s) have a malformed URL and cannot be sent:")
            for note in broken_urls:
                _log(f"  {note['request']} — {note['detail']}")
        if postman.get("namespace_names") and not as_shipped(ctx.config):
            stamped = namespace_bodies(
                collection, ctx.namespace, postman.get("namespace_fields") or ["name"]
            )
            if stamped:
                _log(f"{key}: namespaced {stamped} request body name(s)")
        target = work / f"collection-{key}.json"
        _write_private_json(target, collection)
        ctx.collection_paths[key] = target
        _log(f"{key}: {source.name} -> {target.name}")


# ------------------------------------------------------------- environment


def _clear_work_dir(work):
    """Start each run with only this run's working files in there.

    The names are derived from the phase names, so a run overwrites its
    predecessor's files — but only for the phases it still has. Renaming a phase
    or disabling a folder would otherwise leave the previous run's collection,
    environment and JSON sitting beside the current ones, indistinguishable from
    them and holding that run's tokens.
    """
    for pattern in ("run-*.json", "vars-*.json", "env-*.json", "newman-*.json",
                    "newman-*.log", "collection-*.json", "environment.json"):
        for path in work.glob(pattern):
            path.unlink(missing_ok=True)


def work_dir(config):
    configured = config["postman"].get("work_dir")
    if configured:
        directory = Path(configured).expanduser()
        if not directory.is_absolute():
            directory = HERE / directory
    else:
        directory = Path(config["run"].get("report_dir") or (HERE / "reports")) / "newman"
    directory.mkdir(parents=True, exist_ok=True)
    # This directory holds live tokens for the length of the run.
    os.chmod(directory, stat.S_IRWXU)
    return directory


def _write_private_json(path, payload):
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _protect(path)


def _protect(*paths):
    """Owner-only, for a file that carries a credential."""
    for path in paths:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass


# A variable holding a reference to something that already exists on the
# deployment: an id, a token, a password. Everything else a shipped environment
# carries is scaffolding — hosts, path bases, a urn — and is safe to start from.
_ID_KEY = re.compile(r"(?:^|_)(id|ids|req_?id|request_?id)$|Id$", re.IGNORECASE)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_UUID_ANYWHERE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
_JWT = re.compile(r"^eyJ[A-Za-z0-9_\-]+\.")
# config's own rule catches secret/password/token/access_key; the collections
# also shorten "password" to "pass", which it does not.
_PASSWORD_KEY = re.compile(r"(?:^|_)(pass|pwd|passwd)\d*$", re.IGNORECASE)


def _is_credential(key):
    return config_module._is_secret_key(key) or bool(_PASSWORD_KEY.search(key))


def _points_at_existing_data(key, value):
    return bool(
        _ID_KEY.search(key)
        or _is_credential(key)
        or (isinstance(value, str) and (_UUID.match(value.strip()) or _JWT.match(value.strip())))
    )


def base_variables(ctx):
    """The scaffolding the shipped environment file carries, and nothing else.

    Keeping the shipped values means a variable this harness has never heard of
    — one a new folder introduces before the config catches up — still has the
    value the collection was written against, so a new folder runs before anyone
    has taught the harness about it.

    Ids, tokens and passwords are dropped rather than seeded, and that is a
    safety property, not tidiness. A shipped `user_feedback_id` names a real
    row on the deployment, a shipped `delegate_pass` names a real account, and a
    collection has DELETE requests keyed on exactly those variables. Seeded,
    a negative-path folder would cheerfully delete somebody else's data. Every
    id this run needs is one the run itself produced.
    """
    if not ctx.config["postman"].get("seed_from_environment_file"):
        return {}
    # In as-shipped mode the ids and credentials are the point, so the filter
    # below is off. That is the whole risk of the mode in one line, and it is
    # why run_as_shipped forces the deletes to be held back.
    keep_everything = as_shipped(ctx.config)
    values, dropped = {}, 0
    for entry in ctx.config["postman"]["collections"].values():
        if not isinstance(entry, dict) or not entry.get("enabled") or not entry.get("environment"):
            continue
        path = config_module.resolve_path(entry["environment"])
        shipped = json.loads(Path(path).read_text(encoding="utf-8"))
        for value in shipped.get("values", []):
            key, raw = value.get("key"), value.get("value", "")
            if not key or not value.get("enabled", True):
                continue
            # Registered before it is discarded: the value is in memory now, and
            # a later error echoing this file back must not print it.
            if _is_credential(key) and isinstance(raw, str) and raw.strip():
                client_module.register_secrets([raw.strip()])
            if not keep_everything and _points_at_existing_data(key, raw):
                dropped += 1
                continue
            values[key] = raw
    if dropped:
        _log(f"dropped {dropped} shipped variable(s) pointing at existing platform data")
    return values


def _fallback_window():
    """A window wide enough to contain anything, for a run that has not
    published yet — same spellings the publisher's window uses."""
    return ngsild_publish.observation_window(365 * 24)


def _service_base(section):
    """The base URL of a checked service, or None when it is off.

    Written the way `script/sandbox` and `script/community-layer` write it, so
    a `url` that already carries a scheme is left alone and one that does not
    gets the section's — the same config answers both the scripts and a
    collection that reads the service.
    """
    if not isinstance(section, dict) or not section.get("enabled"):
        return None
    url = str(section.get("url") or "").strip()
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url.rstrip("/")
    return f"{section.get('scheme') or 'https'}://{url}".rstrip("/")


def runtime_variables(ctx, personas=None):
    """Everything this run knows, in the variables the collections read.

    This is the whole "filled in Postman" half of the configuration: hosts and
    bases from the deployment config, one account per persona, a fresh token for
    each, and every id the flow has produced so far. A collection therefore
    needs no editing to run against a different deployment, and no shipped id
    survives into a run.

    `personas` re-points one persona at another account — {"consumer":
    "consumer3"} — for the length of a single phase. Everything belonging to
    that persona moves together: username, password, token and the id
    variables, so a folder that signs in as one account and then addresses
    `/admin/{{consumer_user_id}}/update` operates on that same account rather
    than on somebody else.
    """
    config = ctx.config
    control = config["control_plane"]["base_url"].rstrip("/")
    acl = config["acl"]["base_url"].rstrip("/")
    keycloak = config["keycloak"]["url"].rstrip("/")

    values = {
        # Hosts and path bases.
        "base_url": control,
        "core_host": control,
        "core_url": control,
        "auditing_host": control,
        "acl_url": acl,
        "acl_host": acl,
        "kc_url": keycloak,
        "kc_host": keycloak,
        "realm": config["keycloak"]["realm"],
        "client_id": config["keycloak"]["user_client_id"],
        "client_secret": config["keycloak"].get("user_client_secret") or "",
        "auth_base": "/iudx/v2/auth/",
        "cat_base": "/iudx/v2/cat",
        "acl_base": "/iudx/acl/apd/v2",
        "audit_base": "/iudx/v2/auditing",
        "urn_type": "dx:controlPlane:success",
        # This run's own naming, so a collection that echoes it stays sweepable.
        "run_namespace": ctx.namespace,
        "org_name": ctx.primary.org_name,
        "last_org_name": ctx.primary.org_name,
        "org_manager_email": f"{ctx.namespace}-requester-manager@example.invalid",
        "org_member_email": ctx.users["member"].email,
        "last_join_email": ctx.users["member"].email,
        "credit_expiration_date": (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).strftime("%Y-%m-%dT%H:%M:%S"),
    }

    file_server = config["resource_servers"].get("file") or {}
    if file_server.get("enabled") and file_server.get("url"):
        values["files_server_host"] = flow.server_base(file_server) + "/" + file_server.get(
            "api_version", "v1"
        ).strip("/")

    # The data-plane hosts, so the collection that reads them is pointed at a
    # deployment from config rather than from the host somebody typed into it.
    #
    # `reference_item_id` is the other half: an item that already exists on the
    # deployment, with data behind it and whatever attributes the collection
    # filters on, published as `<key>_reference_id`. A folder pointed at it is
    # asking "does this endpoint work", which is a different question from "does
    # this run's chain work" and is worth asking separately — a run whose own
    # item is empty then reports the endpoint healthy and the chain broken,
    # rather than one indistinguishable failure. Leave it null to skip those
    # phases.
    for key, server in config["resource_servers"].items():
        if not isinstance(server, dict) or not server.get("enabled"):
            continue
        if server.get("url"):
            values[f"{key}_host"] = flow.server_base(server)
        if server.get("reference_item_id"):
            values[f"{key}_reference_id"] = server["reference_item_id"]

    # The two services that are checked rather than onboarded to — they have no
    # per-item route, so they are their own config sections rather than resource
    # servers, and a collection that reads one needs the host from the same
    # place everything else comes from.
    for key in ("sandbox", "community"):
        base = _service_base(config.get(key))
        if base:
            values[f"{key}_url"] = base

    # Keycloak as host and port alone. `kc_url` is the full base the control
    # plane's collection uses; the data-plane collection builds its own scheme
    # and its own `/auth` around the bare host, so both spellings are published
    # and each collection takes the one it was written against.
    values["kc_netloc"] = re.sub(r"^https?://", "", keycloak).split("/")[0]

    # The APD a RESTRICTED item names. It is read off the item document rather
    # than from the deployment's config, so an item created without it can never
    # be accessed — and a collection that hard-codes one points this run's items
    # at somebody else's APD.
    values["apdURL"] = config["acl"]["apd_url"]
    values["apd_url"] = config["acl"]["apd_url"]

    # The range this run's NGSI-LD data occupies, for a `timerel=between` query.
    # Phase 04 sets it from the window it actually published into; before that,
    # and on a run that publishes nothing, a wide fallback keeps the query well
    # formed — a temporal request built from an empty variable is a request that
    # never runs, which would be reported as a collection defect rather than as
    # the missing data it is.
    window = getattr(ctx, "ngsild_window", None) or _fallback_window()
    for name, text in window.items():
        values[f"data_window_{name}"] = text

    # `personas` re-points one persona at another account for this phase only —
    # the mechanism that keeps a folder which rewrites a consumer from rewriting
    # the consumer the rest of the run depends on.
    personas = personas or {}

    for username_var, password_var, key in PERSONA_ACCOUNTS:
        user = ctx.users.get(personas.get(key, key))
        if not user:
            continue
        values[username_var] = user.username
        values[password_var] = user.password

    for token_var, key in PERSONA_TOKENS.items():
        token = ctx.token_for(personas.get(key, key))
        if token:
            values[token_var] = token

    # A cos_admin the config borrows rather than creates is not in ctx.users, so
    # the loops above cannot see it. Its token is minted once at sign-in and
    # reused; its password is read from config and never written anywhere else.
    if not ctx.owns_cos_admin and ctx.cos_admin_token:
        cos = ctx.config["cos_admin"]
        values["cos_admin"] = cos["username"]
        values["cos_admin_pass"] = cos["password"]
        values["cosadmin_user"] = cos["username"]
        values["cosadmin_pass"] = cos["password"]
        for token_var, key in PERSONA_TOKENS.items():
            if key == "cosadmin":
                values[token_var] = ctx.cos_admin_token

    # Ids, once the flow has them. Written under every name the collections use
    # for the same thing, because they disagree: organizationId and org_id are
    # one organisation, seen from two folders.
    lane = ctx.primary
    if lane.org_id:
        values.update(
            {
                "org_id": lane.org_id,
                "organizationId": lane.org_id,
                "org_id_backup": lane.org_id,
                "target_org_id": lane.org_id,
                "consumer_org_id": lane.org_id,
            }
        )
    if lane.org_request_id:
        values["org_create_req_id"] = lane.org_request_id
    if lane.item_id:
        values["item_id"] = lane.item_id
        values["databank_item_id"] = lane.item_id
        values["asset_id"] = lane.item_id
        values["databankId"] = lane.item_id
    if lane.access_request_id:
        values["access_request_id"] = lane.access_request_id
    if lane.policy_ids:
        values["policy_id"] = lane.policy_ids[0]

    # A gateway item lives in its own lane when it needs a provider of its own,
    # and `item_id` names the primary lane's. Published under its own name so a
    # gateway folder can be pointed at the item this run actually gave the
    # gateway — which is the primary item on a run where only one of the two
    # servers is enabled, and a different one where both are.
    for other in ctx.lanes:
        if other.item_id and any(key == "gateway" for key, _ in other.servers):
            values["gateway_item_id"] = other.item_id

    for variable, key in PERSONA_IDS:
        user = ctx.users.get(personas.get(key, key))
        if user and user.user_id:
            values[variable] = user.user_id

    values.update(ctx.config["postman"].get("extra_variables") or {})
    return values


# A `{{name}}` inside a pinned value, resolved against what the run knows.
_TEMPLATE = re.compile(r"\{\{([^{}]+)\}\}")


def _expand(text, values, missing):
    """A pinned value with its `{{name}}` references filled in.

    A name nothing has set becomes the empty string and is collected in
    `missing`, rather than being left as the literal `{{name}}`. Left in place
    it would reach the URL, and a URL with braces in it is a request that never
    goes out — reported as a broken request, which is not what an unset id is.
    """
    def replace(match):
        name = match.group(1).strip()
        found = values.get(name)
        if found in (None, ""):
            missing.append(name)
            return ""
        return str(found)

    return _TEMPLATE.sub(replace, text)


def phase_variables(ctx, phase, values):
    """The variables a collection, or one phase of it, pins for its own length.

    Two collections can disagree about what a name means and both be right:
    `base_url` is the control plane's API base in one and `/dataplane` on the
    same deployment in the other. A block on the collection settles it without
    either collection being edited; a `variables` block on a phase narrows it
    further, for a folder that needs something the rest of the collection does
    not.

    A pinned value may name another variable — `"{{item_id}}"` — and it is
    resolved against everything the run knows at that moment. That is what lets
    a collection's own placeholder be pointed at an id this run produced
    instead of at the dev record it shipped with, from config alone.

    Pinned values are deliberately *not* remembered: they are what this phase
    means by a name, not what the run means by it. See capture_environment.
    """
    if not phase:
        return {}
    postman = ctx.config["postman"]
    pinned = {}
    try:
        key, entry = collection_for(ctx, phase)
    except PhaseError:
        key, entry = None, {}
    if key:
        pinned.update(entry.get("variables") or {})
    pinned.update(phase.get("variables") or {})
    if not pinned:
        return {}

    out, missing = {}, []
    for name, value in pinned.items():
        if isinstance(value, str):
            value = _expand(value, values, missing)
        out[name] = value
    if missing:
        # Worth saying out loud. A pinned id that nothing has set yet usually
        # means the phase is ordered before the phase that produces it, and the
        # symptom otherwise is a folder of 4xx nobody can explain.
        _log(
            f"nothing has set {', '.join(sorted(set(missing)))} yet — "
            f"pinned to an empty value for this phase"
        )
    _log("pinned for this phase: " + ", ".join(sorted(out)))
    return out


def collection_owned_variables(ctx, phase):
    """Names the harness must leave alone, because the collection produces them.

    The mirror image of `variables` pinning. Pinning answers "the collection
    wrote a placeholder here and this run's value belongs in it"; this answers
    "the collection's own scripts produce this, and an answer from the harness
    would be the wrong one".

    It exists because of how Postman resolves a name: the environment outranks
    the collection, so a value seeded here silently shadows what a pre-request
    script just stored — and a folder that then compares the response against
    what its own script stored fails on a mismatch it did not cause. A
    self-driven collection, which signs in and builds its own organisation,
    item and policy, is exactly that case: it needs accounts and hosts from the
    run and nothing else.

    Not applied as-shipped, where the shipped file already wins outright.
    """
    if not phase or as_shipped(ctx.config):
        return []
    try:
        _, entry = collection_for(ctx, phase)
    except PhaseError:
        return []
    return list(entry.get("own_variables") or []) + list(phase.get("own_variables") or [])


def seed_environment(ctx, personas=None, expose_captured=False, phase=None):
    """Write the environment newman is given for the next phase.

    Order matters: what the shipped file carried, then what a previous phase
    exported, then what this run knows. The run wins — a token or an id the
    harness holds is always more current than the one a collection wrote.
    """
    shipped = base_variables(ctx)
    values = dict(shipped)
    values.update(ctx.env_values)
    values.update(runtime_variables(ctx, personas))
    if as_shipped(ctx.config):
        # The precedence inverts. Normally the run wins, because a token the
        # harness holds is more current than one a collection wrote down; here
        # the shipped file is the subject of the test, so it wins instead. The
        # run's values stay underneath rather than being dropped, so a name the
        # shipped file never mentions still resolves to something rather than to
        # the literal `{{name}}` — which would be reported as a request that
        # could not be sent, and that is not what an unset variable is.
        values.update(shipped)
    if expose_captured:
        # Teardown only. A teardown request addresses an artefact by the id the
        # collection stored, and that id lives under the mirror prefix — so here
        # it is put back under the name the request actually reads. Confined to
        # teardown because during the flow it would override, for a whole
        # folder, whichever id that folder's own tests are managing.
        for name, value in captured_ids(ctx).items():
            values.setdefault(name, value)
    # Only what a phase resolved for itself is remembered. A persona override is
    # for the length of one folder, so it must not leak into the next phase's
    # starting point.
    ctx.env_values = values if not personas else dict(values, **runtime_variables(ctx))

    # Resolved before anything is dropped: a pin is an explicit request for a
    # value, so `"item_id": "{{item_id}}"` has to reach the run's item even in a
    # phase whose collection otherwise owns that name. Owning a name means the
    # harness does not *volunteer* it, not that it cannot be asked for.
    pinned = {} if as_shipped(ctx.config) else phase_variables(ctx, phase, values)

    # Dropped onto the copy, after the run's own memory has been written: a name
    # the collection owns is one the harness must not answer for this phase.
    owned = collection_owned_variables(ctx, phase)
    if owned:
        present = sorted(name for name in owned if name in values and name not in pinned)
        if present:
            _log("left to the collection for this phase: " + ", ".join(present))
        values = {k: v for k, v in values.items() if k not in owned}

    # Pinned last, and onto a copy: what this collection means by `base_url` is
    # written into the file newman is handed, and nowhere else.
    ctx.pinned_variables = pinned
    if ctx.pinned_variables:
        values = dict(values, **ctx.pinned_variables)

    document = {
        "id": ctx.namespace,
        "name": f"{ctx.namespace} (generated)",
        "values": [
            {"key": key, "value": "" if value is None else str(value), "enabled": True}
            for key, value in values.items()
        ],
        "_postman_variable_scope": "environment",
    }
    path = work_dir(ctx.config) / "environment.json"
    _write_private_json(path, document)
    ctx.env_path = path
    # What this phase is actually running with, for the collection scope too.
    ctx.seeded_values = values
    return path


def read_environment(path):
    """The variables a newman run exported, as a plain dict."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        value["key"]: value.get("value", "")
        for value in document.get("values", [])
        if value.get("key") and value.get("enabled", True)
    }


def capture_environment(ctx, exported):
    """Take what a phase produced, and remember what teardown will need.

    Ids are the only thing a collection leaves behind that a namespace sweep
    cannot find on its own: an item created by a pre-request script under a name
    that script invented is reachable by id and by nothing else.
    """
    postman = ctx.config["postman"]
    # Everything the phase wrote, except what the phase pinned. newman exports
    # the whole environment it was given, pinned values included, so taking it
    # back wholesale would carry one collection's meaning of `base_url` into the
    # next collection's phases — where it is not merely different, it is wrong.
    # A pinned name is re-derived for every phase that wants it; see
    # phase_variables.
    pinned = ctx.pinned_variables or {}
    ctx.env_values.update({k: v for k, v in exported.items() if k not in pinned})

    ours = {lane.item_id for lane in ctx.lanes} | {lane.org_id for lane in ctx.lanes}
    for variable in postman.get("capture_items") or []:
        value = exported.get(variable)
        # Shape-checked, because a negative case writes whatever it was given —
        # a placeholder uuid, an empty string — into the same variable the
        # positive case writes a real id into. The all-zero uuid is the one
        # sanitise_collection_variables leaves behind for a folder that never
        # ran, and queuing it for teardown sends two doomed requests and reports
        # two problems that were never real.
        if value and str(value).lower() == _ZERO_UUID:
            continue
        if value and _UUID.match(str(value)) and value not in ours and value not in ctx.postman_items:
            ctx.postman_items.append(value)
    for variable in postman.get("capture_orgs") or []:
        value = exported.get(variable)
        if value and str(value).lower() == _ZERO_UUID:
            continue
        if value and _UUID.match(str(value)) and value not in ours and value not in ctx.postman_orgs:
            ctx.postman_orgs.append(value)

    # The flow's own record of the chain, so the script phases and teardown work
    # from what actually happened rather than from what was seeded.
    lane = ctx.primary
    if not lane.org_id and exported.get("org_id"):
        lane.org_id = exported["org_id"]
    if not lane.org_request_id and exported.get("org_create_req_id"):
        lane.org_request_id = exported["org_create_req_id"]
    if not lane.access_request_id and exported.get("access_request_id"):
        lane.access_request_id = exported["access_request_id"]
    policy = exported.get("policy_id")
    if policy and policy not in lane.policy_ids:
        lane.policy_ids.append(policy)


# ---------------------------------------------------------- running a phase


class PhaseResult:
    """What one phase did, for the report and for the exit code."""

    def __init__(self, name, kind, detail=""):
        self.name = name
        self.kind = kind
        self.detail = detail
        self.requests = 0
        self.assertions = 0
        self.failed = 0
        self.skipped = 0
        self.seconds = 0.0
        # Every request this phase made, with its assertions — read out of
        # newman's JSON at parse time rather than at render time, so the report
        # does not depend on the work directory still being there.
        self.calls = []
        self.slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        self.failures = []
        # Requests that never reached the server — a malformed URL, a variable
        # that never resolved, a host that does not answer. Kept apart from
        # `failures` on purpose: a failed assertion is a finding about the
        # deployment, and this is a defect in the collection or the environment
        # that has to be fixed before the request tests anything at all.
        self.broken = []
        self.error = None
        self.required = True
        # Set when a phase was not run because its `when` variable is unset.
        self.skipped_reason = None
        # newman's own HTML report for this phase, when postman.newman_html_report
        # is on. One file per phase, because a phase is one newman process — the
        # combined report this harness writes is what covers the run.
        self.html_report = None
        # Destructive requests held back by postman.skip_delete_requests.
        self.withheld = 0
        # Which server this folder exercises, for the per-server reports. A
        # list, because one folder can serve more than one — the data plane's
        # token folder mints what both the NGSI-LD and the gateway reads use,
        # and leaving it out of either report would make that report look as
        # though its tokens came from nowhere.
        self.servers = []

    @property
    def ok(self):
        return self.error is None and self.failed == 0 and not self.broken

    def summary(self):
        if self.skipped_reason:
            return self.skipped_reason
        if self.error:
            return f"error: {self.error}"
        if self.kind == "postman":
            text = (
                f"{self.requests} request(s), {self.assertions} assertion(s), "
                f"{self.failed} failed"
            )
            if self.broken:
                text += f", {len(self.broken)} never ran"
            return text
        return self.detail or "ok"


def _lane_for_server(ctx, key):
    """The lane whose item declares one kind of resource server, or None.

    The gateway gets a lane of its own when it would otherwise share a broker
    user with an NGSI-LD item, so "the gateway item" is not always the primary
    one.
    """
    for lane in ctx.lanes:
        if any(server_key == key for server_key, _ in lane.servers):
            return lane
    return None


@contextlib.contextmanager
def phase_backing_services(ctx, phase):
    """Whatever has to be running for the length of one Postman phase.

    A gateway item is not served out of storage: the request is put on the
    item's queue and answered by an adaptor consuming it. With nothing consuming
    it the request is never replied to and the read times out — which the
    collection reports as a failed assertion on a 504, indistinguishable from
    the gateway being broken.

    The script phase that reads the data plane already runs the adaptor for its
    own calls. A Postman phase against the same item needs it just as much, so a
    phase says `"adaptor": "gateway"` and gets it for exactly its own length.
    """
    wanted = phase.get("adaptor")
    if not wanted:
        yield
        return
    if wanted != "gateway":
        raise PhaseError(
            f"phase {phase.get('name')!r} asks for adaptor {wanted!r}; "
            f"the only one is 'gateway'"
        )
    lane = _lane_for_server(ctx, "gateway")
    if not lane:
        _log("no gateway lane in this run; no adaptor to start")
        yield
        return
    with flow._gateway_adaptor(ctx, lane):
        yield


# How often a phase that is still running says so.
_HEARTBEAT_SECONDS = 20


# How long a phase is given past its own worst case before it is stopped. The
# per-request timeout is newman's promise, not a guarantee: a socket that opens
# and dribbles bytes, a response newman is still parsing, a test script in the
# collection that loops — none of those are a request timing out, and none of
# them end on their own. The grace is for the honest overshoot; anything past
# it is the phase not coming back.
_WALL_GRACE_SECONDS = 30


def _stop_newman(process):
    """End a newman that is past its budget, politely first.

    newman is a node process started in its own session, so the whole group
    goes — a request in flight otherwise leaves node alive holding the socket
    and the log file open.
    """
    for signal_name, sender in (("SIGTERM", signal.SIGTERM), ("SIGKILL", signal.SIGKILL)):
        try:
            os.killpg(os.getpgid(process.pid), sender)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            if signal_name == "SIGKILL":
                return


def _run_newman(command, log_path, count, timeout_ms, wall_seconds=None):
    """Run newman, saying every so often that it is still going, and stop it
    when it has been going too long.

    newman's own output is captured rather than streamed, because the reports
    are built from its JSON and its cli chatter would bury the phase log. The
    cost is that a slow phase prints nothing at all: a folder holding three
    requests against an endpoint that accepts the call and never answers sits
    silent for three minutes and looks exactly like a hang.

    So the process is polled instead of waited on, and a line goes out every
    `_HEARTBEAT_SECONDS` naming the elapsed time and the per-request timeout —
    the two numbers you need to work out whether it is stuck or just slow. The
    output still lands in one place, a file beside the phase's other artefacts,
    for when the answer is in newman's own words.

    `wall_seconds` is the point at which the answer is settled. Auditing is what
    taught this: four requests at a 60s per-request timeout printed heartbeats
    past 360s, because the endpoint answers slowly enough that no single request
    ever times out. A phase that has outrun its own worst case has told you what
    it is going to tell you, so it is killed and reported as such rather than
    left to hold the run.
    """
    ceiling = count * (timeout_ms / 1000.0) if timeout_ms else None
    killed = False
    with open(log_path, "w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            command,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        started = time.monotonic()
        announced = 0
        while process.poll() is None:
            time.sleep(0.5)
            elapsed = time.monotonic() - started
            if wall_seconds and elapsed >= wall_seconds:
                killed = True
                _log(
                    f"    … {elapsed:.0f}s elapsed, past this phase's {wall_seconds:.0f}s "
                    "cap — stopping newman"
                )
                _stop_newman(process)
                break
            if elapsed - announced < _HEARTBEAT_SECONDS:
                continue
            announced = elapsed
            note = f"    … still running, {elapsed:.0f}s elapsed"
            if timeout_ms:
                note += (
                    f" (per-request timeout {timeout_ms / 1000:.0f}s; "
                    f"{count} request(s), so at worst {ceiling:.0f}s"
                )
                note += f", capped at {wall_seconds:.0f}s)" if wall_seconds else ")"
            _log(note.strip())
    _protect(log_path)

    # The rest of the harness reads .stdout/.stderr off a CompletedProcess.
    try:
        output = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        output = ""
    if killed:
        # Said in the log too, because the log is what a report links to and a
        # killed newman's own last line is whatever it happened to be printing.
        note = (
            f"\n[complete-test] stopped after {wall_seconds:.0f}s — this phase "
            f"outran its cap (postman.max_phase_seconds / the phase's max_seconds)\n"
        )
        output += note
        try:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(note)
        except OSError:
            pass
    return subprocess.CompletedProcess(command, process.returncode, output, ""), killed


def run_postman_phase(ctx, phase, result, teardown=False):
    """Hand one folder to newman, and read back what it did.

    `teardown` marks a phase from `postman.teardown`. Those exist to delete, so
    the keep-artefacts switch must not reach them — it is about the deletes a
    CRUD folder does in passing, not about the ones teardown is for.
    """
    key, entry = collection_for(ctx, phase)
    collection_path = ctx.collection_paths[key]
    collection = load_collection(collection_path)

    folder, whole = folder_for_phase(collection, key, phase)
    postman = ctx.config["postman"]
    destructive = None
    if postman.get("skip_delete_requests") and not teardown:
        destructive = postman.get("destructive_requests") or []
    if as_shipped(ctx.config) and postman.get("as_shipped_skip_deletes", True):
        # Every id in scope belongs to the deployment rather than to this run,
        # and the collections have DELETE requests keyed on exactly those
        # variables — `DELETE /auth/app/{{app_id}}` against a shipped `app_id`
        # removes a real application. Turn it off to reproduce a Postman run
        # exactly, deletes included.
        destructive = list(postman.get("destructive_requests") or []) + _SHIPPED_DELETE_PATTERNS
    selected = _filter_items(
        folder, phase.get("requests") or [], phase.get("skip") or [], destructive
    )
    if not selected:
        result.error = (
            f"no requests left in folder {folder.get('name')!r} after include "
            f"{phase.get('requests')} / skip {phase.get('skip')}"
        )
        return

    subset = {k: copy.deepcopy(v) for k, v in collection.items() if k != "item"}
    # A whole-collection phase keeps the collection's own top level rather than
    # nesting it under one more folder: newman would otherwise report every
    # request one level deeper than the collection reads.
    subset["item"] = selected["item"] if whole else [selected]
    leaves = _leaf_requests(selected)
    if destructive is not None:
        withheld = len(_leaf_requests(folder)) - len(leaves)
        if withheld:
            result.withheld = withheld
            _log(f"holding back {withheld} destructive request(s) — artefacts are being kept")
    # The URL as the collection writes it, before newman substitutes anything.
    # A request that fails to resolve is reported with this rather than with the
    # resolved one, because this is the string that has to be edited.
    raw_urls = {}
    for leaf in leaves:
        url = ((leaf.get("request") or {}).get("url")) or {}
        raw_urls[leaf.get("name", "?")] = url.get("raw") if isinstance(url, dict) else url
    count = len(leaves)
    result.detail = f"{folder.get('name')} ({count} request(s))"
    if result.withheld:
        result.detail += f", {result.withheld} held back"
    _log(f"{folder.get('name')}: {count} request(s) via newman")

    slug = re.sub(r"[^a-z0-9]+", "-", phase["name"].lower()).strip("-")
    work = work_dir(ctx.config)
    run_collection = work / f"run-{slug}.json"
    collection_out = work / f"vars-{slug}.json"

    personas = phase.get("personas") or {}
    if personas:
        described = ", ".join(f"{k} -> {v}" for k, v in personas.items())
        _log(f"personas for this folder: {described}")
    env_path = seed_environment(ctx, personas, expose_captured=teardown, phase=phase)
    subset["variable"] = collection_variables_for_phase(collection, ctx.seeded_values)
    _write_private_json(run_collection, subset)
    env_out = work / f"env-{slug}.json"
    json_out = work / f"newman-{slug}.json"
    result.slug = slug

    command = newman_command(ctx.config) + [
        "run", str(run_collection),
        "-e", str(env_path),
        "--export-environment", str(env_out),
        # The collection is exported as well — a collection's tests write tokens
        # and ids into *collection* variables, and those writes have to survive
        # one newman process ending. What comes back is the subset that just
        # ran, so only its variables are merged into the working copy; writing
        # it back whole would leave the next phase one folder to choose from.
        "--export-collection", str(collection_out),
    ]

    # JSON always. A phase is one newman process, so an HTML reporter here
    # writes one file per folder — and the run wants a single report covering
    # every folder in order. newman's JSON is the data that report is built
    # from; see write_newman_report.
    reporters = ["cli", "json"]
    html_path = newman_html_path(ctx.config, slug)
    if html_path:
        # newman's own report as well, per phase, when the config asks for it:
        # it is the one that shows each request with its headers, its body and
        # the answer beside them, which is what a walkthrough is read from. It
        # complements the combined report rather than replacing it — that is
        # still the only place the run's sequence is visible.
        reporters.append("htmlextra")
    command += ["--reporters", ",".join(reporters), "--reporter-json-export", str(json_out)]
    if html_path:
        command += _newman_html_options(ctx, phase, html_path)
    # A phase may set its own. 60s is right for an endpoint that is merely slow,
    # and wrong for a folder with three requests against one that never answers
    # — that is three minutes of nothing, every run, to learn what the first one
    # already said. Capping it there keeps the finding and drops the wait.
    timeout_ms = phase.get("request_timeout_ms", postman.get("request_timeout_ms"))
    if timeout_ms:
        command += ["--timeout-request", str(int(timeout_ms))]
    # And a budget for the folder as a whole. A phase may name its own
    # `max_seconds`; otherwise it is the worst case the heartbeat already prints
    # plus a grace, so the run can no longer sit on a folder for longer than it
    # said it might. newman is told as well as watched — `--timeout` ends the
    # run itself, which writes the JSON report and leaves the finding readable;
    # the watchdog in _run_newman is the backstop for when it does not.
    wall_seconds = phase.get("max_seconds", postman.get("max_phase_seconds"))
    if wall_seconds is None and timeout_ms:
        wall_seconds = count * (timeout_ms / 1000.0) + _WALL_GRACE_SECONDS
    if wall_seconds:
        wall_seconds = float(wall_seconds)
        command += ["--timeout", str(int(wall_seconds * 1000))]
    # A phase may set its own, and one that chains create -> read through an
    # index needs to: Elasticsearch only sees a document after a refresh, so a
    # read fired immediately after the write it depends on gets a 404 that says
    # nothing about the endpoint. The script phases poll for that; a collection
    # that cannot poll gets the pause instead.
    delay_ms = phase.get("delay_request_ms", postman.get("delay_request_ms"))
    if delay_ms:
        command += ["--delay-request", str(int(delay_ms))]
    if postman.get("insecure"):
        command.append("--insecure")
    if postman.get("bail"):
        command.append("--bail")

    started = time.monotonic()
    with phase_backing_services(ctx, phase):
        process, timed_out = _run_newman(
            command, work / f"newman-{slug}.log", count, timeout_ms, wall_seconds
        )
    result.seconds = time.monotonic() - started
    # newman writes these itself, so it writes them with the default umask. The
    # exported environment holds live bearer tokens and the JSON report holds
    # the bodies they were sent in.
    _protect(env_out, collection_out, json_out)
    if html_path and Path(html_path).is_file():
        # Same reasoning: it carries the bodies, and a body is where a password
        # or a token is. Written owner-only, like every other file that does.
        _protect(html_path)
        result.html_report = str(html_path)

    if timed_out and not json_out.is_file():
        # Killed with nothing written: newman never got to its reporter, so
        # there is no per-request detail to read. Say what the wait was and
        # what to turn, rather than handing back its half-finished cli output.
        result.error = (
            f"stopped after {wall_seconds:.0f}s — this folder outran its cap "
            f"({count} request(s), per-request timeout "
            f"{(timeout_ms or 0) / 1000:.0f}s). The endpoint is answering too "
            "slowly for the per-request timeout to bite; raise the phase's "
            "max_seconds if this is expected, or treat it as a finding."
        )
        return
    if process.returncode != 0 and not json_out.is_file():
        # newman could not even start — a missing collection, a bad flag. Its
        # own message is the only useful thing here.
        result.error = redact_text((process.stderr or process.stdout).strip())[-1500:]
        return

    _read_newman_report(ctx, json_out, result, raw_urls)
    merge_collection_variables(collection_path, collection_out)
    exported = read_environment(env_out)
    if exported:
        capture_environment(ctx, exported)

    if not postman.get("keep_work_dir"):
        for path in (run_collection, collection_out, env_out, json_out,
                     work / f"newman-{slug}.log"):
            path.unlink(missing_ok=True)


def newman_html_dir(config):
    """Where newman's own per-phase HTML reports go, or None when off.

    Beside the run's reports rather than inside the work directory: the work
    directory is this run's scratch — cleared at the start of every run — and
    these are meant to be opened and walked through afterwards.
    """
    if not config["postman"].get("newman_html_report"):
        return None
    configured = config["postman"].get("newman_html_dir")
    if configured:
        directory = Path(configured).expanduser()
        if not directory.is_absolute():
            directory = HERE / directory
    else:
        directory = Path(config["run"].get("report_dir") or (HERE / "reports")) / "newman-html"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def newman_html_path(config, slug):
    """One phase's newman HTML report, named by the phase so a rebuild can find
    it again without the run that wrote it."""
    directory = newman_html_dir(config)
    return directory / f"{slug}.html" if directory else None


def _newman_html_options(ctx, phase, path):
    """The htmlextra flags for one phase.

    Two of them are the reason this is safe to leave on. `skipHeaders` keeps the
    Authorization header out of the file — every request in these collections
    carries a bearer token in it — and `hide_bodies` names the requests whose
    body is a credential either way: a sign-in posts a password and answers with
    a token, and no amount of header filtering helps there. Both are config, so
    a collection that puts a secret somewhere else can say so.
    """
    postman = ctx.config["postman"]
    options = [
        "--reporter-htmlextra-export", str(path),
        "--reporter-htmlextra-title", f"{phase.get('name', 'newman')} — {ctx.namespace}",
        "--reporter-htmlextra-browserTitle", f"newman — {phase.get('name', 'run')}",
        # The collections log the ids they store as they go; in a walkthrough
        # that console output is half the explanation.
        "--reporter-htmlextra-logs",
        "--reporter-htmlextra-showFolderDescription",
    ]
    skip_headers = postman.get("newman_html_skip_headers")
    if skip_headers:
        # A comma-joined string, which is what the reporter's own matcher
        # expects; it substring-matches, so the names must be written in full.
        options += ["--reporter-htmlextra-skipHeaders", ",".join(skip_headers)]
    hidden = list(postman.get("newman_html_hide_bodies") or [])
    hidden += list(phase.get("hide_bodies") or [])
    if hidden:
        joined = ",".join(hidden)
        options += [
            "--reporter-htmlextra-hideRequestBody", joined,
            "--reporter-htmlextra-hideResponseBody", joined,
        ]
    return options


def collection_variables_for_phase(collection, values):
    """The collection's variables, carrying this run's values.

    Postman resolves `{{x}}` from the environment before the collection, so for
    a template the environment alone would be enough. But this collection's
    scripts do not use templates for tokens — they read
    `pm.collectionVariables.get("cosadmin_token")` and build the header string
    themselves, which reads only the collection scope. Both scopes therefore
    have to carry the run's values, and the collection's have to be refreshed
    every phase because the tokens in them expire.

    Only keys the collection already defines are touched: this fills in the
    collection's own variables, it does not invent new ones.
    """
    out = []
    for variable in collection.get("variable") or []:
        key = variable.get("key")
        if key in values and values[key] not in (None, ""):
            variable = dict(variable, value=str(values[key]))
        out.append(variable)
    return out


def merge_collection_variables(collection_path, exported_path):
    """Carry the collection variables a phase wrote into the working copy.

    Only the variables: the exported document is the single folder that just
    ran, and writing it back whole would throw away every folder still to come.

    In practice newman exports the collection as it was handed over, without the
    runtime `pm.collectionVariables.set()` writes — which is why those ids are
    mirrored into the environment instead (see mirror_collection_variables).
    This stays because it is correct either way and costs nothing.
    """
    if not Path(exported_path).is_file():
        return
    try:
        exported = json.loads(Path(exported_path).read_text(encoding="utf-8"))
        working = json.loads(Path(collection_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    variables = {v["key"]: v for v in working.get("variable", []) if v.get("key")}
    for variable in exported.get("variable", []):
        if variable.get("key"):
            variables[variable["key"]] = variable
    working["variable"] = list(variables.values())
    _write_private_json(Path(collection_path), working)


def _read_newman_report(ctx, json_out, result, raw_urls=None):
    """Fold newman's JSON report into this run's record of what happened."""
    try:
        report = json.loads(json_out.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        result.error = f"could not read newman's report: {err}"
        return

    run = report.get("run", {})
    for execution in _distinct_executions(run.get("executions", [])):
        item = execution.get("item", {}) or {}
        request = execution.get("request", {}) or {}
        response = execution.get("response", {}) or {}
        assertions = execution.get("assertions", []) or []

        failed = [a for a in assertions if a.get("error")]
        skipped = [a for a in assertions if a.get("skipped")]
        result.requests += 1
        result.assertions += len(assertions)
        result.failed += len(failed)
        result.skipped += len(skipped)

        # A test script that does not compile — or throws before its first
        # pm.test — produces no assertions at all, and a request with no
        # assertions is not a request that passed: it is one whose checks never
        # ran. newman reports it as a script error rather than a failure, so
        # without this a folder reads green while testing nothing. Counted like
        # a failed assertion, and named so the fix is obvious.
        for scope in ("testScript", "prerequestScript"):
            for entry in execution.get(scope) or []:
                error = (entry or {}).get("error")
                if not error:
                    continue
                result.failed += 1
                result.failures.append(
                    {
                        "request": item.get("name", "?"),
                        "assertion": f"{scope} did not run",
                        "message": redact_text(
                            f"{error.get('name', 'Error')}: {error.get('message', '')} "
                            f"— the script failed before its assertions could run, so "
                            f"nothing in this request was actually checked"
                        )[:400],
                    }
                )

        for assertion in failed:
            error = assertion.get("error") or {}
            result.failures.append(
                {
                    "request": item.get("name", "?"),
                    "assertion": assertion.get("assertion", "?"),
                    "message": redact_text(str(error.get("message", "")))[:400],
                }
            )

        # Every call the run made lands in one table, whoever made it.
        status = response.get("code", 0)
        error = execution.get("requestError")
        if error or not status:
            raw = (raw_urls or {}).get(item.get("name", "?"))
            result.broken.append(
                {
                    "request": item.get("name", "?"),
                    "method": request.get("method", "?"),
                    "url": redact_text(_newman_url(request)),
                    "raw": redact_text(str(raw)) if raw else None,
                    "reason": _diagnose_request(error, request, raw),
                    "blame": _blame_for_request(error, request, raw),
                    "error": redact_text(str(error))[:300] if error else "no response",
                }
            )

        result.calls.append(
            {
                "name": item.get("name", "?"),
                "method": request.get("method", "?"),
                "url": redact_text(_newman_url(request)),
                "status": status,
                "ms": int(response.get("responseTime") or 0),
                "size": int(response.get("responseSize") or 0),
                "error": redact_text(str(error))[:300] if error else None,
                "assertions": [
                    {
                        "name": str(a.get("assertion", "?")),
                        "ok": not a.get("error") and not a.get("skipped"),
                        "skipped": bool(a.get("skipped")),
                        "message": redact_text(
                            str((a.get("error") or {}).get("message", ""))
                        )[:400],
                    }
                    for a in assertions
                ],
                "request_body": _body_text(ctx, request.get("body")),
                "response_body": _body_text(ctx, response),
            }
        )
        ctx.recorder.add(
            f"{item.get('name', '?')}",
            request.get("method", "?"),
            redact_text(_newman_url(request)),
            status if not error else 0,
            not failed and not error,
            int(response.get("responseTime") or 0),
        )

    # newman counts a request that never got a response as an error rather than
    # a failed assertion, and a phase that made no request at all is a failure
    # of the harness, not of the API.
    failures = run.get("failures") or []
    if not result.requests and failures:
        result.error = redact_text(str(failures[0]))[:500]


def _distinct_executions(executions):
    """One record per request the folder actually ran.

    newman reports an execution again for every extra HTTP call a request makes
    — a pre-request script fetching its own token with pm.sendRequest is the
    usual one — and the repeat carries the same item, the same response and the
    same assertions as the original. Counted as-is it doubles the assertion
    totals against `run.stats`, so executions are folded on the cursor
    reference newman gives each item, keeping the last.
    """
    folded = {}
    for index, execution in enumerate(executions):
        cursor = execution.get("cursor") or {}
        key = cursor.get("ref") or (cursor.get("iteration"), cursor.get("position"), index)
        folded[key] = execution
    return list(folded.values())


# What a request that never reached the server says about why. The first two
# are the collection's own fault and are worth naming precisely, because they
# are the ones somebody can go and fix.
_UNRESOLVED = re.compile(r"[{}]")


# Socket errors that mean the request was addressed correctly and sent, and the
# deployment did not answer. Everything else in _diagnose_request is a URL that
# could not be turned into a request in the first place.
_DEPLOYMENT_ERRORS = ("ECONNREFUSED", "ETIMEDOUT", "ESOCKETTIMEDOUT", "ECONNRESET", "EHOSTUNREACH")


def _blame_for_request(error, request, raw):
    """Whose problem a request that produced no response is.

    Both outcomes fail the run by default and both mean the endpoint is
    untested, but they are not the same finding and must not be reported as
    one. A hostname with a brace in it is a typo somebody has to fix in
    Postman. A DELETE that the deployment accepts and then never answers is a
    hanging endpoint, and telling the QA team to fix their collection sends them
    looking for a defect that is not there.
    """
    code = str((error or {}).get("code") or "")
    host = str((error or {}).get("hostname") or "")
    if not host:
        url = request.get("url")
        if isinstance(url, dict):
            host = ".".join(str(h) for h in (url.get("host") or []))
    if _UNRESOLVED.search(host) or not host:
        return "collection"
    if code in _DEPLOYMENT_ERRORS:
        return "deployment"
    # ENOTFOUND and anything else: the address itself did not work, which is
    # the collection's to fix — a variable nobody set, a host that is gone.
    return "collection"


def _diagnose_request(error, request, raw):
    """Why a request never ran, in words that say what to do about it.

    newman reports all of these the same way — a DNS or socket error — but they
    are not the same problem. A hostname with a brace in it is a typo in the
    collection; a hostname that is a variable name is a variable nobody set; a
    real hostname that refuses the connection is the deployment. Reporting them
    as one line of `errno -3008` leaves the reader to work that out every time.
    """
    error = error or {}
    code = str(error.get("code") or "")
    host = str(error.get("hostname") or "")
    if not host:
        url = request.get("url")
        if isinstance(url, dict):
            host = ".".join(str(h) for h in (url.get("host") or []))

    if _UNRESOLVED.search(host):
        hint = ""
        if isinstance(raw, str) and not _brace_error(raw):
            # The half somebody would naturally look at is already correct.
            hint = (
                " The `raw` URL is fine — it is `url.host` in the collection "
                "that still has the typo. Postman stores the URL twice and "
                "newman sends the parsed half; re-saving the URL in the Postman "
                "UI rewrites both."
            )
        return (
            "the URL is malformed in the collection — a brace is unmatched, so "
            "the variable was never substituted and the whole placeholder was "
            f"used as the hostname.{hint or ' Fix the request URL.'}"
        )
    if not host:
        # Everything before the path collapsed, which is what an empty variable
        # in the host position leaves behind.
        return (
            "the URL has no host after substitution — a variable in it resolved "
            "to nothing. Set it in the environment, or have an earlier request "
            "write it."
        )
    if code == "ENOTFOUND":
        hint = ""
        if isinstance(raw, str) and "{{" in raw:
            hint = " It comes from a variable, so check what that variable holds."
        return f"the hostname {host} does not resolve.{hint}"
    if code == "ECONNREFUSED":
        return f"{host or 'the host'} refused the connection — the service may be down."
    if code in ("ETIMEDOUT", "ESOCKETTIMEDOUT"):
        return f"{host or 'the host'} did not answer in time."
    if not error:
        return "the request produced no response at all."
    return f"the request could not be made ({code or 'unknown error'})."


def _body_text(ctx, node):
    """A request or response body, if the config asked for bodies at all.

    Off by default, and that is the safe default rather than a cautious one:
    the token endpoints are called with a password in the body and answer with
    a bearer token in it, so a report carrying bodies carries credentials. What
    does get through is redacted like everything else, and truncated — a report
    is read for which assertion failed, not for a megabyte of GeoJSON.
    """
    if not ctx.config["postman"].get("html_report_bodies") or not isinstance(node, dict):
        return None

    raw = node.get("raw")
    if raw is None:
        # A response body arrives as a byte stream rather than as text.
        stream = node.get("stream")
        if isinstance(stream, dict) and isinstance(stream.get("data"), list):
            try:
                raw = bytes(stream["data"]).decode("utf-8", "replace")
            except (TypeError, ValueError):
                raw = None
    if not isinstance(raw, str) or not raw.strip():
        return None

    limit = ctx.config["run"].get("response_preview_chars") or 800
    text = redact_text(raw)
    return text if len(text) <= limit else text[:limit] + f"… ({len(text)} chars)"


def _newman_url(request):
    url = request.get("url")
    if isinstance(url, str):
        return url
    if isinstance(url, dict):
        raw = url.get("raw")
        if raw:
            return raw
        host = ".".join(url.get("host") or [])
        path = "/".join(url.get("path") or [])
        return f"{host}/{path}"
    return "?"


# ------------------------------------------------------------ script phases


def step_create_users(ctx):
    """Every persona the collections address, created in Keycloak.

    ControlPlane has no create-user API, so this cannot be a Postman phase: the
    accounts have to exist before a single request is sent, and they have to be
    this run's own so teardown can remove them afterwards.
    """
    flow.phase_00_signin(ctx)
    for key in ctx.users:
        ctx.token_minted[key] = time.monotonic()
    return f"{len(ctx.users)} account(s) created and signed in"


def step_await_org_roles(ctx):
    """Confirm the organisation the Postman phase onboarded, or onboard one.

    The approval in folder 02 is what grants org_admin and provider, and every
    phase after it depends on those roles being in the requester's token. Roles
    are read from Keycloak rather than from the collection's assertions: what
    matters here is the state of the platform, not whether a test passed.

    When the collection's own flow did not get the requester into an
    organisation — a rejected request, a folder run out of order, a defect the
    negative cases exposed — the workflow's own onboarding runs instead, and the
    report says the gap was filled. The alternative is every later phase failing
    for a reason that has nothing to do with what it tests.
    """
    lane = ctx.primary
    requester = lane.provider

    try:
        roles = ctx.kc.await_roles(requester.user_id, ORG_APPROVAL_ROLES, timeout=30)
        _log(f"keycloak roles: {', '.join(roles)}")
    except Exception as err:  # noqa: BLE001 - falling back is the point
        _log(f"the collection did not leave {requester.key} in an organisation ({err})")
        _log("onboarding one with the workflow so the chain can continue")
        ctx.gaps_filled.append(
            "organisation onboarding — the Postman folder did not grant "
            "org_admin/provider, so the workflow onboarded an organisation"
        )
        flow._onboard_organisation(ctx, lane)
        ctx.invalidate_tokens()
        # The extra lanes are the workflow's own either way — the collection
        # onboards one organisation, for the persona it names, and never the
        # second. Onboarding them only on the path where the folder succeeded
        # left the gateway lane without an organisation whenever folder 02 did
        # not run: a `--only` slice, a rejected org request, or postman.enabled
        # off. Phase 08 then failed with "no organisation to own an item",
        # which describes the symptom and not the cause.
        return (
            f"organisation {lane.org_id} (onboarded by script)"
            + _onboard_extra_lanes(ctx)
        )

    ctx.invalidate_tokens()
    requester.token = ctx.token_for(requester.key)

    attributes = ctx.kc.attributes(requester.user_id)
    org_attr = attributes.get("organisation_id")
    if org_attr:
        lane.org_id = org_attr[0] if isinstance(org_attr, list) else org_attr

    if not lane.org_id:
        raise AssertionError(
            f"{requester.key} has the approval roles but no organisation_id attribute"
        )

    members = ctx.cp.get(
        f"/iudx/v2/auth/organisations/{lane.org_id}/users",
        "confirm org membership",
        token=requester.token,
    )
    ctx.results["org_members"] = len(members) if isinstance(members, list) else 0
    detail = f"organisation {lane.org_id}, {ctx.results['org_members']} member(s)"
    return detail + _onboard_extra_lanes(ctx)


def _onboard_extra_lanes(ctx):
    """Organisations for any lane the collection did not onboard.

    A run has a second lane when the gateway server is enabled alongside
    NGSI-LD: the catalogue names the broker user it creates after the item's
    *provider*, so one provider owning both kinds of item leaves a single broker
    user for two teardowns to delete. The second provider needs an organisation
    of its own, and the org-create approval is the only thing that grants the
    provider role — there is no join flow that would put it in the first one.

    The collection onboards one organisation, for the persona it names. Every
    further lane is the workflow's own, so it is onboarded the same way
    `main/e2e.py` does.
    """
    extra = ctx.lanes[1:]
    if not extra:
        return ""
    for lane in extra:
        _log(f"onboarding {lane.org_name} for the {lane.key} lane (no collection covers it)")
        flow._onboard_organisation(ctx, lane)
        ctx.invalidate_tokens()
    return "; " + "; ".join(f"{lane.key} lane: organisation {lane.org_id}" for lane in extra)


def step_create_item(ctx):
    """This run's catalogue item, declaring the resource servers under test.

    Folder 08 has already exercised the catalogue's own contract — create,
    read, update, delete, positive and negative. What it cannot do is create the
    item the *rest* of this run needs: the servers an item declares decide which
    data-plane pipeline applies to it, and those come from config.json, not from
    a body written into a collection.

    So the folder proves the API and this proves the platform, and the item id
    every later Postman folder chains on is this one.
    """
    made = []
    for lane in ctx.lanes:
        if not lane.org_id:
            raise AssertionError(f"the {lane.key} lane has no organisation to own an item")
        lane.provider.token = ctx.token_for(lane.provider.key)
        flow._create_item(ctx, lane)
        made.append(
            f"{lane.item_name} — {lane.item_id} "
            f"({', '.join(lane.server_keys()) or 'no server'})"
        )
    return "; ".join(made)


def step_ensure_policy(ctx):
    """A policy on this run's item for this run's consumer.

    The ACL folder exercises the access-request API against whatever item the
    environment names, and its own negative cases can leave the request rejected
    or the policy deactivated. The data-plane phase that follows only means
    something if a policy actually exists — a read refused because nobody
    granted access looks exactly like a read refused by a broken server.

    So the state is checked, and completed if it is not there.
    """
    consumer = ctx.users["consumer"]
    consumer.token = ctx.token_for("consumer")
    return "; ".join(_ensure_lane_policy(ctx, lane, consumer) for lane in ctx.lanes)


def _ensure_lane_policy(ctx, lane, consumer):
    """One lane's policy. Each item needs its own — a policy is on an item."""
    if not lane.item_id:
        raise AssertionError(f"the {lane.key} lane has no catalogue item to grant access to")
    lane.provider.token = ctx.token_for(lane.provider.key)

    existing = _policies_for_item(ctx, consumer, lane.item_id)
    if existing:
        lane.policy_ids = existing
        return f"{len(existing)} policy(ies) already on {lane.item_id}"

    ctx.gaps_filled.append(
        f"access policy on {lane.item_name} — the ACL folder left no active "
        "policy on it, so the workflow requested and granted access"
    )
    request_id = _find_or_create_access_request(ctx, lane, consumer)
    lane.access_request_id = request_id

    expiry = flow._access_expiry(ctx)
    ctx.acl.put(
        "/iudx/acl/apd/v2/access_request",
        f"approve access request (provider){lane.suffix}",
        token=lane.provider.token,
        json_body={"requestId": request_id, "status": "granted", "expiryAt": expiry},
    )

    lane.policy_ids = _policies_for_item(ctx, consumer, lane.item_id)
    if not lane.policy_ids:
        raise AssertionError(f"no policy for item {lane.item_id} after approval")
    return f"{len(lane.policy_ids)} policy(ies) granted on {lane.item_id}"


def _policies_for_item(ctx, consumer, item_id):
    """This consumer's active policies on one item.

    Matched on the consumer as well as the item, which is not belt and braces:
    `/policy/consumer` on this deployment returns policies belonging to other
    consumers too (see the note in the run report), so "a policy exists on this
    item" and "this consumer may read this item" are not the same question.
    Asking the weaker one would let phase 21 mint a token against somebody
    else's grant and report a pass this run had not earned.
    """
    payload = ctx.acl.get(
        "/iudx/acl/apd/v2/policy/consumer",
        "list consumer policies",
        token=consumer.token,
        expect=(200, 404),
    )
    ids = []
    for row in rows_of(payload):
        if str(field(row, "itemId")) != str(item_id):
            continue
        owner = field(row, "consumerId", "consumer_id")
        if owner and consumer.user_id and str(owner) != str(consumer.user_id):
            continue
        status = str(field(row, "status", default="active")).lower()
        if status in ("deleted", "delete", "expired", "inactive"):
            continue
        policy_id = field(row, "policyId", "id", "_id")
        if policy_id:
            ids.append(policy_id)
    return ids


def _find_or_create_access_request(ctx, lane, consumer):
    """This consumer's pending request for the item, creating one if needed.

    A second POST for an item the consumer already asked about is rejected, so
    the existing request is looked for first — which is the normal case here,
    the ACL folder having just made one.
    """
    try:
        return flow._find_access_request_id(ctx, lane)
    except Exception:  # noqa: BLE001 - no request yet, which is what we expect
        pass
    ctx.acl.post(
        "/iudx/acl/apd/v2/access_request",
        "create access request (consumer)",
        token=consumer.token,
        json_body={"itemId": lane.item_id, "requestType": "DOWNLOAD"},
    )
    return flow._find_access_request_id(ctx, lane)


def step_data_onboarding(ctx):
    """Put data behind the item — RabbitMQ, S3, the files API.

    None of this is reachable from a collection: it is a broker publish, a
    pre-signed multipart upload and an onboarding job, driven by the same
    scripts that are used by hand.
    """
    flow.phase_04_data_onboarding(ctx)
    parts = [
        f"{key}: {value}"
        for key, value in ctx.results.items()
        if key in ("ngsild_publish", "ogc_vector", "ogc_raster", "file_upload")
    ]
    return "; ".join(parts) or "nothing to onboard"


def step_resource_servers(ctx):
    """The end-to-end proof: a granted token reads the data, an ungranted one
    does not.

    The data-plane collection covers the NGSI-LD and gateway *API surface* —
    every status code each endpoint is specified to return — but it is not this,
    and adding it did not retire this step. Three things it cannot do:

    - read *this run's* item. Its 200 cases name resource ids typed in on
      somebody's deployment; the ids in variables are pointed at this run's item
      from config, the ids written into URLs cannot be.
    - assert the refusal. Nothing in it signs in as an account that was never
      granted access, so a blanket-allow bug would pass every folder in it.
    - reach OGC or the file server at all. No collection exists for those yet.

    Retiring this step needs a collection that chains on the item the run
    created. Until one arrives both run, and the overlap is the point: the
    collection says the endpoint is correct, this says the chain is.
    """
    ctx.gaps_filled.append(
        "resource servers: the data-plane collection tests the NGSI-LD and "
        "gateway endpoints against fixed resource ids, so the scripts still own "
        "reading this run's own item, proving the no-policy token is refused, "
        "and OGC and the file server, which have no collection yet."
    )
    flow.phase_05_resource_servers(ctx)
    outcome = ctx.results.get("resource_servers")
    if isinstance(outcome, dict):
        return "; ".join(f"{k} {v}" for k, v in outcome.items())
    return str(outcome)


def step_auditing(ctx):
    """Assert this run's own activity reached the audit trail.

    Folder 17 calls the auditing APIs; this asserts that the rows belonging to
    *this run's* provider are in them, which is what proves the RabbitMQ to
    Elasticsearch path delivered rather than that the endpoint answered.
    """
    flow.phase_06_auditing(ctx)
    return (
        f"consumer {ctx.results.get('audit_consumer_rows', 0)} row(s), "
        f"admin {ctx.results.get('audit_admin_rows', 0)} row(s)"
    )


def step_data_plane_cleanup(ctx):
    """The broker, S3 and OGC objects — before anything deletes the item.

    These are all named after the catalogue item, and the file server, the STAC
    store and the OGC tables all refuse to talk about a databank that no longer
    exists. The collection's own `DELETE /cat/item` runs after this, so this has
    to go first or every one of them fails.
    """
    problems = cleanup.teardown(ctx, stage="data_plane")
    for problem in problems:
        _log(problem)
    ctx.teardown_problems.extend(problems)
    return f"{len(problems)} problem(s)" if problems else "clean"


def step_cleanup(ctx):
    """Everything a collection cannot delete, deleted by the scripts.

    The postman sweep goes between the two teardown stages, not after them. It
    finds items by signing in as each namespaced account and asking what that
    account owns, so it has to run while the accounts are still there —
    `accounts` deletes them, and after that there is nobody left to ask.
    """
    problems = cleanup.teardown(ctx, stage="platform")
    _sweep_postman_artefacts(ctx, problems)
    problems.extend(cleanup.teardown(ctx, stage="accounts"))
    for problem in problems:
        _log(problem)
    ctx.teardown_problems.extend(problems)
    return f"{len(problems)} problem(s)" if problems else "clean"


def _sweep_postman_artefacts(ctx, problems):
    """Remove what the collections created beyond this run's own chain.

    Two routes, because the collections create artefacts two ways. A body with a
    literal name was namespaced before the run, so the prefix sweep finds it.
    A body a pre-request script built at run time was not, and is only reachable
    by the id its response announced — which is what was captured after every
    phase.

    Runs between teardown's `platform` and `accounts` stages: the run's own item
    is already gone, so the prefix sweep below sees only what the collections
    left, and every account it needs a token for still exists.
    """
    for item_id in ctx.postman_items:
        token = ctx.token_for(ctx.primary.provider.key)
        cleanup._delete_item_with_policies(ctx, token, item_id, f"postman item {item_id}", problems)

    # Items created under a namespaced name, by whichever account owns them.
    cleanup._sweep_catalogue_items(ctx, problems)

    ours = {lane.org_id for lane in ctx.lanes}
    extra = [org for org in ctx.postman_orgs if org not in ours]
    if extra:
        # Organisations have no delete API — AdminHandler refuses, and the row
        # is removed by the database sweep. Naming them here is what tells the
        # reader why the sweep found more than one.
        _log(f"organisations created by the collections: {', '.join(extra)}")


STEPS = {
    "create_users": step_create_users,
    "await_org_roles": step_await_org_roles,
    "create_item": step_create_item,
    "ensure_policy": step_ensure_policy,
    "data_onboarding": step_data_onboarding,
    "resource_servers": step_resource_servers,
    "auditing": step_auditing,
    "data_plane_cleanup": step_data_plane_cleanup,
    "cleanup": step_cleanup,
}


# ------------------------------------------------------------------ the run


def selected_phases(config, only, key="phases"):
    """The phases to run, in order, after --only and `enabled` are applied.

    --only takes a comma-separated list because the phases are a chain: running
    one folder usually means running the script phase that sets it up too, and
    `--only "00 users,03"` is how you say that without editing the config.
    """
    wanted = [part.strip() for part in (only or "").split(",") if part.strip()]
    phases = []
    for phase in config["postman"][key]:
        if phase.get("enabled") is False:
            continue
        if wanted and not any(
            part in phase.get("name", "") or part == phase.get("folder") for part in wanted
        ):
            continue
        phases.append(phase)
    return phases


def run_phase(ctx, phase, teardown=False):
    """One phase, whichever kind it is. Never raises: the report needs the rest."""
    name = phase.get("name") or phase.get("folder") or phase.get("step")
    result = PhaseResult(name, phase["type"])
    result.required = phase.get("required", True)
    result.servers = phase_servers(phase)

    if phase["type"] == "postman" and not postman_enabled(ctx.config):
        # Skipped, not failed. The folder was never asked to run, so it has
        # nothing to say about the deployment — and a report that counts it as a
        # failure would say the opposite.
        result.skipped_reason = "postman.enabled is off — no folder is handed to newman"
        ctx.phases.append(result)
        _banner(f"[{name}]")
        _log(f"skipped — {result.skipped_reason}")
        return result

    if as_shipped(ctx.config) and phase["type"] != "postman":
        # A script phase creates the accounts, the item and the data that the
        # collections are normally pointed at. In this mode nothing is pointed
        # at them, so running one would build a chain, leave it unused and then
        # tear it down — cost and side effects for no answer.
        result.skipped_reason = "as-shipped run: only the collections run"
        ctx.phases.append(result)
        _banner(f"[{name}]")
        _log(f"skipped — {result.skipped_reason}")
        return result

    unmet = _unmet_condition(ctx, phase)
    if unmet:
        result.skipped_reason = unmet
        ctx.phases.append(result)
        _banner(f"[{name}]")
        _log(f"skipped — {unmet}")
        return result

    ctx.recorder.enter(name)
    _banner(f"[{name}]")

    started = time.monotonic()
    try:
        if phase["type"] == "postman":
            run_postman_phase(ctx, phase, result, teardown)
        else:
            step = STEPS.get(phase["step"])
            if not step:
                raise PhaseError(
                    f"unknown script step {phase['step']!r}. Known steps: "
                    + ", ".join(sorted(STEPS))
                )
            result.detail = step(ctx) or ""
            result.seconds = time.monotonic() - started
    except SystemExit:
        raise
    except Exception as err:  # noqa: BLE001 - recorded, and the run continues
        result.error = str(err)
    if not result.seconds:
        result.seconds = time.monotonic() - started

    ctx.phases.append(result)
    if result.error:
        marker = "ERROR" if result.required else "error (not required)"
    elif result.failed:
        marker = "failed" if result.required else "failed (not required)"
    else:
        marker = "ok"
    _log(f"{marker} ({result.seconds:.1f}s) — {result.summary()}")
    for broken in result.broken:
        _log(f"  !! NEVER RAN — {broken['request']}")
        _log(f"     {broken['reason']}")
        if broken.get("raw"):
            _log(f"     URL in the collection: {broken['raw']}")
    for failure in result.failures[:8]:
        _log(f"  ✗ {failure['request']}: {failure['assertion']}")
    if len(result.failures) > 8:
        _log(f"  … and {len(result.failures) - 8} more, see the report")
    return result


def broken_requests(ctx, blame=None, phases=None):
    """Every request in the run that produced no response, with its phase.

    `blame` narrows it to one kind: "collection" for a request that could not be
    addressed, "deployment" for one that was sent and never answered. `phases`
    narrows it to a subset of the run — one server's folders, for that server's
    own report.
    """
    return [
        (phase, broken)
        for phase in (ctx.phases if phases is None else phases)
        for broken in phase.broken
        if blame is None or broken.get("blame", "collection") == blame
    ]


def failing_phases(ctx):
    """Phases that make the run fail.

    A phase fails the run when it could not run at all, and — only when
    `fail_on_assertion_failure` is on — when one of its assertions failed. A
    phase marked `required: false` never does either: its collection is known to
    assert against defects the deployment has not fixed, and those findings
    belong in the report rather than in the exit code.
    """
    postman = ctx.config["postman"]
    strict = postman["fail_on_assertion_failure"]
    strict_broken = postman["fail_on_broken_request"]
    strict_unanswered = postman.get("fail_on_unanswered_request", True)

    def unran(phase):
        """The requests in this phase that produced no response and count."""
        return [
            entry
            for entry in phase.broken
            if (strict_broken and entry.get("blame", "collection") == "collection")
            or (strict_unanswered and entry.get("blame") == "deployment")
        ]

    return [
        phase
        for phase in ctx.phases
        if (phase.required and (phase.error is not None or (strict and phase.failed)))
        # A request that never ran is not an ordinary finding, so
        # `required: false` — which exists to tolerate known platform defects —
        # does not excuse it. Whichever side it belongs to, the endpoint it
        # covers is untested rather than passing.
        or unran(phase)
    ]


def counted_failures(ctx):
    """Failed assertions that count against the run.

    A phase marked `required: false` is one whose collection is known to assert
    against defects the deployment has not fixed yet. Its failures are reported
    in full — that is the point of running it — but they do not turn a green run
    red, or nobody reads the result at all.
    """
    return sum(p.failed for p in ctx.phases if p.required)


def _unmet_condition(ctx, phase):
    """Why a phase should not run, or None.

    `when` names the Postman variables a phase acts on — a teardown that
    deletes a subscription needs a subscription id. Running it anyway sends a
    request built from an empty variable, which the platform rejects and the
    collection reports as a failed assertion: noise that reads exactly like a
    real defect. A phase whose subject does not exist is skipped and says so.
    """
    # A phase naming a collection nobody has enabled is skipped, not failed.
    # The phase list is written ahead of the collections: the servers arrive one
    # at a time, and the run for a deployment that has only some of them should
    # say which folders it did not have rather than reporting an error per
    # folder. Turning the collection on in config is all it takes to run them.
    key = phase.get("collection")
    if key and phase.get("type") == "postman":
        entry = ctx.config["postman"]["collections"].get(key)
        if not isinstance(entry, dict) or not entry.get("enabled"):
            return f"postman.collections.{key} is not enabled"

    wanted = phase.get("when")
    if not wanted:
        return None
    known = _known_variables(ctx)
    names = [wanted] if isinstance(wanted, str) else list(wanted)
    for name in names:
        # `config:dotted.path` asks about the deployment rather than about the
        # run. A phase reading a resource that already exists on the platform —
        # a reference item, say — is conditional on somebody having named one,
        # and that is answerable before the run has produced anything, which a
        # variable-name guard is not.
        if str(name).startswith("config:"):
            value = ctx.config
            for part in name[len("config:"):].split("."):
                value = (value or {}).get(part) if isinstance(value, dict) else None
            if str(value or "").strip():
                return None
            continue
        value = str(known.get(name) or "").strip()
        if value and _UUID.match(value):
            return None
    described = " / ".join(
        n[len("config:"):] if str(n).startswith("config:") else n for n in names
    )
    return f"nothing to act on — {described} was never set by this run"


# The prefix the injected script mirrors collection variables under.
_MIRROR_PREFIX = "__cv_"


def captured_ids(ctx):
    """Ids the collections stored as collection variables, by their real name.

    A collection's tests write ids to either scope — `pm.environment.set` in
    some folders, `pm.collectionVariables.set` in others — and which one a given
    test used is not something a teardown entry should have to know. newman does
    not carry collection-variable writes out of the process that made them, so
    the injected script mirrors them into the environment under a prefix; this
    reads them back.
    """
    return {
        key[len(_MIRROR_PREFIX):]: value
        for key, value in ctx.env_values.items()
        if key.startswith(_MIRROR_PREFIX) and value
    }


def _known_variables(ctx):
    """Every variable a `when` guard may be satisfied by.

    The environment first, then the ids mirrored out of collection variables —
    an id counts as known whichever scope the collection chose to keep it in.
    """
    values = dict(captured_ids(ctx))
    values.update(ctx.env_values)
    return values


def fatal(ctx, result):
    """Whether a failed phase should stop the run.

    A failed assertion is a finding, not a reason to stop: the collections carry
    negative cases against known defects, and one of them failing says nothing
    about whether the next folder can run. A phase that could not run at all is
    different — everything after it is chained to what it was supposed to
    produce.
    """
    return result.error is not None and result.required


# ---------------------------------------------------------------- reporting


def _cell(value):
    return html.escape(str(value if value is not None else "—"))


def _artefacts(ctx):
    rows = [("namespace", ctx.namespace)]
    for key, user in ctx.users.items():
        # Why there are three consumers rather than one is not guessable from a
        # list of near-identical addresses, and it is the thing a reader most
        # needs when a consumer-shaped assertion fails.
        job = CONSUMER_ROLES.get(key)
        note = f"<br><small>{html.escape(job)}</small>" if job else ""
        rows.append(
            (f"user: {key}", f"{user.username} &nbsp; <code>{user.user_id}</code>{note}")
        )
    if not ctx.owns_cos_admin:
        rows.append(("cos admin", f"{ctx.config['cos_admin']['username']} (existing, never swept)"))
    # One block per lane. A run has a second one when the gateway server is
    # enabled alongside NGSI-LD and the gateway item needs a provider of its own.
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
    if ctx.postman_items:
        rows.append(
            ("items from collections", ", ".join(f"<code>{i}</code>" for i in ctx.postman_items))
        )
    if ctx.postman_orgs:
        rows.append(
            ("orgs from collections", ", ".join(f"<code>{o}</code>" for o in ctx.postman_orgs))
        )
    body = "".join(
        f"<tr><th>{html.escape(name)}</th><td>{value}</td></tr>"
        for name, value in rows
        if value not in (None, "None", "", "<code>None</code>")
    )
    return f"<table class='kv'>{body}</table>"


def _phase_rows(ctx, newman_link):
    rows = []
    for result in ctx.phases:
        link = "—"
        if newman_link and result.kind == "postman" and result.calls:
            link = (
                f"<a href='{html.escape(newman_link)}#{html.escape(result.slug)}'>"
                f"{len(result.calls)} request(s)</a>"
            )
        if result.skipped_reason:
            state = "skipped"
        else:
            state = "good" if result.ok else ("bad" if result.required else "warn-row")
        # A skipped phase has no detail of its own, and a grey row with an empty
        # cell reads as "something went wrong here" — which is the opposite of
        # what a deliberate skip means. Say which switch did it.
        detail = result.detail or (
            f"skipped — {result.skipped_reason}" if result.skipped_reason else ""
        )
        rows.append(
            f"<tr class='{state}'>"
            f"<td>{_cell(result.name)}</td>"
            f"<td>{_cell(result.kind)}</td>"
            f"<td>{_cell(detail)}</td>"
            f"<td class='s'>{result.requests or '—'}</td>"
            f"<td class='s'>{result.assertions or '—'}</td>"
            f"<td class='s'>{result.failed or '—'}</td>"
            f"<td class='s'>{result.seconds:.1f}</td>"
            f"<td>{link}</td></tr>"
        )
        if result.error:
            rows.append(
                f"<tr class='bad'><td colspan='8'><pre>{_cell(result.error)}</pre></td></tr>"
            )
    return "".join(rows)


def _broken_section(ctx, link_prefix=None, phases=None):
    """The requests that never ran, spelled out so they can be fixed.

    First in both reports, above the assertion failures, because it is a
    different question: an assertion failure asks whether the platform behaves
    as specified, and this asks whether the request was ever sent. A collection
    with one of these has a test that has never run and cannot fail — the worst
    kind of green.
    """
    def rows_for(blame):
        rows = []
        for phase, broken in broken_requests(ctx, blame, phases):
            # None means render the phase name plainly — there is nothing to
            # link to. "" links within this page; a filename links across to the
            # other report.
            if link_prefix is None:
                name = _cell(phase.name)
            else:
                anchor = f"{html.escape(link_prefix)}#{html.escape(phase.slug)}"
                name = f"<a href='{anchor}'>{_cell(phase.name)}</a>"
            rows.append(
                "<tr class='bad'>"
                f"<td>{name}</td>"
                f"<td>{_cell(broken['request'])}</td>"
                f"<td class='m'>{_cell(broken['method'])}</td>"
                f"<td>{_cell(broken['reason'])}</td>"
                f"<td class='u'>{_cell(broken.get('raw') or broken['url'])}</td>"
                f"<td class='u'>{_cell(broken['error'])}</td></tr>"
            )
        return rows

    header = (
        "<table><tr><th>Phase</th><th>Request</th><th>Method</th><th>Why</th>"
        "<th>URL in the collection</th><th>What newman reported</th></tr>"
    )
    collection_rows = rows_for("collection")
    deployment_rows = rows_for("deployment")
    if not collection_rows and not deployment_rows:
        return "<p class='ok'>None — every request reached a server.</p>"

    out = []
    # Split, because these are two different findings addressed to two different
    # people. Both mean the endpoint is untested rather than passing, which is
    # why both are here and both fail the run — but telling the QA team to fix
    # their collection because an endpoint hung sends them looking for a defect
    # that is not there.
    if collection_rows:
        out.append(
            "<h3>Could not be sent — fix the collection</h3>"
            "<p class='fail'>The request could not be turned into a call at all: "
            "a brace in the URL, a variable nobody set, a hostname that does not "
            "resolve. Until it is fixed those endpoints are untested rather than "
            "passing.</p>" + header + "".join(collection_rows) + "</table>"
        )
    if deployment_rows:
        out.append(
            "<h3>Sent, never answered — a finding about the deployment</h3>"
            "<p class='fail'>The request was addressed correctly and went out, "
            "and the server refused it or never replied. Nothing in the "
            "collection will fix this: it is a hanging or unreachable endpoint, "
            "and its tests did not run.</p>" + header + "".join(deployment_rows) + "</table>"
        )
    return "".join(out)


def _notes_section(ctx):
    """Ids a collection carries that no run can satisfy.

    Not a failure — a collection may mean it — but each one is a request whose
    subject has not existed since the day it was written, so whatever it
    asserts, it is not asserting it about this deployment.
    """
    if not ctx.collection_notes:
        return "<p class='ok'>None found by reading the collections.</p>"
    rows = "".join(
        f"<tr class=\"{'bad' if note['kind'] in ('malformed URL', 'rewrite matched nothing') else ''}\">"
        f"<td>{_cell(note.get('collection'))}</td>"
        f"<td>{_cell(note['folder'])}</td>"
        f"<td>{_cell(note['request'])}</td>"
        f"<td>{_cell(note['kind'])}</td>"
        f"<td class='u'>{_cell(note['detail'])}</td></tr>"
        for note in ctx.collection_notes
    )
    return (
        "<p class='fail'><b>malformed URL</b> — a brace is unmatched. Postman "
        "keeps a URL twice, as <code>raw</code> and parsed into "
        "<code>host</code>/<code>path</code>, and newman sends the parsed one. "
        "Fixing only <code>raw</code> leaves the request still going nowhere, so "
        "the field named here is the one to edit. Re-saving the URL in the "
        "Postman UI rewrites both.</p>"
        "<p class='warn'><b>hard-coded id</b> — the request names a record by id "
        "instead of by variable, in its URL, its body or the script that "
        "rewrites them. That id belonged to whoever wrote the request, on their "
        "deployment, on that day, so the request runs, gets an answer, and tests "
        "nothing about this run. Replace it with a variable the run already sets "
        "(<code>{{item_id}}</code>, <code>{{sampleResourceId}}</code>, "
        "<code>{{consumer_user_id}}</code>, …) or confirm it is deliberate. "
        "Requests whose name says they expect a 4xx are not listed: an id that "
        "resolves to nothing is the point of those.</p>"
        "<p class='ok'><b>retargeted</b> — the collection named a resource id, or "
        "a query window, that belonged to the deployment it was written on. It "
        "has been pointed at this run's own item for the run, so the request "
        "asks about data this run put there. The collection still carries the "
        "original: replace it with the variable named here.</p>"
        "<p class='fail'><b>rewrite matched nothing</b> — a rewrite in "
        "<code>postman.collections.&lt;key&gt;.rewrite</code> found nothing to "
        "replace, so whatever it was protecting against is unprotected. Usually "
        "the collection has been re-exported with a different id, and those "
        "requests are back to reading somebody else's data.</p>"
        "<p class='warn'><b>credential in request</b> — the credential was in the "
        "request's own auth block rather than in a variable, so emptying the "
        "variables did not reach it. It has been re-pointed at this run's own "
        "application for the run; the collection still carries the original and "
        "should be edited to read a variable.</p>"
        "<table><tr><th>Collection</th><th>Folder</th><th>Request</th>"
        "<th>Kind</th><th>Detail</th></tr>"
        + rows
        + "</table>"
    )


def _failure_rows(ctx):
    rows = []
    for result in ctx.phases:
        for failure in result.failures:
            rows.append(
                "<tr>"
                f"<td>{_cell(result.name)}</td>"
                f"<td>{_cell(failure['request'])}</td>"
                f"<td>{_cell(failure['assertion'])}</td>"
                f"<td class='u'>{_cell(failure['message'])}</td></tr>"
            )
    if not rows:
        return "<p class='ok'>Every assertion passed.</p>"
    return (
        "<table><tr><th>Phase</th><th>Request</th><th>Assertion</th><th>Message</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _call_rows(ctx):
    rows = []
    for index, entry in enumerate(ctx.recorder.entries):
        rows.append(
            "<tr class='{cls}'><td>{n}</td><td>{phase}</td><td>{label}</td>"
            "<td class='m'>{method}</td><td class='u'>{url}</td>"
            "<td class='s'>{status}</td><td class='s'>{ms}</td></tr>".format(
                cls="good" if entry["ok"] else "bad",
                n=index + 1,
                phase=_cell(entry["phase"]),
                label=_cell(entry["label"]),
                method=_cell(entry["method"]),
                url=_cell(entry["url"]),
                status=entry["status"],
                ms=entry["ms"],
            )
        )
    return "".join(rows)


# The two reports this harness writes are both single files with fixed names,
# overwritten every run. A directory that accumulates one file per run per
# folder is a directory nobody opens: the question being asked is almost always
# "what does it look like now", and the answer should be at a path that can be
# bookmarked, linked from CI, or left open in a browser tab and reloaded.

SHARED_STYLE = """
 body{font:14px/1.55 system-ui,sans-serif;margin:2rem;color:#111;max-width:1500px}
 table{border-collapse:collapse;width:100%;margin-bottom:1.5rem}
 td,th{border-bottom:1px solid #e3e3e3;padding:.45rem .5rem;vertical-align:top;text-align:left}
 th{background:#f6f6f6;font-weight:600}
 table.kv th{width:200px;background:#fafafa}
 tr.bad td{background:#fff2f2} tr.warn-row td{background:#fffaf0}
 tr.skipped td{color:#777}
 td.m{font-weight:600;white-space:nowrap}
 td.s{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
 td.u{font:12px/1.4 ui-monospace,monospace;word-break:break-all;max-width:560px}
 pre{margin:.35rem 0 0;white-space:pre-wrap;word-break:break-word;font-size:12px;
     background:#fafafa;padding:.5rem;border-left:3px solid #ddd;max-height:340px;overflow:auto}
 code{background:#f2f2f2;padding:.05rem .3rem;border-radius:3px;font-size:12px}
 summary{cursor:pointer;font-size:12px;color:#0645ad}
 .status{font-size:1.2rem;font-weight:700}
 .ok{color:#0a7a2f} .warn{color:#8a5a00} .bad-list li,.status.bad,.fail{color:#a11}
 .tiles{display:flex;gap:1rem;flex-wrap:wrap;margin:1rem 0}
 .tile{border:1px solid #e3e3e3;border-radius:6px;padding:.6rem 1rem;min-width:120px}
 .tile b{display:block;font-size:1.4rem}
 .pass{color:#0a7a2f}
 h2{margin-top:2rem;border-top:1px solid #e3e3e3;padding-top:1rem}
 .assertions{margin:.2rem 0 0;padding-left:1.1rem;font-size:12px}
 .assertions li{margin:.1rem 0}
 .nav a{margin-right:1rem;font-size:13px}
 h2 a.full{font-size:12px;font-weight:400;margin-left:.6rem;white-space:nowrap}
 .note{background:#f6f8fa;border-left:3px solid #cfd8e3;padding:.5rem .75rem;margin:.6rem 0}
"""


def newman_report_path(config, run_report):
    """Where the single newman report goes: beside the run report."""
    name = config["postman"].get("report_file") or "newman-report.html"
    return Path(run_report).parent / name


def phase_servers(phase):
    """The servers one phase exercises, as a list.

    `server` is a string for the usual case and a list where one folder serves
    more than one. Free text on purpose: the names are not checked against
    `resource_servers`, so two folders that belong in one report are merged by
    giving them the same name and split by giving them different ones. A phase
    that names none appears in the combined report and nowhere else.
    """
    named = phase.get("server")
    if not named:
        return []
    if isinstance(named, str):
        return [named]
    return [str(one) for one in named if one]


def report_servers(ctx):
    """Every server named by a phase that actually ran, in first-run order."""
    seen = []
    for phase in ctx.phases:
        if phase.kind != "postman" or not (phase.calls or phase.error):
            continue
        for server in phase.servers:
            if server not in seen:
                seen.append(server)
    return seen


def server_report_path(config, run_report, server):
    """Where one server's newman report goes: beside the combined one."""
    name = config["postman"].get("report_file") or "newman-report.html"
    stem = Path(name).stem
    slug = re.sub(r"[^a-z0-9]+", "-", str(server).lower()).strip("-") or "server"
    return Path(run_report).parent / f"{stem}-{slug}.html"


def write_newman_reports(ctx, run_report):
    """The combined report, and one per server.

    Two questions, two files. "Did this run pass" is answered by reading every
    folder in the order it ran, script phases and all — that is the combined
    report, and it is the only place the sequence is visible. "Did the gateway
    pass" is answered by reading the gateway's folders and nothing else, with
    totals that count only those; in the combined report that answer exists but
    has to be assembled by scrolling, and a server whose folders are spread
    across a fifty-phase run is easy to misread.

    Which folders belong to which server is config, not inference: each phase
    carries a `server` name — see phase_servers. Returns (combined, [(server,
    path), ...]); either half can be empty.
    """
    combined = write_newman_report(newman_report_path(ctx.config, run_report), ctx, run_report)
    if not combined or not ctx.config["postman"].get("server_reports", True):
        return combined, []

    per_server = []
    for server in report_servers(ctx):
        path = server_report_path(ctx.config, run_report, server)
        written = write_newman_report(path, ctx, run_report, server=server,
                                      combined=Path(combined).name)
        if written:
            per_server.append((server, written))

    # Written last, because it links to files that did not exist until now.
    if per_server:
        write_newman_report(newman_report_path(ctx.config, run_report), ctx, run_report,
                            per_server=per_server)
    return combined, per_server


def write_newman_report(path, ctx, run_report, server=None, combined=None, per_server=()):
    """Every folder newman ran, in the order it ran them.

    newman writes a report per process, and a phase is a process — so a run that
    interleaves twenty folders with the script steps between them produces
    twenty reports, none of which describes the run. This is the one that does:
    the same request-by-request, assertion-by-assertion detail, ordered as the
    run went, with the script phases marked in place so the sequence still reads
    as a whole.

    With `server` it narrows to the folders that name that server, and every
    total on the page counts only those — a per-server report whose tiles held
    the whole run's numbers would be worse than no per-server report at all.
    """
    phases = [p for p in ctx.phases if p.kind == "postman" and (p.calls or p.error)]
    if server is not None:
        phases = [p for p in phases if server in p.servers]
    if not phases:
        return None

    requests = sum(len(p.calls) for p in phases)
    assertions = sum(len(c["assertions"]) for p in phases for c in p.calls)
    failed = sum(1 for p in phases for c in p.calls for a in c["assertions"] if not a["ok"])
    skipped = sum(1 for p in phases for c in p.calls for a in c["assertions"] if a["skipped"])
    seconds = sum(p.seconds for p in phases)

    broken = broken_requests(ctx, phases=phases)
    nav = " ".join(
        f"<a href='#{p.slug}'>{html.escape(p.name)}</a>" for p in phases
    )
    sections = "".join(_newman_section(p, Path(path).parent) for p in phases)

    if server is None:
        heading = "newman — every folder, in run order"
        scope = ""
    else:
        heading = f"newman — {html.escape(str(server))}"
        scope = (
            f"<p class='note'>Only the folders that exercise "
            f"<b>{html.escape(str(server))}</b>. Every count below is that "
            f"server's alone.</p>"
        )

    links = [f'<a href="{html.escape(Path(run_report).name)}">the run report</a>']
    if combined:
        links.append(f'<a href="{html.escape(combined)}">the combined newman report</a>')
    # The per-server files get a line of their own rather than the header, which
    # is where "what am I looking at" belongs and not "what else is there".
    by_server = ""
    if per_server:
        by_server = "<p class='note'>One report per server: " + " &middot; ".join(
            f'<a href="{html.escape(Path(other).name)}">{html.escape(str(name))}</a>'
            for name, other in per_server
        ) + "</p>"

    document = f"""<!doctype html><meta charset="utf-8">
<title>newman — {html.escape(str(server) if server else ctx.namespace)}</title>
<style>{SHARED_STYLE}</style>
<h1>{heading}</h1>
<p>run <code>{_cell(ctx.namespace)}</code> &middot;
   target <code>{_cell(ctx.config['control_plane']['base_url'])}</code> &middot;
   {' &middot; '.join(links)}</p>
{scope}{by_server}

<div class="tiles">
  <div class="tile"><b>{len(phases)}</b>folders</div>
  <div class="tile"><b>{requests}</b>requests</div>
  <div class="tile"><b>{assertions}</b>assertions</div>
  <div class="tile"><b class="{'fail' if failed else 'pass'}">{failed}</b>failed</div>
  <div class="tile"><b class="{'fail' if broken else 'pass'}">{len(broken)}</b>never ran</div>
  <div class="tile"><b>{skipped}</b>skipped</div>
  <div class="tile"><b>{seconds:.0f}s</b>elapsed</div>
</div>

<h2>Requests that never ran ({len(broken)})</h2>
{_broken_section(ctx, "", phases)}

<p class="nav">{nav}</p>
{sections}
"""
    Path(path).write_text(document, encoding="utf-8")
    if ctx.config["postman"].get("html_report_bodies"):
        # With bodies in it the report holds credentials, so it stops being a
        # file to hand around.
        _protect(path)
    return str(path)


def _newman_section(result, base=None):
    """One folder: its requests, each with its assertions.

    `base` is the directory the report is written into, so a phase that also has
    newman's own HTML report can link to it — the place to go when the question
    is "what exactly did that request send", which this table deliberately does
    not carry.
    """
    failed = sum(1 for c in result.calls for a in c["assertions"] if not a["ok"])
    full = ""
    if result.html_report and base:
        link = os.path.relpath(result.html_report, base)
        full = f" <a class='full' href='{html.escape(link)}'>newman's own report ↗</a>"
    heading = (
        f"<h2 id='{html.escape(result.slug)}'>{_cell(result.name)} "
        f"<small>— {_cell(result.detail)}, {len(result.calls)} request(s), "
        f"{failed} failed assertion(s)</small>{full}</h2>"
    )
    if result.error:
        return heading + f"<p class='fail'>This folder could not run.</p><pre>{_cell(result.error)}</pre>"

    rows = []
    for index, call in enumerate(result.calls):
        passed = [a for a in call["assertions"] if a["ok"]]
        broken = [a for a in call["assertions"] if not a["ok"]]
        state = "bad" if broken or call["error"] else "good"
        rows.append(
            f"<tr class='{state}'>"
            f"<td class='s'>{index + 1}</td>"
            f"<td>{_cell(call['name'])}</td>"
            f"<td class='m'>{_cell(call['method'])}</td>"
            f"<td class='u'>{_cell(call['url'])}</td>"
            f"<td class='s'>{_cell(call['status'])}</td>"
            f"<td class='s'>{_cell(call['ms'])}</td>"
            f"<td>{_assertion_list(passed, broken)}</td></tr>"
        )
        if call["error"]:
            rows.append(
                f"<tr class='bad'><td></td><td colspan='6'>"
                f"<b>the request could not be made:</b> {_cell(call['error'])}</td></tr>"
            )
        bodies = _body_block(call)
        if bodies:
            rows.append(f"<tr><td></td><td colspan='6'>{bodies}</td></tr>")

    return (
        heading
        + "<table><tr><th>#</th><th>Request</th><th>Method</th><th>URL</th>"
        "<th>Status</th><th>ms</th><th>Assertions</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _assertion_list(passed, broken):
    """Failures spelled out, passes folded away.

    A folder can carry a hundred assertions, and the ones worth reading are the
    ones that failed. The rest are still here — a report that hides what passed
    cannot be used to answer "was this actually checked?" — just not in the way.
    """
    out = []
    if broken:
        out.append(
            "<ul class='assertions'>"
            + "".join(
                f"<li class='fail'>✗ {html.escape(a['name'])}"
                + (f"<br><code>{html.escape(a['message'])}</code>" if a["message"] else "")
                + "</li>"
                for a in broken
            )
            + "</ul>"
        )
    if passed:
        out.append(
            f"<details><summary>{len(passed)} passed</summary><ul class='assertions'>"
            + "".join(f"<li class='pass'>✓ {html.escape(a['name'])}</li>" for a in passed)
            + "</ul></details>"
        )
    if not out:
        out.append("<span class='warn'>no assertions</span>")
    return "".join(out)


def _body_block(call):
    """Request and response bodies, when the config asked for them."""
    parts = []
    for label, key in (("request", "request_body"), ("response", "response_body")):
        if call.get(key):
            parts.append(
                f"<details><summary>{label} body</summary>"
                f"<pre>{html.escape(call[key])}</pre></details>"
            )
    return "".join(parts)


def write_report(path, ctx, survivors, teardown_problems):
    directory = Path(path).parent
    directory.mkdir(parents=True, exist_ok=True)

    # The newman reports are written first, because this one links into them.
    newman_link = ""
    server_links = []
    if ctx.config["postman"].get("html_report") and postman_enabled(ctx.config):
        combined, per_server = write_newman_reports(ctx, path)
        if combined:
            newman_link = Path(combined).name
        server_links = [(name, Path(one).name) for name, one in per_server]

    failed_phases = failing_phases(ctx)
    soft_failures = [p for p in ctx.phases if not p.ok and not p.required]
    assertions = sum(p.assertions for p in ctx.phases)
    failed_assertions = sum(p.failed for p in ctx.phases)
    counted = counted_failures(ctx)
    requests = sum(p.requests for p in ctx.phases)
    broken = broken_requests(ctx)
    held = sum(p.withheld for p in ctx.phases)
    held_tile = (
        f'<div class="tile"><b>{held}</b>held back</div>' if held else ""
    )

    status = "PASSED" if not failed_phases and not survivors else "FAILED"

    # One report per server, so "did the gateway pass" is a file rather than a
    # scroll through every folder the run touched.
    server_report_links = ""
    if server_links:
        server_report_links = (
            "<p>Per server, each holding only that server's folders and only "
            "that server's totals: "
            + " &middot; ".join(
                f'<a href="{html.escape(link)}">{html.escape(str(name))}</a>'
                for name, link in server_links
            )
            + "</p>"
        )

    if survivors:
        survivor_html = (
            "<ul class='bad-list'>"
            + "".join(f"<li>{html.escape(s)}</li>" for s in survivors)
            + "</ul>"
        )
    elif not ctx.config["run"]["cleanup"]:
        survivor_html = (
            "<p class='warn'>Cleanup was disabled for this run — every artefact "
            "above is still on the deployment. Remove them with "
            "<code>--sweep-only</code>.</p>"
        )
    else:
        survivor_html = "<p class='ok'>Nothing survived teardown.</p>"

    if teardown_problems:
        survivor_html += (
            "<p class='warn'>Teardown reported:</p><ul class='warn'>"
            + "".join(f"<li>{html.escape(redact_text(p))}</li>" for p in teardown_problems)
            + "</ul>"
        )

    held_note = (
        f"<p class='warn'>Artefacts were kept: {held} destructive request(s) were "
        "held back across the run, so each folder sent fewer requests than it "
        "contains. Negative-path deletes still ran. "
        "<code>postman.skip_delete_requests</code></p>"
        if held
        else ""
    )

    gaps = (
        "<ul class='warn'>"
        + "".join(f"<li>{html.escape(g)}</li>" for g in ctx.gaps_filled)
        + "</ul>"
        if ctx.gaps_filled
        else "<p class='ok'>None — the collections carried the chain unaided.</p>"
    )

    document = f"""<!doctype html><meta charset="utf-8">
<title>Complete test — {html.escape(ctx.namespace)}</title>
<style>{SHARED_STYLE}</style>
<h1>Complete test — Postman collections, end to end</h1>
<p class="status {'bad' if status == 'FAILED' else 'ok'}">{status}</p>
<p>config <code>{_cell(ctx.config.get('_config_file'))}</code> &middot;
   target <code>{_cell(ctx.config['control_plane']['base_url'])}</code> &middot;
   namespace <code>{_cell(ctx.namespace)}</code></p>

<div class="tiles">
  <div class="tile"><b>{len(ctx.phases)}</b>phases</div>
  <div class="tile"><b>{requests}</b>Postman requests</div>
  <div class="tile"><b>{assertions}</b>assertions</div>
  <div class="tile"><b class="{'bad-list' if failed_assertions else 'ok'}">{failed_assertions}</b>failed<br><small>{counted} in required phases</small></div>
  <div class="tile"><b class="{'bad-list' if broken else 'ok'}">{len(broken)}</b>never ran</div>
  {held_tile}
  <div class="tile"><b>{len(ctx.recorder.entries)}</b>calls in total</div>
</div>

{held_note}
<h2>Phases</h2>
<p>Each Postman phase links into
   <a href="{html.escape(newman_link)}">the newman report</a>, which carries
   every request and every assertion for that folder. All of these files are
   rewritten in place on each run.</p>
{server_report_links}
<table>
<tr><th>Phase</th><th>Kind</th><th>What ran</th><th>Requests</th><th>Assertions</th>
    <th>Failed</th><th>s</th><th>Detail</th></tr>
{_phase_rows(ctx, newman_link)}</table>
{"<p class='warn'>%d phase(s) failed but were not required, so the run continued.</p>" % len(soft_failures) if soft_failures else ""}

<h2>Requests that never ran ({len(broken)})</h2>
{_broken_section(ctx, newman_link or None)}

<h2>Failed assertions ({failed_assertions})</h2>
{_failure_rows(ctx)}

<h2>Problems in the collections ({len(ctx.collection_notes)})</h2>
{_notes_section(ctx)}

<h2>Gaps the scripts filled</h2>
<p>Where a collection could not leave the platform in the state the next phase
   needed, the workflow stepped in. Each line is a collection worth extending.</p>
{gaps}

<h2>Artefacts</h2>
{_artefacts(ctx)}

<h2>Cleanup verification</h2>
{survivor_html}

<h2>Every call ({len(ctx.recorder.entries)})</h2>
<table>
<tr><th>#</th><th>Phase</th><th>Call</th><th>Method</th><th>URL</th><th>Status</th><th>ms</th></tr>
{_call_rows(ctx)}</table>
"""
    Path(path).write_text(document, encoding="utf-8")
    print(f"\nrun report     {path}")
    if newman_link:
        print(f"newman report  {directory / newman_link}")
    for name, link in server_links:
        print(f"  {name:<12} {directory / link}")
    _print_newman_html(ctx)

    environment = directory / "postman-environment.json"
    count = write_postman_environment(environment, ctx)
    kept = not ctx.config["run"]["cleanup"]
    print(
        f"postman env    {environment} ({count} variables"
        + (", artefacts kept — import and run the collection by hand)" if kept
           else "; this run's artefacts were torn down, so re-run with "
                "run.cleanup=false to hand it over)")
    )


def _print_newman_html(ctx):
    """Where newman's own reports went, and what they hold.

    Said out loud rather than left to be discovered: they carry request and
    response bodies, so they are the ones to open in a walkthrough and the ones
    not to forward without reading first.
    """
    written = [p for p in ctx.phases if p.html_report]
    if not written:
        return
    directory = Path(written[0].html_report).parent
    print(f"newman html    {directory} ({len(written)} file(s), request and response in full)")
    for phase in written:
        print(f"  {Path(phase.html_report).name:<40} {phase.name}")
    print("               owner-only: these hold live request and response bodies")


def write_postman_environment(path, ctx):
    """Export the run's environment as a file Postman can import.

    The point is that the collections stay runnable by hand. Everything a folder
    needs is in here — hosts, one account per persona with its password, a token
    for each, and every id the run produced — so somebody who would rather click
    through Postman imports this, picks the collection, and runs the same
    requests against the same artefacts without going near this harness.

    Only useful from a run that kept its artefacts: with teardown on, the
    accounts and items this names are gone by the time the file is written. See
    `postman.skip_delete_requests` and `run.cleanup`.

    Written owner-only, because it carries live tokens and passwords.
    """
    values = [
        {"key": key, "value": "" if value is None else str(value), "enabled": True, "type": "default"}
        for key, value in sorted(ctx.env_values.items())
        # Bookkeeping this harness mirrors for its own teardown; nothing in a
        # collection reads it, so it would only be noise in somebody's Postman.
        if not key.startswith(_MIRROR_PREFIX)
    ]
    document = {
        "id": ctx.namespace,
        "name": f"{ctx.namespace} (complete-test)",
        "values": values,
        "_postman_variable_scope": "environment",
        "_postman_exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "_postman_exported_using": "complete-test/complete_test.py",
    }
    _write_private_json(Path(path), document)
    return len(values)


def report_path(explicit, config, ctx):
    """Where the run report goes.

    --report wins, then `run.report_file` — one file, rewritten every run, which
    is what a report that gets linked from CI or left open in a tab needs. Only
    when no name is configured is the report named after the run, so that a
    deliberate choice to keep every run's report still can.
    """
    if explicit:
        path = Path(explicit).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    directory = Path(config["run"].get("report_dir") or (HERE / "reports"))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (config["run"].get("report_file") or f"{ctx.namespace}-complete.html")


def rebuild_newman_reports(ctx, explicit_report):
    """Rebuild the newman reports from the last run's JSON, without re-running.

    Each phase exports newman's JSON into the work directory under a fixed name,
    and that directory is kept (`postman.keep_work_dir`). So the reports can be
    rebuilt from it — which is what you want after changing how a report is
    rendered, or after adding a `server` tag to a phase: the answer is already
    on disk, and re-running a fifty-phase suite against a live deployment to
    re-render an HTML file would create accounts and items to learn nothing.

    Only the newman reports. The run report is not rebuilt: it describes a run —
    artefacts, cleanup, gaps the scripts filled — and none of that is in
    newman's JSON.
    """
    work = work_dir(ctx.config)
    config = ctx.config
    order = list(config["postman"]["phases"]) + list(config["postman"]["teardown"])
    missing = 0
    for phase in order:
        if phase.get("type") != "postman" or phase.get("enabled") is False:
            continue
        name = phase.get("name") or phase.get("folder")
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        json_out = work / f"newman-{slug}.json"
        if not json_out.exists():
            missing += 1
            continue
        result = PhaseResult(name, "postman")
        result.required = phase.get("required", True)
        result.servers = phase_servers(phase)
        result.detail = f"{phase.get('folder') or 'whole collection'} (from {json_out.name})"
        # newman's own reports are written outside the work directory and are
        # not rebuilt from JSON — they are already on disk, so the rebuilt
        # report just links to the ones that are still there.
        html_report = newman_html_path(config, slug)
        if html_report and html_report.is_file():
            result.html_report = str(html_report)
        _read_newman_report(ctx, json_out, result)
        ctx.phases.append(result)

    if not ctx.phases:
        print(f"error: no newman JSON in {work} — run once before rebuilding",
              file=sys.stderr)
        return 2

    run_report = report_path(explicit_report, config, ctx)
    combined, per_server = write_newman_reports(ctx, run_report)
    print(f"rebuilt from   {work}")
    if missing:
        print(f"  {missing} phase(s) had no JSON and were left out")
    print(f"newman report  {combined}")
    for server, path in per_server:
        print(f"  {server:<12} {path}")
    _print_newman_html(ctx)
    return 0


# ---------------------------------------------------------------------- cli


def list_phases(config):
    if not postman_enabled(config):
        print("\npostman.enabled is off — every postman phase below will be skipped")
    for key in ("phases", "teardown"):
        print(f"\n{key}:")
        for phase in config["postman"][key]:
            state = "" if phase.get("enabled", True) else "  (disabled)"
            required = "" if phase.get("required", True) else "  (not required)"
            what = phase.get("folder") or phase.get("step")
            if phase["type"] == "postman" and not phase.get("folder"):
                what = f"{phase.get('collection') or 'default'} (whole collection)"
            print(f"  {phase['type']:<8} {phase.get('name', ''):<28} {what}{state}{required}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the published Postman collections end to end, with newman."
    )
    parser.add_argument(
        "config_file", nargs="?",
        help="path to a config file (default: config.json beside this script)",
    )
    parser.add_argument(
        "--set", action="append", default=[], metavar="PATH=VALUE",
        help="override a config key, e.g. --set run.cleanup=false",
    )
    parser.add_argument(
        "--only", metavar="TEXT",
        help="run only phases matching this text; comma-separate several, "
             "e.g. --only '00 users,03'",
    )
    parser.add_argument("--list", action="store_true", help="list the phases and exit")
    parser.add_argument(
        "--sweep-only", action="store_true",
        help="skip the run; only reap namespaced leftovers from earlier runs",
    )
    parser.add_argument(
        "--install-newman", action="store_true",
        help="npm install newman beside this script and exit",
    )
    parser.add_argument("--report", metavar="FILE", help="write the run report here")
    parser.add_argument(
        "--as-shipped", action="store_true",
        help="run the collections on their own environment file, against the "
             "deployment's own data — no retargeting, no script phases, no "
             "teardown (same as --set postman.as_shipped=true)",
    )
    parser.add_argument(
        "--rebuild-reports", action="store_true",
        help="re-render the newman reports from the last run's JSON, without "
             "running anything",
    )
    args = parser.parse_args(argv)

    try:
        configuration = config_module.load(args.config_file, args.set)
    except config_module.ConfigError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    if args.as_shipped:
        configuration["postman"]["as_shipped"] = True

    if args.list:
        list_phases(configuration)
        return 0

    if args.install_newman:
        try:
            install_newman(configuration)
        except NewmanMissing as err:
            print(f"error: {err}", file=sys.stderr)
            return 2
        print(f"newman installed in {install_dir(configuration)}")
        return 0

    client_module.register_secrets(config_module.secret_values(configuration))

    recorder = Recorder()
    ctx = CompleteTestContext(configuration, recorder)

    print(f"config    {configuration['_config_file']}")
    print(f"target    {configuration['control_plane']['base_url']}")
    print(f"namespace {ctx.namespace}")
    for note in config_module.warnings(configuration):
        print(f"warning:  {note}")
    if as_shipped(configuration):
        held = configuration["postman"].get("as_shipped_skip_deletes", True)
        print(
            "mode      AS SHIPPED — the collections run on their own environment "
            "file, against the deployment's own data.\n"
            "          Nothing is retargeted at this run, no script phase runs, "
            "and there is no teardown.\n"
            "          Deletes are "
            + ("held back." if held else
               "**NOT** held back — shipped ids will be deleted.")
        )
    if configuration["postman"]["skip_delete_requests"]:
        print(
            "keeping    artefacts — the collections' own DELETE requests are held "
            "back, so folder counts are lower than usual"
        )
        if configuration["run"]["cleanup"]:
            print(
                "warning:  teardown still runs and will remove them at the end. "
                "Add --set run.cleanup=false to keep them."
            )

    if as_shipped(configuration) and not postman_enabled(configuration):
        # as-shipped runs the collections and nothing else; with newman off
        # there is no third thing left to run. Caught here rather than after a
        # run that skips every phase it has.
        print(
            "error: --as-shipped runs only the collections, and postman.enabled "
            "is off — nothing would run",
            file=sys.stderr,
        )
        return 2

    if args.rebuild_reports:
        if not postman_enabled(configuration):
            print(
                "error: postman.enabled is off, so there is no newman JSON from "
                "this config to rebuild from",
                file=sys.stderr,
            )
            return 2
        return rebuild_newman_reports(ctx, args.report)

    if args.sweep_only:
        _banner("[sweep only]")
        problems = cleanup.sweep_only(ctx)
        for problem in problems:
            print(f"    {problem}")
        print("\nsweep complete" if not problems else f"\nsweep finished with {len(problems)} problem(s)")
        return 1 if problems else 0

    if postman_enabled(configuration):
        try:
            command = newman_command(configuration)
            _log(f"newman: {' '.join(command)}")
        except NewmanMissing as err:
            print(f"error: {err}", file=sys.stderr)
            return 2

        if newman_html_reporter_missing(configuration):
            print(
                "warning:  postman.newman_html_report is on but the reporter is not "
                "installed —\n"
                f"          npm install --prefix {install_dir(configuration)} "
                "newman-reporter-htmlextra\n"
                "          continuing without newman's own HTML reports"
            )
            configuration["postman"]["newman_html_report"] = False
    else:
        # Said at the top, where the target and the namespace are, because it
        # changes what the run means: the phase log will be full of skips and
        # the summary will report no requests at all, and both are correct.
        print(
            "mode      postman.enabled is off — no folder is handed to newman.\n"
            "          The script phases still run, so the chain is still built "
            "and torn down;\n"
            "          every postman phase is skipped and no newman report is "
            "written."
        )

    lock = RunLock(configuration)
    try:
        lock.acquire(ctx)
    except PhaseError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    if postman_enabled(configuration):
        _banner("[prepare]")
        prepare_collections(ctx)
        if not ctx.collection_paths:
            print("error: no collection is enabled under postman.collections", file=sys.stderr)
            return 2

    stopped = None
    survivors = []
    try:
        for phase in selected_phases(configuration, args.only):
            result = run_phase(ctx, phase)
            if fatal(ctx, result):
                stopped = result
                _log("this phase is required and could not run; stopping the flow")
                break
    finally:
        if as_shipped(configuration):
            # Teardown deletes what the run created, and this run created
            # nothing — every id it touched belongs to the deployment. Running
            # it here would aim the collection's DELETEs at shipped ids, which
            # is the one thing this mode must never do.
            print("\nas-shipped run — no teardown: nothing here was created by this run")
        elif configuration["run"]["cleanup"]:
            _banner("[teardown]")
            for phase in selected_phases(configuration, None, key="teardown"):
                run_phase(ctx, phase, teardown=True)
            if configuration["run"]["verify_cleanup"]:
                _banner("[verify cleanup]")
                survivors = cleanup.verify(ctx)
                for survivor in survivors:
                    print(f"    SURVIVED: {survivor}")
                if not survivors:
                    print("    nothing survived")
        else:
            print("\ncleanup disabled — artefacts left in place")

        path = report_path(args.report, configuration, ctx)
        write_report(path, ctx, survivors, ctx.teardown_problems)

    lock.release()

    _banner("=" * 64)
    failed_phases = failing_phases(ctx)
    failed_assertions = sum(p.failed for p in ctx.phases)
    broken = broken_requests(ctx)
    print(f"phases       {len(ctx.phases)} ({len(failed_phases)} failed)")
    print(f"requests     {sum(p.requests for p in ctx.phases)} ({len(broken)} never ran)")
    print(f"assertions   {sum(p.assertions for p in ctx.phases)} ({failed_assertions} failed)")
    print(f"calls        {len(recorder.entries)}")
    unsendable = broken_requests(ctx, "collection")
    unanswered = broken_requests(ctx, "deployment")
    for entries, heading in (
        (unsendable, "requests that could not be sent — fix these in the collection:"),
        (unanswered, "requests sent but never answered — a finding about the deployment:"),
    ):
        if not entries:
            continue
        print(f"\n{heading}")
        for phase, entry in entries:
            print(f"  [{phase.name}] {entry['method']} {entry['request']}")
            print(f"      {entry['reason']}")
            if entry.get("raw"):
                print(f"      URL in the collection: {entry['raw']}")
    for key, value in ctx.results.items():
        print(f"{key:<12} {value}")

    if stopped:
        print(f"\nFAILED: {stopped.name} could not run — {stopped.error}")
        return 1
    postman = configuration["postman"]
    counted = (unsendable if postman["fail_on_broken_request"] else []) + (
        unanswered if postman.get("fail_on_unanswered_request", True) else []
    )
    if counted:
        parts = []
        if unsendable and postman["fail_on_broken_request"]:
            parts.append(f"{len(unsendable)} could not be sent (collection defects)")
        if unanswered and postman.get("fail_on_unanswered_request", True):
            parts.append(f"{len(unanswered)} went out and were never answered (the deployment)")
        print(
            f"\nFAILED: {len(counted)} request(s) produced no response — "
            + ", ".join(parts)
            + ". Either way the endpoints they cover are untested, not passing."
        )
        return 1
    if failed_phases:
        errored = [p for p in failed_phases if p.error]
        print(
            f"\nFAILED: {len(failed_phases)} required phase(s) — {len(errored)} could "
            f"not run, {counted_failures(ctx)} assertion(s) failed"
        )
        return 1
    if survivors:
        print(f"\nFAILED: {len(survivors)} artefact(s) survived teardown")
        return 1
    if failed_assertions:
        print(
            f"\nPASSED with {failed_assertions} assertion failure(s) — "
            "postman.fail_on_assertion_failure is off, so they are reported "
            "but not counted against the run"
        )
        return 0
    print("\nPASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
