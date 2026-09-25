# complete-test

The published Postman collections, driven end to end by newman.

```bash
python3 complete-test/complete_test.py
```

`main/e2e.py` proves the onboarding workflow by calling the APIs itself. This
harness proves the same workflow **and** the API contract around it: every
positive case and every negative one the collections carry, run in an order that
leaves a real, working chain behind — a provider, an organisation, an item with
data behind it, a policy, and an audit trail — then removes all of it.

---

## The two kinds of phase

`postman.phases` in `config.json` is the flow. Each entry is one of two things:

| Kind | What it is |
|---|---|
| `postman` | a folder handed to newman, asserted by the collection's own tests |
| `script` | work no collection can do — Keycloak users, RabbitMQ, S3, Postgres |

A `postman` phase names its folder. It may also name **none**, and then it runs
the whole collection in one newman process — which is what a collection whose
chain runs *between* its folders needs: each phase is a process of its own, and
of what a collection's scripts write only uuid-valued collection variables
survive one ending, so a token stored in folder 00 is gone by folder 01. Run
whole, such a collection behaves exactly as it does when Run is pressed in
Postman. `complete-test/temporary/` is the working example.

Every API call this harness can make through a collection, it makes through a
collection. The scripts fill exactly the gaps a collection cannot reach, and the
report names each one under **Gaps the scripts filled** — that list is the
backlog of what the next version of a collection could cover.

```
script   00 users                 create_users        Keycloak has no create-user API
postman  00 auth & token          folder 00
postman  01 … 08                  folders 01-08
script   08 item                  create_item         the item this run's servers need
postman  19 acl apd               folder 19           access, before anything consumes the item
script   19 policy                ensure_policy       a policy on this run's item
postman  09 … 16                  folders 09-16
script   20 data onboarding       data_onboarding     RabbitMQ / S3 / files API
script   21 resource servers      resource_servers    granted token reads, ungranted does not
postman  23 … 28                  data plane          NGSI-LD: temporal, entities, latest,
                                                      search, download, app-id auth
postman  29 … 30                  data plane          gateway: entities and complex search
postman  17 auditing              folder 17
script   22 audit trail           auditing            this run's own rows arrived
postman  18 dashboard             folder 18
```

`20 data onboarding` and `21 resource servers` are the data plane as
`main/e2e.py` does it in its phases 04 and 05 — publish to the item's RabbitMQ
exchange, ingest STAC and upload the GeoTIFFs, upload into the databank; then
read every declared server with a granted token and prove an ungranted one is
refused.

Phases 23-30 are the data-plane collection: the NGSI-LD and gateway API
surface, every status code each endpoint is specified to return, 51 requests
over eight folders. They read **this run's own item**, not the deployment the
collection was written against — see *Retargeting a collection* below.

They do **not** replace `21 resource servers`. Two things the collection cannot
do, which is why both still run:

- **assert the refusal.** Nothing in it signs in as an account that was never
  granted access, so a blanket-allow bug would pass every folder of it.
- **reach OGC or the file server.** No collection exists for those yet.

The overlap is the point: the collection says the endpoint is correct, the
script says the chain is.

A gateway item is answered by an adaptor consuming the item's queue, not out of
storage, so the phases that read one carry `"adaptor": "gateway"` and get one
started for exactly their own length. Without it the request is never replied
to and the read times out — which the collection reports as a failed assertion
on a 504, indistinguishable from the gateway being broken.

**What the adaptor answers with matters as much as that it answers.** Both
gateway folders filter `q=District==PUNE` and then assert
`item.District.toUpperCase() === "PUNE"` on every row they got back. The adaptor
does not read the query — the incoming message is a trigger and its body is
never parsed — so it cannot filter, and a reply whose rows have no `District` at
all makes that assertion *throw* rather than fail: the folder reports a broken
gateway when the gateway is fine and only the payload was wrong.

So `gateway_adaptor.api_url` defaults to **null**, and the adaptor answers from
its own `SAMPLE_RECORDS` — shaped like the CSC dataset a gateway item on dev
answers with (`CSCID`, `District`, `Sub_District`, `Longitude`, `Latitude`,
`Pincode`), every row `PUNE`. Every row being PUNE is deliberate: it is the only
way an unfiltered reply can satisfy an "every row matches the filter" assertion
honestly.

Three sources, in order of precedence, so this stays a config edit when the next
collection filters on something else:

| Setting | The adaptor answers with |
|---|---|
| `gateway_adaptor.data_file` | the JSON array in that file |
| `gateway_adaptor.api_url` | whatever that upstream returns under `results_key` |
| neither (the default) | its built-in `SAMPLE_RECORDS` |

### Two subjects: this run's item, and a reference one

Each gateway folder runs **twice**, because there are two questions and one
failure should not answer both:

| Phase | Subject | Asks |
|---|---|---|
| 29, 30 | `resource_servers.gateway.reference_item_id` | does this endpoint work? |
| 31, 32 | the item this run created, with the adaptor running | does this run's chain work? |

The reference item is a resource that already exists on the deployment, with
data behind it and whatever attributes the collection's `q=` filter names — the
one the collection was written against. A run whose own item came out empty then
says *the endpoint is healthy and the chain is not*, rather than failing once
for both reasons and leaving you to guess which.

It is configured per server, not per collection, so any server can grow one:

```json
"resource_servers": {
  "gateway": { "reference_item_id": "ff2ee34b-61a0-4881-afa3-f6f2d0e66911" },
  "ngsild":  { "reference_item_id": null }
}
```

Null skips those phases — the guard is `"when":
"config:resource_servers.gateway.reference_item_id"`, which asks the *config*
rather than a run variable, so it is answerable before the run has produced
anything.

### Temporal data

`timerel=between` asks which records fall inside a window, and a dataset whose
rows all carry one timestamp cannot answer it — the window holds all of them or
none, so a broken filter and a working one look identical. Two settings make
that folder mean something:

- `resource_servers.ngsild.query_types` includes **`TEMPORAL`** as well as
  `ATTR`. It is declared on the catalogue item, and without it the server
  refuses a temporal query on this run's item whatever the data says.
- `ngsild_publish.spread_hours` (default 24) spreads `observationDateTime`
  evenly backwards from the newest record, instead of stamping every record with
  the same instant. `count` (default 24) decides how many rows there are to
  spread — it has to exceed the largest page any folder asks for, and
  `Latest Data`'s positive case asks for `size=10` and asserts it got exactly
  ten.
- `ngsild_publish.observation_end` (default `null`, meaning *now*) is where the
  newest record sits. It exists for a collection that compares against a date
  the harness cannot retarget: a test script naming a date is asserting about
  it, so it is never rewritten, and data outside the era such an assertion
  expects then fails a test about a query that worked perfectly. Anchoring the
  publish satisfies that without editing the collection. Nothing needs it
  today — every date in the data-plane collection now lives in a URL or a body,
  where the rewrite reaches it.

The window the run published into is handed to the collections, padded an hour
each side so a query over it asks *is the data there* rather than *is the
boundary inclusive*. It comes in three spellings, because this collection uses
three — a window in the wrong one is not a near miss, the server either rejects
it or reads a different instant:

| Variable | Looks like | Used by |
|---|---|---|
| `data_window_start` / `data_window_mid` / `data_window_end` | `2025-10-31T12:00:00+05:30` | `timerel=between` query params |
| `…_start_utc` / `…_mid_utc` / `…_end_utc` | `2025-10-31T06:30:00Z` | `temporalQ` bodies |
| `…_start_compact` / `…_mid_compact` / `…_end_compact` | `2025-10-31T12:00:00+0530` | `beforeTemporal` search criteria |

**`mid` is insurance, not something a live request needs today.** `Latest Data`
and `Download Data` ship `timeRel` / `time` / `endTime` params that are all
**disabled** — their positive cases send only `size` and `page`. Were they
enabled, `timeRel=before` *together with* `time` and `endTime` reads two ways
that want opposite data: if `before` filters on `time` alone the data has to sit
earlier than it, if the pair is a window the data has to sit inside it. No single
instant satisfies both, but a `time` in the **middle** of the data does — either
reading then selects about half the rows, and the assertion is
`result.length === size`, not a total. So every `time=` param is already mapped
to `mid` and every `endTime=` to `end`; enable them and the only other change
needed is doubling `count`, so half the data still fills a page.

The collection's own fixed timestamps are rewritten to these names, so what the
publisher wrote and what a temporal query asks for cannot drift apart. Before
anything has been published — and on a run that publishes nothing — they fall
back to a year-wide window, so the request is still well formed: a temporal
request built from an empty variable is a request that never runs, which would
be reported as a collection defect rather than as the missing data it is.

`python3 complete-test/complete_test.py --list` prints the current list.

---

## Where a value lives

This is the whole division, and nothing is configured twice.

**Filled into Postman**, generated fresh for every run and written to
`reports/newman/environment.json`:

- hosts and path bases — `base_url`, `acl_url`, `kc_url`, `realm`, `client_id`,
  `auth_base`, `cat_base`, `acl_base`, `audit_base`
- one account per persona — `new_user`, `org_admin`, `admin_user`,
  `provider_user`, `consumer_user`, `member_user`, `no_org_user`,
  `delegate_user`, `cos_admin`, each with its password
- a fresh token for each — `provider_token`, `consumer_token`, `cosadmin_token`,
  `delegate_token`, `orgmem_token`, …
- every id the flow produces — `org_id`, `organizationId`, `item_id`,
  `access_request_id`, `policy_id`, `consumer_user_id`, …

**Kept in `config.json`**, because no collection can carry it:

- Keycloak's admin client, used to create and delete the test accounts
- RabbitMQ, for the NGSI-LD publish and the gateway adaptor
- S3 and Postgres, for OGC onboarding and the teardown sweep
- local file paths — the GeoPackage, the GeoTIFFs, the CSV that gets uploaded
- `postman.extra_variables`, for any collection variable this harness does not
  know about yet — written into the environment last, so it overrides everything

`config.json` is gitignored. Copy `config.example.json` and fill it in.

### Three consumers, with jobs

A collection that tests the user API **changes the account it is pointed at** —
folder 03 changes a password and rewrites a profile, folder 04 revokes a KYC.
Run against one shared consumer, those requests break the account the rest of
the run depends on, and the audit assertions fail for a reason that has nothing
to do with auditing.

So the consumers are not interchangeable:

| Account | Job |
|---|---|
| `consumer` | the end-to-end chain — access request, policy, data-plane reads, audit trail. **Nothing may change its credentials.** |
| `consumer2` | a second consumer where a collection wants two (`consumer2_token`), and the target for folders that alter account state |
| `consumer3` | the one folders may break — password changes and profile rewrites go here |
| `nopolicy` | never granted access; proves an ungranted token is refused |

A phase points a persona at a different account with `personas`:

```json
{ "name": "03 user & role", "type": "postman", "folder": "03",
  "personas": { "consumer": "consumer3" } }
```

For the length of that folder, every consumer variable — `consumer_user`,
`consumer_pass`, `consumer_token`, `consumer_user_id`, `userId` — resolves to
`consumer3`, so `/admin/{{consumer_user_id}}/update` rewrites the same account
the folder signs in as. The override does not leak into the next phase.

When a new collection arrives, look for requests that change a password, revoke
a role, or delete a shared object, and route them the same way. That is cheaper
and more honest than repairing the damage afterwards.

### Names a collection owns

The mirror image of pinning. `variables` says "this run's value belongs in the
collection's placeholder"; `own_variables` says "the collection's own scripts
produce this, and an answer from the harness would be the wrong one":

```json
"controlplane_kt": {
  "own_variables": ["org_name", "org_id", "item_id", "policy_id"]
}
```

It exists because of how Postman resolves a name: the environment outranks the
collection, so a seeded value silently shadows what a pre-request script just
stored — and the folder that then compares a response against what its own
script stored fails on a mismatch nobody caused. A self-driven collection, one
that signs in and builds its own organisation and item, wants the run's accounts
and hosts and nothing else. A phase may add names of its own the same way, and
none of it applies in as-shipped mode, where the shipped file already wins.

### What is *not* seeded from the shipped environment file

The collection's own environment file is used as a starting point so a variable
the harness has never heard of still has a value — but every id, token and
password in it is dropped rather than seeded. That is a safety property: a
shipped `user_feedback_id` names a real row on the deployment and the collection
has a DELETE keyed on exactly that variable. Every id this run uses is one this
run produced.

---

## Teardown

Same division, in reverse. `postman.teardown` runs first — the collection's own
DELETE requests, for the artefacts it can remove itself. Everything left falls
to the same sweep `main/e2e.py` uses:

- catalogue items, by the run namespace and by the ids the collections announced
- policies and access requests
- organisations, Keycloak accounts, exchanges, queues, buckets, database rows

Every artefact carries the run's namespace, so teardown is a deterministic sweep
rather than id bookkeeping that goes missing when a phase dies partway through.
Bodies a collection builds in a pre-request script cannot be namespaced from
outside, so those are torn down by the id their response announced — which is
why every phase's exported environment is captured.

Teardown runs in a `finally` block, and the run fails if anything survives it.

`--sweep-only` reaches further than a teardown does, because it has to: after a
crash the ids are gone and only the namespace is left. It resolves the borrowed
cos_admin and OGC provider from Keycloak so neither is mistaken for an orphan,
reads this run's item ids out of `request` before the database sweep deletes
those rows, and uses them to reap the two things nothing else names — the
Elasticsearch data indices, which carry no prefix of their own, and the OGC
collections, which live in the OGC server's own database keyed by item uuid.

```bash
python3 complete-test/complete_test.py --sweep-only    # reap orphans from a crash
python3 complete-test/complete_test.py --set run.cleanup=false
```

### Any one step, by hand

Every step teardown takes is also a script of its own, with its own config:
paste in the connection values and the ids (or the prefix) and run it. The
data-plane ones are the `script/*_Automation_Script/` deletion scripts the
harness already calls; the ControlPlane ones — policies and item, the collection's
own DELETE endpoints by id, the Keycloak account sweep, the database sweep —
are in
[`script/CONTROLPLANE_Cleanup_Script/`](../script/CONTROLPLANE_Cleanup_Script/README.md),
whose README lists all twelve in the order teardown runs them. Use them when a
run left something behind and rerunning the whole teardown is not wanted, or
when the thing to remove was not created by a run at all.

---

## Phase order is part of the test

The item this run creates is `RESTRICTED`, so until a policy grants the consumer
access, **every consumer-side call against it is answered 403 "Access denied for
restricted item"** — subscriptions, asset requests and interactions all fail, and
it reads like three broken endpoints rather than one missing grant.

So the access phases run immediately after the item exists:

```
08 catalogue crud → 08 item → 19 acl apd → 19 policy → 09 … 16
```

`run.access_expiry_days` decides how long that grant lasts. `main/e2e.py`
defaults to 1 day, which is plenty to read the data plane with. The complete
test sets **730**, because the platform refuses a subscription whose expiry is
later than its policy's, and collections write dates years out.

---

## Keeping the artefacts to look at

Two independent switches, because two different things delete.

| Switch | Stops |
|---|---|
| `run.cleanup: false` | **teardown**, after the flow — the sweep of Keycloak, RabbitMQ, S3, Postgres, items |
| `postman.skip_delete_requests: true` | the deletes a collection does **during** the flow — folder 08 removes its own item, folder 12 its subscription, folder 01 its resource server |

Turning teardown off alone is not enough: a CRUD folder tests its own delete, so
the run can finish having already removed the thing you wanted to inspect. To
keep everything, use both:

```bash
python3 complete-test/complete_test.py \
    --set postman.skip_delete_requests=true \
    --set run.cleanup=false \
    --set run.prefix=e2e-keep
```

The distinct prefix matters: `--sweep-only` reaps by prefix, so a run kept under
its own prefix cannot be swept away by a later cleanup of the usual one. Remove
it deliberately when you are finished:

```bash
python3 complete-test/complete_test.py --set run.prefix=e2e-keep --sweep-only
```

**Negative-path deletes still run.** A request whose name says it expects a
client error — `Delete 404`, `Delete 401` — is refused or addresses an id that
never existed, so it removes nothing and is worth keeping. Of the 19 destructive
requests in the current collection, 12 are held back and 7 still run.

`destructive_requests` lists requests that destroy something without being a
DELETE, matched on name; it defaults to `["*Deactivate*", "*Revoke*"]`, which
catches the policy deactivate and the KYC revoke.

Teardown is never affected — its whole purpose is to delete, so the switch does
not reach `postman.teardown`. The run prints what it held back, and so does the
report.

---

## Handing the collections to somebody without this harness

Every run exports `reports/postman-environment.json` — a file Postman imports
directly. It carries everything a folder needs: hosts and path bases, one
account per persona with its password, a token for each, and every id the run
produced. Import it, pick the collection, and run the same requests against the
same artefacts with no Python involved.

It is only useful from a run that **kept** its artefacts, since otherwise the
accounts and items it names are gone by the time you open it:

```bash
python3 complete-test/complete_test.py \
    --set postman.skip_delete_requests=true \
    --set run.cleanup=false \
    --set run.prefix=e2e-share
```

Then hand over `reports/postman-environment.json` together with the collection
from `resource/`. Sweep afterwards with
`--set run.prefix=e2e-share --sweep-only`.

It holds live tokens and passwords for those accounts, so it is written
owner-only and should be treated as a credential — they are disposable test
accounts, but they are real ones until the sweep.

### What this harness does and does not modify

The **collection under `resource/` is never written to.** A working copy is made
in `reports/newman/` for each run, and only that copy is adjusted:

- request body names get the run namespace, so artefacts created under a fixed
  name are still swept by prefix afterwards
- a small script is added that records ids for teardown
- shipped ids, tokens and passwords in the collection's own variables are
  emptied or replaced with the all-zero uuid, so no request can resolve to a
  record on the deployment that this run did not create
- a collection variable a script *reads* but the collection never *declares* is
  declared empty, so the run can fill it. `pm.collectionVariables.get("x")`
  does not fall back to the environment the way `{{x}}` does — it returns
  undefined, the script builds `"Bearer " + undefined` out of it, and every
  request in the folder 401s, which reads as a credentials problem and is not
  one. Phase 2 of the control-plane collection has five such names
- a credential written into a request's own auth block rather than into a
  variable is re-pointed at this run's application — the data-plane
  collection's app-id request carries a real application id and secret, which
  emptying the variables does not reach. Each one is listed in the report under
  **credential in request**, because the collection still carries it.

No assertion, no URL and no body is touched. The source file you maintain in
Postman stays exactly as you exported it.

The **environment is generated**, not edited: this harness builds it from
config plus what the run produces, so nothing has to be kept in step by hand.

---

## Requests that never ran

The first section of both reports, above the assertion failures, and it fails
the run on its own — separately from `fail_on_assertion_failure`, which is off
while the collections' findings are untriaged.

A failed assertion is a finding *about the platform*: the request went out, the
server answered, the answer did not match the spec. A request that produced no
response is different in kind — its tests did not fail, they did not run, which
is the one outcome a suite must never report as green.

There are two ways for that to happen, and they are reported separately because
they are addressed to two different people:

| | What it means | Switch |
|---|---|---|
| **Could not be sent** | the request never became a call — a brace in the URL, a variable nobody set, a hostname that does not resolve | `fail_on_broken_request` |
| **Sent, never answered** | correctly addressed, went out, and the server refused it or never replied | `fail_on_unanswered_request` |

Both default to on. Telling the QA team to fix their collection because an
endpoint hung sends them looking for a defect that is not there, so the split
matters more than it looks.

Each one is reported with the URL exactly as the collection writes it:

```
requests that could not be sent — fix these in the collection:
  [03 user & role] POST POST /user/update – Update Profile 200
      the URL is malformed in the collection — a brace is unmatched, so the
      variable was never substituted and the whole placeholder was used as the
      hostname.
      URL in the collection: {base_url}}/iudx/v2/auth/user/update

requests sent but never answered — a finding about the deployment:
  [11 asset requests] DELETE DELETE /asset/request/:id – Delete 200
      v2.dev.iudx.io did not answer in time.
      URL in the collection: {{base_url}}/iudx/v2/auth/asset/request/{{asset_request_id}}
```

`required: false` does not excuse either: that flag exists to tolerate known
platform *findings*, and an untested endpoint is not one.

### When a phase looks stuck

It usually is not. newman's own output is captured rather than streamed — the
reports are built from its JSON, and its cli chatter would bury the phase log —
so a folder holding several requests against an endpoint that accepts the call
and never answers prints nothing for minutes. A line now goes out every 20
seconds while a phase runs, naming the two numbers that tell you whether it is
stuck or just slow:

```
[11 asset requests]
    11 – Asset Requests: 21 request(s) via newman
    … still running, 20s elapsed (per-request timeout 15s; 21 request(s), so at worst 315s)
```

newman's full output is kept beside the phase's other artefacts as
`reports/newman/newman-<phase>.log`.

A phase may cap its own `request_timeout_ms`. 60s is right for an endpoint that
is merely slow and wrong for a folder with three requests against one that hangs
— that is three minutes of silence every run to learn what the first request
already said. Folder 11 is capped at 15s for exactly that reason, which took it
from 185s to 50s without losing the finding.

A per-request timeout is not enough on its own, because an endpoint can answer
*slowly* without any single request ever timing out. Folder 17 is where that
showed: four requests at a 60s per-request timeout, still printing heartbeats
past 360s. So every postman phase also has a wall-clock cap — `max_seconds` on
the phase, `postman.max_phase_seconds` for all of them, and failing both, the
worst case the heartbeat already prints plus 30s of grace. newman is given it as
`--timeout` and the harness watches the clock as well, killing the process group
if newman does not stop itself. Folder 17 is pinned at a 15s per-request timeout
and a 60s cap; it is `required: false`, so hitting the cap is reported as a
finding and the run carries on to the audit-trail assertion behind it.

---

## One run at a time

Every phase hands newman the same `environment.json`, rewritten just before it
runs. Two runs sharing a work directory therefore overwrite each other's
variables mid-flight, and **the symptom is not an error**: a phase reads the
other run's values and fails on a URL that never resolved — a gateway read
pointed at the control plane, an id left as the literal `{{name}}`. That is
indistinguishable from a broken collection until the file timestamps are
compared.

So a run takes a lock on its work directory and a second one refuses, naming the
pid and namespace that holds it. A lock whose process is gone is stale and is
taken over, so a killed run never blocks the next one. To run two deliberately,
give each its own directory:

```bash
python3 complete_test.py --set run.report_dir=reports/second
```

`--set postman.ignore_run_lock=true` takes a live lock over instead, which is
almost never what you want.

## Reports

All **rewritten in place on every run** — same path each time, so they can be
bookmarked, linked from CI, or left open in a tab and reloaded:

| Report | What it holds |
|---|---|
| `reports/complete-test-report.html` | the run: requests that never ran, failed assertions, phases, gaps the scripts filled, artefacts, cleanup, every call |
| `reports/newman-report.html` | **every** folder newman ran, every server, in run order — each request with its status, timing and every assertion |
| `reports/newman-report-<server>.html` | one per server — only that server's folders, and totals that count only those |

They cross-link: each Postman phase in the run report jumps to that folder's
section in the combined newman report, the combined report lists the per-server
ones, and each of those links back.

Open them from a terminal with `xdg-open reports/newman-report.html`; the paths
are printed at the end of every run.

They are written **by a run** — a fresh checkout has none until something has
run. To re-render them from the last run's newman JSON without running anything:

```bash
python3 complete_test.py --rebuild-reports
```

Each phase leaves its JSON in `reports/newman/` and that directory is kept, so
this is what to reach for after changing how a report is rendered or after
adding a `server` tag to a phase: the answer is already on disk, and re-running
a fifty-phase suite against a live deployment to re-render an HTML file would
create accounts and items to learn nothing. It rebuilds only the newman
reports — the run report describes a run (artefacts, cleanup, gaps the scripts
filled) and none of that is in newman's JSON. A phase whose `when` guard never
fired has no JSON and is reported as left out.

### newman's own report, per phase

The reports above are built by this harness from newman's JSON: they carry the
verdicts and the sequence, and — unless `html_report_bodies` is on — not the
bodies. When the question is *what exactly did that request send and get back*,
which is what a walkthrough or a hand-over is read for, switch on newman's own:

```json
"postman": { "newman_html_report": true }
```

One file per phase in `reports/newman-html/`, because a phase is one newman
process, each linked from its section in the combined report and listed at the
end of the run. It needs the reporter beside newman —
`npm install --prefix complete-test newman-reporter-htmlextra` — and a run that
asks for it without it says so and carries on.

Two things stay out of those files by default: the `Authorization` header
(`newman_html_skip_headers`), and the request and response bodies of any request
named in `newman_html_hide_bodies` — a sign-in posts a password and answers with
a token, and no header filter helps there. They are written owner-only even so:
they hold real bodies, so read one before forwarding it.

### One report per server

`newman-report.html` answers "did this run pass" — it is the only place the
sequence is visible, script phases and all. It does not answer "did the gateway
pass" without scrolling, and a server whose folders are spread across a
fifty-phase run is easy to misread. So each server also gets a file of its own,
holding exactly its folders with its own totals.

Which folders belong to which server is **config, not inference**. Every phase
carries a `server`:

```json
{"name": "24 ngsi-ld temporal & entities", "type": "postman",
 "server": "ngsild", "collection": "dataplane", "folder": "NGSILD"}
```

A list where one folder serves more than one — the data plane's token folder
mints what both the NGSI-LD and the gateway reads use, so it appears in both:

```json
{"name": "23 data plane token", "type": "postman",
 "server": ["ngsild", "gateway"], "collection": "dataplane", "folder": "Token"}
```

The names are free text and are **not** checked against `resource_servers`.
Two folders that should be read together are merged by giving them the same
name (`"server": "dataplane"` on both the NGSI-LD and the gateway phases yields
one `newman-report-dataplane.html`), and split by giving them different ones. A
phase with no `server` appears in the combined report and nowhere else. Turn
the whole thing off with `postman.server_reports: false`.

Today's run produces `controlplane`, `ngsild` and `gateway`. The `file` and
`ogc` servers get theirs as soon as their collections arrive — the phases are
tagged, and nothing else has to change.

newman writes one report per process, and a phase *is* a process — so its own
HTML reporter would leave twenty files, none of which describes the run. Each
phase exports JSON instead, and the combined report is built from that. Nothing
accumulates: `reports/newman/` holds only working files (the generated
collection, the environment, the per-phase JSON), all under fixed names.

Neither report carries a request or response body by default, so neither can
carry a credential. Set `postman.html_report_bodies` to `true` to include them
while debugging — the newman report is then written owner-only, and should be
treated as secret.

Rename them with `run.report_file` and `postman.report_file` — the per-server
files take their stem from the latter, so `"dx-newman.html"` yields
`dx-newman-gateway.html`. Set `run.report_file` to `null` to go back to one
report per run, named after the run's namespace.

---

## Two questions, two modes

The default run points every collection at artefacts **this run** created — its
own accounts, its own item, its own data window — and answers *does the chain
work end to end*. That is what the whole retargeting apparatus is for.

There is a second question the same collections can answer: *does the collection
pass on its own terms*, against the deployment's own data — what somebody sees
when they press Run in Postman, reproduced in CI and reported the same way.

```bash
python3 complete_test.py --as-shipped        # or postman.as_shipped: true
```

It changes five things **together**, because half of it would produce a
collection that is neither:

| | default | `--as-shipped` |
|---|---|---|
| shipped environment ids, tokens, passwords | dropped | kept, and they **win** over the run's values |
| collection variables / request auth | sanitised, re-pointed | untouched |
| `rewrite`, `variables` pinning, body namespacing | applied | none — the working copy is byte-identical to the file on disk |
| script phases | run | skipped: nothing is pointed at what they build |
| teardown | runs | none — this run created nothing |

The run's own values stay *underneath* the shipped ones rather than being
dropped, so a name the shipped file never mentions still resolves to something
rather than to a literal `{{name}}` — which would be reported as a request that
could not be sent, and an unset variable is not that.

### The safety property

This mode reads and writes somebody else's data: every id in scope belongs to
the deployment. The collections have DELETE requests keyed on exactly those
variables, and `DELETE /auth/app/{{app_id}}` against a shipped `app_id` removes a
real application. So `postman.as_shipped_skip_deletes` is **on by default** and
holds back the delete, revoke and deactivate requests — 12 of the control
plane's 355. Creates, updates and reads still run, because those are what a
Postman run does and reproducing it is the point.

Turn it off only when you mean it:

```bash
python3 complete_test.py --as-shipped --set postman.as_shipped_skip_deletes=false
```

The startup banner says which way it is set, every time.

### The four combinations

| `as_shipped` | `as_shipped_skip_deletes` | mode | shipped env | retarget | scripts | teardown | deletes |
|---|---|---|---|---|---|---|---|
| **true** | **false** | as shipped | 25/25 | no | skipped | none | **run** |
| true | true | as shipped | 25/25 | no | skipped | none | held (12) |
| **false** | **false** | automation | 8/25 | yes | run | run | run |
| false | true | automation | 8/25 | yes | run | run | run |

*(counts measured against the data-plane environment file and the control-plane
collection; they move with the collections, the shape does not.)*

`as_shipped_skip_deletes` is **inert** unless `as_shipped` is on — it is only
read inside that branch, which is why the last two rows are identical. Setting
`as_shipped` back to `false` alone returns to the normal run; there is no second
flag to remember.

The top row is a Postman "Run collection" reproduced exactly, and it **deletes
real rows on the deployment** — the app, catalogue item, subscription,
delegation, credit/compute/asset requests and resource server that the shipped
environment names. That is fine when the environment points at throwaway dev
artefacts and is not fine otherwise, and nothing here can undo it: the harness
has no record of what was there. What it buys is the collection's own DELETE
requests being *exercised*, assertions and all — they never run any other way.

In the automation mode the equivalent knobs are different ones, and neither of
these two touches them: `postman.skip_delete_requests` holds back a CRUD
folder's deletes mid-flow, and `run.cleanup` controls the teardown phase.

---

## Adding the next collection

A config edit, not a code change.

```json
"postman": {
  "default_collection": "controlplane",
  "collections": {
    "controlplane": { "enabled": true, "collection": "resource/…/controlplane.json" },
    "files":        { "enabled": true, "collection": "resource/…/files.json" }
  },
  "phases": [
    { "name": "31 files api", "type": "postman", "collection": "files", "folder": "01" }
  ]
}
```

A phase names a collection only when it is not `default_collection`, so the
control plane's twenty-odd folders stay readable and anything else reads as the
exception it is. A phase naming a collection nobody has enabled is **skipped and
says so**, not failed — so the phase list can be written ahead of the
collections that fill it.

Then delete whatever `script` phase it genuinely replaces, and only that much. A
collection retires a script phase when it does the same job on this run's own
artefacts; one that covers the same endpoints against ids of its own does not —
the data-plane collection is that case, and `21 resource servers` still runs
beside it. The report's **Gaps the scripts filled** section is the list of what
is still worth writing.

### Pinning a variable to one collection

Two collections can use the same name for different things and both be right:
`base_url` is the control plane's API base in one and the data plane on the same
deployment in the other. A `variables` block settles it for one collection's
phases, without either collection being edited:

```json
"dataplane": {
  "enabled": true,
  "collection": "resource/…/data-plane.json",
  "variables": {
    "base_url":         "{{ngsild_host}}/dataplane",
    "basePath":         "ngsi-ld/v2",
    "keycloak-url":     "{{kc_netloc}}",
    "sampleResourceId": "{{item_id}}"
  }
}
```

A value may reference another variable, and it is resolved against everything
the run knows when that phase starts — which is how a collection's shipped
placeholder gets pointed at an id this run produced. A name nothing has set
resolves to empty and is logged, because a half-resolved URL is a request that
never runs. A phase may carry its own `variables` block to narrow the
collection's further; the gateway folders use one to swap `sampleResourceId`
for the gateway lane's item.

Pinned values are deliberately not remembered after the phase: they are what
that collection means by a name, not what the run means by it.

### Retargeting a collection

Pinning only reaches what a collection put in a variable. The resource id in
`?id=04bf3f4e-…`, the one in `/…/51ef8ad1-…/download`, and the `timeAt` of a
`between` query are written into the URL itself, and no environment can reach
them — so those requests ask about somebody else's data however much this run
has published. A `rewrite` block replaces them in the working copy:

```json
"dataplane": {
  "rewrite": {
    "04bf3f4e-d74e-11f0-a2ba-eb694a98984a": "{{sampleResourceId}}",
    "51ef8ad1-a6b4-4321-8863-acd58df3ea5c": "{{sampleResourceId}}",
    "ff2ee34b-61a0-4881-afa3-f6f2d0e66911": "{{sampleResourceId}}",
    "2021-03-28T21:43:44+05:30": "{{data_window_start}}",
    "2026-03-29T21:43:44+05:30": "{{data_window_end}}"
  }
}
```

One literal to one replacement, applied to the URL, the body and the
**pre-request** scripts — the collection builds URLs and bodies in a script as
often as it declares them, and a rewrite reaching only the declared half would
leave the two disagreeing, with newman sending the half you did not fix. Test
scripts are never rewritten: one naming an id is asserting something about it,
and rewriting an assertion would change what the suite claims rather than what
it asks.

**Literal, never a pattern.** The negative cases are built out of ids that look
exactly like the positive ones — `…ea5c` is the resource the 200 reads and
`…ea5a` is the one the 403 may not — so a rule clever enough to find both would
destroy the distinction the folder exists to test. What gets rewritten is what
somebody named, and every one is listed in the report under **retargeted**.

A rewrite that matches nothing is reported too, in red: it usually means the
collection has been re-exported with the id changed, and those requests are
quietly back to reading someone else's data.

### Searching this run's data

The search folder filters on a field the collection's own dataset has:
`{"searchType": "term", "field": "bank_id", "values": ["IDBI"]}`. Two things
make that query find this run's records:

- the publisher's sample rows carry `bank_id: "IDBI"`, alongside the
  `observationDateTime` every row already had;
- the rewrite maps `"field": "bank_id"` to `"field": "bank_id.keyword"`.

The second is not cosmetic. A `term` search is exact-match against the *indexed
token*, and the index is created by the consumer draining the queue with dynamic
mapping — so `bank_id` is analysed and holds `idbi`, while `bank_id.keyword`
holds `IDBI` verbatim. Probed directly against a live item:

| Query | Result |
|---|---|
| `term bank_id = "IDBI"` | no hits |
| `term bank_id = "idbi"` | hits |
| `term bank_id.keyword = "IDBI"` | hits |
| `term piuid = 101` | hits (numeric, never analysed) |

Only the field is rewritten, never the value: the folder's own assertion
compares the response's `bank_id` against the `values[0]` it sent, and both stay
`IDBI`. The underlying finding stands for the next collection revision — either
the server should target the keyword subfield for a `term` search, or the
collection should use `match`.

Folders are matched leniently — exact name, then the numeric prefix, then a
unique substring — so `"folder": "02"` keeps finding organisation management
when the collection is restructured and the folder is renamed.

Useful per-phase keys:

| Key | Meaning |
|---|---|
| `requests` | glob patterns; only matching requests in the folder run |
| `skip` | glob patterns to exclude |
| `required` | `false` means its failures are reported but do not fail the run |
| `when` | a variable name (or list); the phase is skipped unless one holds an id |
| `enabled` | `false` skips it entirely |
| `collection` | which collection the folder is in, when it is not the default |
| `variables` | names this phase pins, overriding the collection's own block |
| `adaptor` | `"gateway"` runs the gateway adaptor for the phase's own length |
| `request_timeout_ms` | overrides the global timeout, for a folder hitting a hanging endpoint |
| `max_seconds` | wall-clock cap for the whole folder; newman is stopped past it |
| `personas` | re-points a persona at another account for this phase only |

`when` is what keeps teardown quiet: a run that never created a subscription has
no `subscription_id`, and running the delete anyway would send a request built
from an empty variable — which the platform rejects and the collection reports
as a failed assertion, noise that reads exactly like a real defect.

---

## newman

Pinned in `package.json` and installed beside the harness, so a checkout runs
the same runner as the last person. No reporter plugin is needed — the harness
builds its HTML from newman's JSON:

```bash
npm install --prefix complete-test
# or
python3 complete-test/complete_test.py --install-newman
```

A global `newman` on `PATH` is used if there is no local one;
`postman.newman_bin` overrides both.

### Turning newman off

`postman.enabled: false` (or `--set postman.enabled=false`) takes newman out of
the run entirely:

```bash
python3 complete_test.py --set postman.enabled=false
```

The flow itself is left intact — the script phases still create the accounts,
the organisation, the item, the data and the policy, still assert the audit
trail, and still tear it all down. What stands down is everything downstream of
a folder: no working copies are prepared, every `type: postman` phase is
**skipped by name rather than failed** (the run report says which switch did it),
the teardown DELETE folders are skipped with them, and no newman report is
written — a newman report of nothing is worse than none.

The run report and the exported `postman-environment.json` are still written,
because both describe what the run did.

Use it when newman is not installed, when the collections are mid-edit, or when
the question is about the platform rather than about the suite. It cannot be
combined with `--as-shipped`, which has nothing left to run without newman; the
harness says so and exits rather than running a suite of skips.

Two narrower switches, for when the answer is not all-or-nothing:

| Switch | Scope |
|---|---|
| `"enabled": false` on a phase | one folder, skipped |
| `postman.collections.<key>.enabled` | one collection — but its phases then **fail** with "names collection …, which is not enabled". It controls which collections are prepared, not which phases run, so it is not a way to skip them. |
