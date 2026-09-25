# temporary/ — the KT demo setup (2026-09-10)

The restructured ControlPlane collection is not finished, so this directory
runs the walkthrough on the cut-down one instead:
`resource/Controlplane_temporary_check_1/` — 24 requests over 6 folders, the
whole onboarding story end to end. The data plane runs exactly as it does
today; nothing about it changed.

**This is a diff, not a fork.** `config.kt.json` starts with
`"extends": "../config.json"`, so the deployment, the credentials and the
resource servers stay in one place. `config.json` was not touched. When the
real collection lands, delete this directory and the settled setup is back —
nothing else has to be undone.

```bash
python3 temporary/kt_demo.py                 # the whole demo
python3 temporary/kt_demo.py --list          # what it would run, without running it
python3 temporary/kt_demo.py --only '00 users,01'   # accounts + the ControlPlane collection
python3 temporary/kt_demo.py --rebuild-reports   # re-render the reports from the last run
```

Every `complete_test.py` flag works here — they are passed straight through.
Running `python3 complete_test.py temporary/config.kt.json` does the same thing;
the wrapper adds a preflight check on the collection files and the reporter.

`--only` selects phases by text, and `00 users` has to be in the selection —
the collection signs in as accounts that phase creates.

**Only one run at a time.** The harness locks its work directory; a second run
refuses and says which pid holds it. Two runs in one directory overwrite each
other's Postman environment between phases and produce failures that are not
real — that is exactly what happened once while this was being built, and the
lock is why it cannot happen quietly again.

## What runs, in order

| # | Phase | What it is |
|---|---|---|
| 1 | `00 users` | script — creates this run's Keycloak accounts. No collection can do this. |
| 2 | `01 controlplane onboarding e2e` | **the collection, folders 00-05, one newman process** — sign in, organisation, item (created, indexed, **published ACTIVE**, confirmed visible), access request, audit, teardown |
| 3 | `02 org roles` | script — confirms the collection's approval reached Keycloak |
| 4 | `08 item` … `21 resource servers` | script — the item, policy, published data and access checks the data plane needs |
| 5 | `24`–`32` | the data-plane collection: NGSI-LD temporal/entities/latest/search/download, gateway searches |
| 6 | `33`–`36` | the file server, OGC, the community layer (health + both public reads) and the sandbox (health + a refusal) |
| 7 | `22 audit trail` | script — the audit rows the run produced |
| — | teardown | data-plane cleanup, then the script sweep |

Phase 2 is one phase rather than six because this collection's chain runs
*between* its folders: folder 00 signs in and folder 01 refreshes that token
after approval. Each phase is its own newman process and only uuid-valued
collection variables survive one ending, so split into six the refresh would
have nothing to refresh. Run whole, it behaves exactly as pressing Run in
Postman does.

## Keeping the artefacts, and what teardown removes

Two switches, both now written out in `config.kt.json` so they are visible
rather than inherited:

```json
"run":     { "cleanup": true, "verify_cleanup": true },
"postman": { "skip_delete_requests": false,
             "destructive_requests": ["*Deactivate*", "*Revoke*", "*Delete*"] }
```

- `run.cleanup: false` — **the script teardown does not run**: the accounts, the
  organisation, the item, the published data and the OGC/file/broker objects all
  stay. `verify_cleanup` is only consulted when cleanup is on.
- `postman.skip_delete_requests: true` — holds back the collection's *own*
  deletes, which run inside the collection phase and would otherwise remove the
  item however cleanup is set. `destructive_requests` is what "own deletes"
  means: the shipped list names the control plane's deactivate/revoke requests,
  and `*Delete*` was added for this collection's folder 05.

To keep everything for a walk through the UI:

```bash
python3 temporary/kt_demo.py --set run.cleanup=false --set postman.skip_delete_requests=true
```

Worth knowing: with the deletes held back, folder 05's *Verify access is
revoked* has nothing to verify and will fail — the policy it expects to be gone
is still there. That failure is the switch working, not a defect.

**What teardown reaps when it does run** — data-plane objects first, because the
file server, the STAC store and the OGC tables all refuse to talk about a
databank whose catalogue item has been deleted:

| | |
|---|---|
| NGSI-LD | the item's RabbitMQ exchange and its broker user |
| gateway | the item's queue and its broker user |
| **file server** | the uploaded object — `postman-upload.csv` from the databank |
| **OGC raster / STAC** | every item in the collection (3 last run) |
| **OGC vector** | the `ri_details`, `collections_enclosure` and `collections_details` rows |
| **OGC S3** | the staged objects under the item's prefix |
| then | policy, catalogue item, organisation, Keycloak accounts, audit rows |

Each is its own `*_delete` / `*_cleanup` section in `config.json` and each has
its own `enabled` flag, so one server's teardown can be turned off without
touching the rest. `verify_cleanup` then re-reads the platform and prints
`SURVIVED:` for anything still standing — the last full run ended
`nothing survived`.

## The other four servers

Folder `06 - Other servers` in the collection, run as four phases of its own so
each lands in its own per-server report. One call each at the file server and
OGC; the community layer and the sandbox get the ones `main/e2e.py` makes:

| Phase | Call | Asserts | Runs when |
|---|---|---|---|
| `33 file server read` | `GET {{files_server_host}}/databanks/{{item_id}}/download` as the granted consumer | 200, and a non-empty body | `resource_servers.file.enabled` |
| `34 ogc stac read` | `GET {{ogc_host}}/stac/collections/{{item_id}}/items` | 200, at least one feature | `resource_servers.ogc.enabled` |
| `35 community layer` | `GET {{community_url}}/healthz` | 200, and **every dependency it reports** is up — the route runs `SELECT 1` against Postgres and `head_bucket` against S3, so a failure says which one is down | `community.enabled` |
| `35 community layer` | `GET {{community_url}}/discussion/tags/popular`, **no** Authorization header | 200, the envelope's `success` is not `false`, and the tags come back as a list | `community.enabled` |
| `35 community layer` | `GET {{community_url}}/challenge/all`, **no** Authorization header | 200, `success` not `false`, and `data.competitions` is a list | `community.enabled` |
| `36 sandbox` | `GET {{sandbox_url}}/v1/health` | 200, and the body says `status: ok` | `sandbox.enabled` |
| `36 sandbox` | `GET {{sandbox_url}}/v1/notebook/list`, **no** Authorization header | 401/403 | `sandbox.enabled` |

Each is guarded by `"when": "config:<section>.enabled"`, so a deployment
without one of these skips that phase and the report says why rather than
failing. The hosts come from config — `resource_servers.<key>.url` for the two
servers, and the `sandbox` / `community` sections for the two services that are
checked rather than onboarded to (they have no per-item route, which is why they
are not resource servers). Turn one off with
`--set community.enabled=false` and its phase disappears from the run.

The file and OGC reads address **this run's own item**, so they sit after
`20 data onboarding` — the file has to be uploaded and the raster published
before there is anything to read. They ask for the id explicitly with
`"variables": {"item_id": "{{item_id}}"}`, because the collection otherwise owns
that name (see below).

The two sandbox calls are a pair, and neither is worth much alone.
`/v1/health` says the process is up and serving, and its body is asserted rather
than its status — the handler is a literal `{"status":"ok","version":…}`, so a
200 carrying `degraded` is a pass by status and a failure in fact. But that
route is registered *outside* the auth middleware, so it answers 200 even on a
build whose auth is broken or absent; only the unauthenticated call to a real
API route shows the middleware is there. Both are anonymous, both only read: the
sandbox's heavy provisioning — namespace, Kubeflow profile, 50Gi PVC — happens
on notebook *create*, which nothing here calls.

The two community reads are the public half of the phase-08 check in
`script/community-layer/community_check.py`, as requests the collection owns.
`/healthz` says the service can reach Postgres and S3; it says nothing about
whether routing works. The two reads do: both are `http_bearer_header_public`
routes, so they serve real data to an anonymous caller, and a 200 there proves
the product's router is mounted and reading its own database. `src/main.py`
mounts the Discussion and Challenge routers **only** when they appear in
`ACTIVATED_SERVICES`, so a deployment running one of them answers **404** on the
other's read — not 401. Both are on for dev; on a deployment that runs only one,
drop the other request from the phase's `requests` list rather than reading the
404 as a defect. `main/e2e.py` also calls a protected route per product without
a token and asserts the 401; that half is not here, because this phase is a
demo of the services being up rather than a check that they are guarded.

Both carry **no** `Authorization` header on purpose, and that is not
incidental: `HttpBearerHeader.__call__` inserts a `users` row on every
*authenticated* request and no route deletes one, so a token here would leave a
row per run that nothing sweeps. The public bearer returns early with
`user_id=None`, so an anonymous call never reaches that code.

**Nothing here needs tearing down**: every call here is a read. The file and OGC reads
address the item the script phases created, and the existing teardown already
owns that; the community layer inserts a `users` row on every *authenticated*
request, which is exactly why this call carries no token.

## Who owns which value

The harness hands the collection the accounts and the hosts. The collection
produces everything else itself, and `own_variables` in the config is what
keeps the harness from answering for those names — Postman resolves the
environment before the collection, so a value seeded there would silently
shadow what a pre-request script just stored, and the folder that compares the
response against its own stored value would fail on a mismatch nobody caused.

| The harness provides | The collection produces |
|---|---|
| `base_url`, `acl_url`, `kc_url`, `realm`, `client_id` | `org_name`, `org_manager_email` (namespaced, so teardown still sweeps them) |
| `requester_user` / `requester_pass` — this run's fresh account, with no organisation, which is exactly what folder 01 needs | `org_id`, `org_request_id` |
| `consumer_user` / `consumer_pass`, `cosadmin_user` / `cosadmin_pass` | `item_id`, `item_name` |
| `consumer_token`, `cosadmin_token` — minted fresh at the phase | `requester_token` and its refresh token: folder 01 refreshes it to pick up `org_admin`/`provider`, and a token seeded from outside would shadow the refreshed one |
| the page/size/sort and audit-retry settings the collection's env file does not carry | `access_request_id`, `policy_id` |

`namespace_names` is **off** here. It prefixes the literal `name` in a request
body, and in this collection that literal is `{{org_name}}` — stamping it would
produce a name the collection's own assertion could not match. The run
namespace reaches the names another way: `org_name_prefix` and
`item_name_prefix` are pinned to `{{run_namespace}}-…`, so what the collection
creates is still swept by prefix afterwards.

## Reports

`reports/kt/` (kept apart from the settled `reports/`, so neither overwrites
the other):

- `complete-test-report.html` — the run: phases, artefacts, gaps, cleanup.
- `newman-report.html` — every folder in run order, with the verdicts.
- `newman-report-controlplane.html`, `-ngsild.html`, `-gateway.html` — per server.
- `newman-html/*.html` — **newman's own report, one per phase**: each request
  with its headers, its body and the answer beside it, plus the collection's
  console output. This is the one to open in the walkthrough.

The last of those is on because `postman.newman_html_report` is `true` in the
demo config. Two things are kept out of it deliberately: the `Authorization`
header on every request, and the request *and* response bodies of the four
sign-in requests — a login posts a password and answers with a token, and no
header filter helps there. The files are still written owner-only: they carry
real request and response bodies, so read one before forwarding it.

## What the first dev run exposed — now fixed in the collection

First run, 2026-09-10: 24 of 44 assertions failed. Three defects, all in the
collection, **all fixed in
`resource/Controlplane_temporary_check_1/` itself** rather than papered over
here:

1. **`sort=createdAt` is not a valid sort.** The API answers
   `400 Invalid sort format: createdAt. Expected field:order`, which broke
   *List pending org requests* and all four auditing requests — and the
   auditing ones then spent their whole retry budget on a 400 that could never
   become a 200 (120s for the folder). The environment file now ships
   `sort=createdAt:desc`, and `audit_sort` beside it.
2. **The item body carried no `apdURL` and no `mediaURL`.** An item created
   without `apdURL` can never be accessed under `RESTRICTED` — the access check
   reads the APD off the item document, not from the deployment's config — so
   every ACL request answered `403 Item not found for ID: …`, and the teardown's
   "delete is blocked by an active policy" got a 200 because there was no policy
   to block it. Both fields are now in the body: `{{apdURL}}`, which the harness
   fills from `config.acl.apd_url` so it follows the deployment, and
   `{{item_media_url}}`, new in the environment file — an empty `mediaURL`
   leaves `dataUploadStatus` false.
3. **The catalogue's read and write paths disagree about whether an item
   exists.** Two symptoms of one cause. *Get catalogue item* fired straight
   after the create got `404 doc doesn't exist`; and the publish PATCH — after
   a read had already found the item — got `404 Item not found for update`,
   because `fetchForWrite` runs its own search and the two take different
   routes into Elasticsearch. It killed a whole run once through the script
   phase, and the collection's own publish once.

   All three requests now retry, bounded, the way the auditing folder already
   did: the read until it is indexed, the publish on 404 only, and the
   read-back until `publishStatus` says ACTIVE (the first read after a
   successful patch still said `PENDING`). `flow._publish_item` does the same
   for the script phase — 5 attempts, 404 only, anything else raises at once.
   Retrying the write costs only the run that hits the race; waiting longer up
   front would cost every run.

   *(was: read back before the catalogue had indexed it)*

The environment file also gained the six names the requests and scripts read but
it never declared: `audit_size`, `audit_sort`, `audit_retry_max`,
`audit_retry_delay_ms`, `policy_status_filter`, `org_claim_mapped` (plus the two
new `cat_retry_*`).

**One workaround is left here on purpose**: `org_emp_id` is pinned in the config.
The environment file declares it, but the harness drops every shipped variable
whose name reads like an id — that is the rule that stops a collection's DELETE
firing at a real record, and it is worth more than this one field.

Folder 02 also gained the two requests the UI needs: **PATCH publishStatus
ACTIVE** (only a COS Admin may set it) and a read-back asserting
`publishStatus == ACTIVE` **and** `dataUploadStatus == true` — the second comes
from the non-empty `mediaURL`, so an item missing it stays invisible however
published it is.

Result: **28 requests, 50 assertions, 0 failed** in the collection phase.

## What was left out, and why

- **`23 data plane token`** and **`28 ngsi-ld app id auth`** are not in the
  phase list. They need the client and the application that ControlPlane
  folders 13 and 14 register, and those folders are not in this collection.
  They are the two the data plane was already known to fail on; leaving them in
  would fail for a reason that is not the demo's subject. They come back with
  the full collection.
- **The Postman teardown entries** are gone from this config for the same
  reason — they name folders of a collection that is not enabled. This
  collection tears down what it created in its own folder 05: delete blocked by
  an active policy (409), delete the policy, prove access is gone, delete the
  item. The script sweep still runs behind it for the accounts and the item the
  script phases made.

## Going back

Delete `complete-test/temporary/`. `config.json` still names the phase 3
ControlPlane collection and the full 37-phase list; nothing was changed there.
The three mechanisms this demo needed are general and stay in the harness,
switched off by default:

- a postman phase may omit `folder` and run the whole collection in one process;
- `postman.collections.<key>.own_variables` — names the harness must not seed;
- `postman.newman_html_report` — newman's own per-phase HTML report.
