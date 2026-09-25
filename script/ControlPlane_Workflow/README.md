# ControlPlane onboarding E2E harness

Drives the full onboarding workflow against any deployment and removes
everything it created afterwards.

```
00  Sign in            requester, cos admin, consumer, consumer-no-policy
01  Org onboarding     submit -> list -> approve -> refresh token -> confirm membership
02  Catalogue          create item (mediaURL + RS array) -> publish ACTIVE (COS)
03  Access request     consumer request -> org admin approve -> policy
04  Data onboarding    NGSI-LD -> exchange | OGC vector -> collection
05  Resource servers   mint RS token -> GET per server (200) | no-policy (403)
06  Auditing           consumer / cos admin / org admin trails
07  Sandbox            health + unauthenticated refusal (no lane, no item)
08  Community          healthz + per-product public read / refusal
    Teardown           NGSI-LD broker objects, policy, item, consumers, org,
                       Keycloak users, DB sweep
```

Phases 01 to 03 and 05 run once per **lane** — a provider, their organisation
and the one item they own. A run has one lane, and two when NGSI-LD and the
gateway are both enabled; see "Two providers" below.

## Setup

Layout:

```
main/e2e.py                      the entry point — this is what you run
main/config.json                 your deployment (gitignored; holds credentials)
main/config.example.json         the committed template
main/reports/                    HTML reports land here
script/ControlPlane_Workflow/    the harness itself, imported as a package
script/sandbox/                  phase 07's checks (sandbox_check.py)
script/community-layer/          phase 08's checks (community_check.py)
```

The last two are servers this harness *checks* rather than drives, and they are
separate deployments with their own repositories, so they live beside the other
per-server automation instead of inside this package. `__init__.py` puts their
directories on the import path — the directory names are then free of the
constraint Python puts on module names, which `community-layer` needs, since a
hyphen cannot appear in an importable package name.

The harness reads `config.json` beside the entry point. It is gitignored, because a
working one holds live credentials. Copy the committed template and fill it in:

```bash
cp main/config.example.json main/config.json
make e2e-config                      # resolve and print, secrets masked
```

Requires `requests`, plus `psycopg2` when `postgres.enabled` is set.

## Running

```bash
make e2e                                   # full run
make e2e-sweep                             # reap old orphans, no flow
make e2e-config                            # print the resolved config, masked
```

`ARGS` passes any flag through, `CONFIG` points at another deployment:

```bash
make e2e ARGS="--report run.html"          # write a report here
make e2e ARGS="--set run.cleanup=false"    # keep artefacts
make e2e ARGS="--only 02"                  # one phase
make e2e CONFIG=main/staging.json   # a different deployment
```

Or call the script directly, which is the same thing:

```bash
python3 main/e2e.py [config-file] [--report FILE] [--set PATH=VALUE]
                               [--only PHASE] [--sweep-only]
```

Exit code is 0 only when every phase passed *and* nothing survived teardown.

The optional positional argument is a path to a config file, used instead of
`config.json`. A bare name resolves to `<name>.json` beside the script, so
`e2e.py staging` and `e2e.py main/staging.json` are the same
thing.

`--report` writes a self-contained HTML page listing every request, status and
error body — the shareable artefact for teammates who don't run Python. Set
`run.report_dir` to write one on every run without passing the flag.

`run.report_file` picks the naming. Set it (`"e2e-report.html"`) and every run
overwrites that one file, which is what you want while iterating — the report is
always at the same path, so a browser refresh is the whole workflow. Leave it
null and each report is named `<namespace>.html` instead, so runs never
overwrite one another and any report can be matched back to the artefacts it
describes; that is the one to use on a schedule, where yesterday's failure is
the thing you need to read.

### Credentials never leave the run

`Recorder.add` keeps what a call *was* — phase, label, method, URL, status,
timing — and discards what it *carried*: request, response, and the excerpt of a
failed response that used to travel as `detail`. A report is a file that gets
shared, and bodies are where credentials, tokens and generated configs live.

Dropping at capture time is an absence rather than a filter: no rendering path
can surface a body later, and no unanticipated shape can slip past redaction. A
run's terminal output is unaffected — phases print each response as they read
it, and a failure still raises with the body that caused it.

What is recorded is redacted before anything is written — reports, logs and
terminal output alike:

* every credential in the resolved config is registered at startup and masked
  wherever it appears — a script echoing its own INI, a password in a URL, an
  error body quoting a connection string
* patterns cover what the config never sees: JWTs, `X-Amz-Signature` /
  `X-Amz-Credential`, `AKIA…` key ids, and `password=` / `secret:` pairs in
  free text
* structured payloads are additionally scrubbed by key, as before

Identifiers are deliberately *not* redacted. Item, organisation, user and
request ids are per-run and disposable, and a report without them cannot be
matched to the artefacts it describes.

Keys that merely look secret are excluded: `token_user` names which account to
act as, `delete_user` whose broker user to remove — masking those values would
blank ordinary words wherever they occurred.

## Configuration

Everything about a deployment lives in one config file. Nothing is hardcoded in
the harness, so pointing at a different stack is a config change and nothing
else. The file states only what differs from the built-in defaults in
`config.py`; anything omitted falls back to those.

### Precedence

```
--set flag  >  environment variable  >  config file  >  built-in default
```

### Two different env-var prefixes

These are easy to confuse, so they are deliberately distinct:

| Prefix | Purpose |
|---|---|
| `E2E_*` | secret **values** the config pulls in via `${E2E_PG_PASS}` |
| `DXE2E_*` | **overrides** addressing a config key by path |

An override addresses a key by its path, upper-cased, joined with a double
underscore:

```bash
DXE2E_KEYCLOAK__URL=https://kc.example.com
DXE2E_RESOURCE_SERVERS__OGC__URL=ogc.example.com
DXE2E_RUN__PREFIX=nightly
DXE2E_RUN__CLEANUP=false
```

Unknown paths are rejected rather than silently ignored, so a typo fails the run
instead of quietly using the default. The same holds for `--set`.

### Secrets

`config.example.json` is committed and contains no secrets. `config.json` is
gitignored, so it *may* hold them directly — but referencing the environment
keeps it shareable:

```bash
export E2E_KC_ADMIN_SECRET=...     # Keycloak admin client secret
export E2E_COS_ADMIN_USER=...      # optional, see cos_admin below
export E2E_COS_ADMIN_PASS=...
export E2E_PG_PASS=...             # only if postgres.enabled
export E2E_KIBANA_USER=...         # only if ngsild_delete.enabled
export E2E_KIBANA_PASS=...
export E2E_OGC_BUCKET=... E2E_OGC_REGION=...   # only if ogc_vector.enabled
export E2E_OGC_DB_PASS=...        # only if ogc_vector_delete.enabled
export E2E_RMQ_PASS=...            # databroker.enabled, and phase 04 publishing
```

Values may reference the environment as `${VAR}` or `${VAR:-fallback}`, resolved
at load time. A missing `${VAR}` with no fallback fails the run — including in a
section that is disabled, because interpolation runs over the whole tree before
anything looks at `enabled`. So write optional secrets as `${E2E_PG_PASS:-}`,
which is what the template does, and reserve the bare form for values a run
genuinely cannot proceed without.

The Keycloak admin client is the deployment's own — `keycloakAdminClientId` /
`keycloakAdminClientSecret` from its `config.json`. Don't mint a second
privileged credential for testing.

### Config sections

| Section | What it covers |
|---|---|
| `control_plane` | ControlPlane base URL and request timeout |
| `acl` | Policy/ACL server; `base_url` defaults to `control_plane.base_url`, `apd_url` to that with the scheme stripped |
| `keycloak` | URL, realm, admin client (user provisioning), user client (token minting), role names |
| `cos_admin` | Optional existing platform admin to borrow — see below |
| `resource_servers` | The four deployed servers: `ngsild`, `gateway`, `ogc`, `file` — with `verify_path` and `expect_denied` per server |
| `databroker` | RabbitMQ, for observing audit publication |
| `ngsild_publish` | Publishing NGSI-LD records into the item's exchange before the data plane is read |
| `gateway_adaptor` | The RPC consumer that answers gateway reads, run for the length of phase 05 |
| `ngsild_delete` | Tearing down the item's exchange, Elasticsearch index and broker user |
| `gateway_delete` | Tearing down a gateway item's queue and broker user |
| `ogc_postgres` | The OGC server's own database, schema and credentials — shared by every OGC teardown |
| `ogc_vector` | Onboarding a GeoPackage as an OGC vector collection |
| `ogc_vector_delete` | Tearing down that collection's rows and table |
| `ogc_raster` | Ingesting a STAC collection and uploading its GeoTIFFs |
| `ogc_raster_delete` | Deleting the STAC items again |
| `sandbox` | The sandbox (notebook/compute) server — checked, never onboarded to |
| `community` | The community layer — discussion and challenge, checked, never written to |
| `postgres` | Residue sweep and row assertions |
| `run` | Prefix, cleanup toggles, timeouts, report directory and filename |

Resource servers are assumed already deployed — the harness never registers or
deletes them. Each needs a `verify_path`: a GET that a granted token must be able
to read and an ungranted token must not. Set `enabled: false` to skip one; at
least one must stay enabled, since the catalogue item's schema requires a
non-empty `resourceServer` array.

Servers disagree on how an item is addressed, so `verify_path` supports both
forms:

| Form | Fetched as | Used by |
|---|---|---|
| `/ngsi-ld/v1/entities` | `…/ngsi-ld/v1/entities?id=<item>` | NGSI-LD, gateway |
| `/collections/{item_id}` | `…/collections/<item>` | OGC |

A path containing `{item_id}` has it substituted and no `id` parameter is sent —
the path already says which item is wanted.

`expect_denied` is what the no-policy token must get back: `[401, 403]` for an
access-controlled path, or `[]` for one that is not. OGC's
`/collections/<item>` is the collection *description* — public metadata by
design, answering 200 to any token — so asserting a refusal on it would assert
something untrue. It is set to `[]`, the refusal check is skipped for that
server, and the run says so rather than passing quietly. Those paths get one extra check: a
200 whose body never mentions the item id fails the phase, because
`/collections/<item>` answering with an empty envelope or somebody else's
collection is a pass by status and a failure in fact.

To exercise ControlPlane and ACL standalone, set `run.verify_resource_servers`
to false. Onboarding, catalogue, access request and policy all still run and are
verified, and no data-plane server is contacted.

Phase 05 prints what each server returned — status, elapsed ms, a one-line shape
summary (`2 record(s)`, `0 record(s)`, `keys: title, type`) and the body itself,
scrubbed and truncated to `run.response_preview_chars` (0 for the summary only).
Every server answers in its own shape, and a 200 with zero records is what an
unpublished item looks like, so the status alone does not tell you whether a
read worked. As more servers are added, this is what makes their differences
visible without reading the HTML report.

### Phase 04, data onboarding

The catalogue creates one RabbitMQ exchange per NGSI-LD item, named with the
item id, and the resource server serves what was published there. Phase 04
publishes into that exchange so the reads in phase 05 mean something: with no
data published, a broken policy and a working one both return nothing.

It does not reimplement publishing — it runs
`script/NGSILD_Automation_Script/ngsild_publish_v1.py`, the same script that is
run by hand, writing that script a temporary INI with `exchange_name` set to the
item id and deleting it afterwards, since it carries the broker password. The
script's exit codes are what the phase branches on: 0 published and confirmed,
1 publish failed, 2 bad configuration.

`host`, `username` and `password` fall back to the `databroker` section when
left null — it is the same broker with the same admin credentials. The port and
vhost do differ: this is the AMQPS data plane (`24567` / `IUDX-V2`), not the
management API on the internal vhost. Point `data_file` at a JSON array to
publish real records instead of the script's sample data, `count` to repeat
them, and set `enabled: false` to skip the phase entirely.

The section is named for the server it publishes to, so the other resource
servers can grow one of their own without the keys colliding.

The phase skips itself two ways. `ngsild_publish.enabled: false` turns it off
outright. And an item that declares no NGSI-LD resource server — every
`resource_servers` entry of type `ngsi-ld` disabled, say an OGC-only run — has
no exchange to publish to, so the phase stands down instead of failing, and the
`ngsild_publish` section is not validated at all. Either way the skip is
reported in the run summary, so a quiet no-op cannot be mistaken for a pass.

That second check reads the item back rather than trusting the request. Phase 02
already GETs the created item, so it keeps the `resourceServer` types the
catalogue returned, and phase 04 decides from those. On a healthy deployment
they match the config exactly — the item is built from it. Where they diverge,
the item wins and the divergence is logged, because the exchange was created
from what the catalogue stored, not from what the harness asked for. If the
response carries no `resourceServer` at all, the config decides, and the run
summary says which source the decision came from.

Paths in this section (`script`, `cert_path`, `data_file`) resolve against the
`e2e` directory and then the repository root, so they do not depend on where the
harness is run from. The publisher needs `pika` installed.

### The gateway adaptor (`gateway_adaptor`)

The gateway resource server does not serve from storage. It publishes the
request onto a queue named with the item id and waits for a reply on the
message's `reply_to`. Nothing answers unless something is consuming that queue,
so a gateway read against a deployment with no consumer is never replied to and
the client eventually times out — which reads as a slow server rather than a
missing component.

`script/GATEWAY_Automation_Script/gateway.py` is that consumer. When the item is
a gateway one, phase 05 starts it against the item's queue, waits
`startup_seconds` (2) for it to attach, makes the calls, and stops it in a
`finally` — it never outlives the phase. If it dies before it can consume, the
phase fails immediately with its log rather than minutes later as a timeout
naming the wrong culprit.

Note the vhost: the adaptor consumes on `IUDX-V2-INTERNAL`, not the data-plane
vhost `ngsild_publish` writes to. `host`, `username` and `password` fall back to
`ngsild_publish` and then `databroker`. `queue_name` defaults to the item id.
`api_url` is the upstream the adaptor answers with — the script's own dummy API
by default, since the point is that the gateway path carries a reply, not what
the reply contains.

### OGC vector (`ogc_vector`, `ogc_vector_delete`)

`resource_servers.ogc.raster` is the switch:

```json
"ogc": { "raster": true,  "enabled": true }   →  STAC / raster pipeline
"ogc": { "raster": false, "enabled": true }   →  vector pipeline
```

It derives `query_types` (`["STAC"]` or `["FEATURE"]`), so the item's declared
type, the onboarding path, the read path and the teardown all move together and
cannot disagree. Set it to `null` to write `query_types` by hand instead.

An OGC item is not served by anything the harness can publish to. A GeoPackage
has to be uploaded and onboarded through the OGC processes API: a pre-signed S3
URL is requested, the file is PUT to it, a collection-onboarding process is
triggered, and its job is polled until it reports `SUCCESSFUL`. Until then the
collection does not exist and `/collections/<item>` has nothing to answer with.

Neither the creation nor the deletion script reports failure through its exit
code — creation logs errors and exits 0 whatever happened, deletion returns
normally after a rolled-back transaction — so the verdict is read out of their
output: `Status: SUCCESSFUL` for creation, `Cleanup completed successfully` for
deletion. A run that produced no evidence of success is treated as a failure.
Creation also writes its own timestamped log file, which is collected into the
harness output so the errors that only appear there are part of the report.

Their configs are JSON and neither takes a `--config` flag. Creation reads
`./config.json` from its working directory; deletion reads `db_config.json` from
*its own* directory, so it runs as a copy inside the temporary run directory
rather than having database credentials written into the checkout.

**The organisation name has to be short.** The OGC record table stores provider
contacts as `{"providerOrg":"<name>","additionalInfoURL":"<org uuid>"}` in a
`varchar(100)` column — 40 characters of JSON plus a 36-character uuid leave 24
for the name. A longer one fails onboarding with `22001 value too long`, which
the process reports only as "Failed to onboard the collection in db.". The
harness caps the org name at 24 characters and rejects a `run.prefix` over 14
when an OGC vector server is enabled.

`ogc_vector` needs `gpkg_path`, `bucket_name` and `region`, which have no
sensible defaults. `base_url` falls back to `resource_servers.ogc`, and the two
process ids the API is driven through are constants in `ogc_vector.py` — they do
not vary by deployment, so they are not config anybody has to fill in.

`ensure_provider_role` adds a `roles` row for the run's provider in the OGC
database before onboarding, because `ri_details.role_id` is a foreign key onto
it and a provider created minutes ago has none. Teardown removes only what the
run inserted.

### OGC raster / STAC (`ogc_raster`, `ogc_raster_delete`)

With `raster: true`, phase 04 runs two scripts in order:

1. `Raster_Automation/Creation/stac_injestion.py` — reads the GeoTIFFs, builds a
   STAC Collection and one Item per file, and POSTs both to `/stac`.
2. `Raster_Automation/S3Upload/S3.py` — uploads the GeoTIFFs the items point at.

with `ingest_settle_seconds` (5) between them and `upload_settle_seconds` (15)
before phase 05 reads. Both halves are required: the items describe assets that
do not exist until the upload lands, and the upload is meaningless without them.

**The staging detail that makes it work.** The STAC asset href is
`<collection id>/<file>`, while the uploader keys objects as
`<directory name>/<file>` — they agree only when the directory is named after
the collection. Each run symlinks `tif_dir`'s rasters into a directory named
with the item id, so neither script needs changing and large GeoTIFFs are not
copied per run.

Phase 05 reads a raster item through `stac_verify_path`
(`/stac/collections/<item>/items`) instead of `verify_path`. Both OGC read paths
answer 200 to any token, so `expect_denied` and `stac_expect_denied` are both
empty: on OGC, phase 05 proves the data is served, not that it is protected, and
the result line says `denied:n/a (public path)` rather than claiming otherwise.

Teardown runs `Raster_Automation/Deletion/stac_deletion.py` first, deleting every
item in the collection, before the catalogue item that names it. The collection's
own database rows are keyed by the same id and go with the `ogc_vector_delete`
pass that follows, so that step covers either kind of OGC item.

`aws_access_key_id` and `aws_secret_access_key` are required — the uploader
writes to the bucket directly rather than through a pre-signed URL. Bucket and
region fall back to `ogc_vector`.

### S3 cleanup (`ogc_s3_cleanup`)

The OGC teardowns remove the platform's own state — collection rows, STAC items,
the ogr2ogr table — but the uploaded files stay in the bucket: `<item id>.gpkg`
for a vector item, an `<item id>/` folder of GeoTIFFs for a raster one. Teardown
runs `Extra_To_Clean_S3/s3_deleteion.py` once per item, before the catalogue
item that names them is deleted, passing `gpkg` or `tif` according to which kind
the item is.

`--confirm` is always passed: without it the script waits on stdin for a typed
"DELETE" and a run would hang forever. It exits 0 whether or not anything was
deleted, so the verdict comes from its summary — a non-zero `Errors:` count
fails the step, while "nothing found" is treated as success, since a run whose
upload failed has nothing in the bucket to tidy.

Bucket, region and credentials fall back to `ogc_raster` and then `ogc_vector` —
the sections that put the files there — so there is nothing to configure unless
the bucket differs.

### The OGC database (`ogc_postgres`)

The OGC server keeps its collections in its own database, not the ControlPlane
one — which is why a teardown pointed at `postgres` finds nothing to delete.
`ogc_postgres` describes that connection once and every OGC teardown, vector and
raster alike, is handed the same one: `database` defaults to `ogc_rs_v2`, the
database `vector_deletion.py` points at, while host, port and credentials fall
back to the `postgres` section.

Collections onboarded by the harness are titled `<namespace> <label>`, taking
`label` from `ogc_vector` and `title` from `ogc_raster`, so the sweep has
something to match on; see *The OGC database is swept separately*.

`schema` is optional and null by default — the tables sit in the connection's
default search_path. Set it only for a deployment that keeps them elsewhere: the
deletion scripts issue unqualified SQL and their config has no schema field, so
a schema named here is applied as `search_path` through `PGOPTIONS` rather than
by rewriting their statements.

### File server (`file_upload`, `file_delete`)

A catalogue item *is* a databank — `adex:DataBank` is the type the harness
creates — and the Files Connect API stores objects under it. A file item has an
empty databank until something is uploaded, so phase 04 runs
`FILE_Automation_Script/creation/file_creation.py`: a multipart upload to
`databanks/<item>/uploads`, followed by the zip/report processing job. With
`processing_wait` on (the default) it waits for that job to reach a terminal
state, so a green run means the file is usable rather than merely accepted.

Teardown runs `deletion/file_deletion.py` before the item, removing exactly the
keys this run uploaded. If the run failed before anything was uploaded it falls
back to the configured key, so a crashed run still cleans up. `--sweep-only`
applies the same to orphans.

These two scripts are the best-behaved of the set: they take their config path
positionally and report success through their exit code, so the harness reads
that rather than scraping output. Both accept a static bearer token, and the
run's provider already holds one — no second credential is configured.

`base_url` falls back to `resource_servers.file`, `key` to the file's own name,
and `content_type` is guessed by the script when null. `file_path` defaults to
the small CSV beside the creation script.

### The sandbox server (`sandbox`)

Not a resource server, and deliberately not modelled as one. Three facts about
`datakaveri/sandbox-connect-api` decide that:

* **No route takes an item id.** Everything is `/v1/notebook/...`,
  `/v1/bookings/...`, `/v1/profile/create`. `verify_path` in this harness is
  item-addressed by construction — `?id=<item>` or `{item_id}` in the path — and
  there is nothing here for it to address.
* **`authMiddleware` wants an identity token.** It reads the JWT's `azp` and
  refuses anything whose authorized party is not `API_KEYCLOAK_CLIENT_ID`, then
  requires `email_verified`, then `kyc_verified` when `API_KYC_ENABLED` is set.
  The item-scoped token phase 05 mints is issued by ControlPlane, not by that
  client, so a "granted" read could never come back 200.
* **It leaves nothing behind.** No exchange, no collection, no databank — so no
  teardown branch has anything to remove, and the phase creates nothing.

Put it in `resource_servers` and every catalogue item would declare a `sandbox`
entry the catalogue has no handling for, on the way to a read that cannot
succeed. So it is its own section and its own phase, running last and gating
nothing.

What phase 07 asserts, both without credentials:

| Call | Config key | Asserts |
|---|---|---|
| `GET /v1/health` | `health_path` | 200, and `status` equals `expect_status` (`ok`) |
| `GET /v1/notebook/list` with no `Authorization` | `verify_path` / `expect_denied` | 401 or 403 |

The refusal check is the one that carries weight. `/v1/health` is registered on
the root mux, *outside* `authMiddleware` — by design, so a monitor can reach it —
which means it answers 200 on a build whose auth is broken or missing entirely.
Only an unauthenticated call to a real API route says the middleware is in place.
Pointing `verify_path` at the health route makes the phase fail, which is the
proof that the assertion is live rather than decorative.

The body check matters for the same reason: `expect_status` is compared exactly,
so a 200 carrying `"status": "degraded"` fails instead of passing as "up". Set it
to null to assert the status code alone.

**Both branches, one config.** `cmd/api/router.go` is byte-identical on
`stable/v2.3` and `dev`, so these two routes exist in the same place on either.
The branches differ only in a blocked-email-domain rule inside the middleware
(`stable/v2.3` has it, `dev` does not), and that check sits *after* the token
parse — an unauthenticated request is refused before it, so the 401 is identical
on both. That is what makes this pair safe to assert against either deployment.

**The optional third call: 200 with a token.** Without it the phase proves the
door is locked; with it, that the right key opens it — which is the difference
between "auth is wired up" and "the API works". Two ways to supply the token,
both null by default:

| Config | Token |
|---|---|
| `bearer_token` | pasted in, from whichever realm the sandbox trusts |
| `token_user` | the identity token of a user this run creates (`"consumer"`, …) |

`token_user` needs phase 00, so under `--only "07 sandbox"` it stands down with
a note rather than failing.

Making that read is safe, and it is worth being explicit about why, because the
sandbox is otherwise the heavyweight of the two: `authMiddleware` validates the
token and writes nothing — unlike the community layer's, which inserts a user
row — and `listNotebooks` only reads. Namespace, Kubeflow profile and the 50Gi
PVC are provisioned by notebook *create*, which the harness never calls.

Whether it passes is a property of the deployment: `azp` must equal
`API_KEYCLOAK_CLIENT_ID`, the account must be email-verified, and KYC-verified
when `API_KYC_ENABLED` is on. Each of those fails with its own message, so one
call says which requirement was missed — `TOKEN_HINTS` maps the message to the
cause and the phase reports both it and the server's own words.

**Why there is no create/teardown here.** `POST /v1/notebook/create` provisions
a Kubeflow profile, a namespace named after the caller's Keycloak id, and a 50Gi
CephFS workspace PVC. `DELETE /v1/notebook/delete` removes the notebook and its
own PVC — and none of those three. No route deletes them, on either branch. This
harness creates a throwaway user per run, so a create/teardown phase would leave
an unreachable namespace and PVC behind every time, invisible to the prefix
sweep, to `--sweep-only` and to `run.verify_cleanup`. See 
[../../docs/sandbox.md](../../docs/sandbox.md).

### The community layer (`community`)

`dx-community-layer` carries two products behind one FastAPI process,
Discussion and Challenge, mounted at `/discussion` and `/challenge`. Not a
resource server, for the same three reasons as the sandbox above.

Phase 08 makes five calls, none of them authenticated:

| Call | Config key | Asserts |
|---|---|---|
| `GET /healthz` | `health_path`, `expect_dependencies` | 200, and every dependency reported up |
| `GET <public_path>` per product | `services.<name>.public_path` | 200, body not `success:false` |
| `GET <verify_path>` per product, no auth header | `services.<name>.verify_path` / `expect_denied` | 401 / 403 |

**`/healthz` earns its assertion.** `routes/utility.py` runs `SELECT 1` against
Postgres and `head_bucket` against S3, returning 200 only when both pass and 500
otherwise. The status alone would catch an outage; the harness checks the
per-dependency map anyway, because it names what broke. `expect_dependencies:
null` accepts whatever the service reports, which survives it adding a third.

**Three route kinds, three different failures.** `/healthz` covers dependencies
and says nothing about routing. `public_path` is a token-*optional* route, so it
proves the router is mounted and reading its own database — and says nothing
about authorisation. `verify_path` is a token-*required* route called without
one, and its refusal is the only one of the three that shows the protected
routes are protected. Point `verify_path` at a public route and the phase fails,
which is the proof it is live.

**Each product is mounted independently.** `main.py` includes a router only when
it appears in the deployment's `ACTIVATED_SERVICES`, so a Discussion-only stack
answers **404, not 401**, on every `/challenge` route. `services.<name>.enabled`
mirrors that; at least one must stay on, or the phase is only a health check and
startup rejects it.

**Why nothing here sends a token.** `middlewares/authorization.py` does more
than validate: on every authenticated request `update_user_info` inserts a
`users` row into the discussion and/or challenge database, and no route deletes
a user. With this harness's throwaway user that is one orphan row per run, per
database, keyed to a Keycloak `sub` that no longer exists — unreachable by the
prefix sweep and by `--sweep-only`. An unauthenticated call never reaches that
code, so these checks leave nothing behind. `bearer_token` opts in, and should
name a fixed long-lived account rather than the run's own user. See 
[../../docs/community-layer.md](../../docs/community-layer.md).

### NGSI-LD teardown (`ngsild_delete`)

Onboarding an NGSI-LD item creates more than a catalogue document: an exchange
named with the item id, an Elasticsearch index `iudx-v2__<item id>`, and a
RabbitMQ user for the provider. Deleting the item removes none of them, and
afterwards nothing on the platform names them.

So teardown runs `script/NGSILD_Automation_Script/ngsild_delete_v1.py` **first**,
while the item still exists — index, exchange, user, in that order. It runs only
when the item is an NGSI-LD one, and it is **off by default** because it is the
only part of the harness needing Elasticsearch credentials: Kibana proxies ES,
so `kibana_username` must be able to delete the index.

### Gateway teardown (`gateway_delete`)

A gateway item leaves a queue named with the item id and a broker user for the
provider. Teardown runs `script/GATEWAY_Automation_Script/delete_rmq.py` before
the item is deleted — the queue over AMQP, then the user over the management
API. Everything falls back to `gateway_adaptor`, and `mgmt_url` to
`ngsild_delete`'s, since it is the same management API. Unlike `ngsild_delete`
it needs no Elasticsearch credentials, so it is on by default.

That script reads `./config.ini` from its working directory with the path
hardcoded, so it runs with cwd set to a temporary directory holding the
generated config — the same treatment `gateway.py` gets.

### Two providers (`ngsild` + `gateway`)

Both teardowns above delete "the provider's broker user", and the catalogue
names that user after the **provider's Keycloak id** — not after the item. With
one provider owning an item that declares both servers, both scripts are aimed
at the same user: the NGSI-LD teardown deletes it, and the gateway teardown then
works on a user that no longer exists.

So `flow.gateway_needs_own_provider` splits the run in two whenever both servers
are enabled:

```
requester     -> org A -> item A (ngsild, ogc, file)  -> broker user A
gwrequester   -> org B -> item B (gateway)            -> broker user B
```

A second organisation, not a second member of the first: the org-create approval
is what grants the provider role, and there is no join flow that would put a
second user in an existing org with it.

Each half is a `flow.Lane`, and phases 01, 02, 03 and 05 loop over `ctx.lanes`;
teardown does the same, so each broker teardown deletes the user its own
provider owns. Everything else is unchanged — phase 04 onboards data for the
NGSI-LD/OGC/file item, phase 06 audits the first provider, and the namespace
sweep reaps both lanes because both are named with it.

With only one of the two servers enabled the run has a single lane carrying
every enabled server, exactly as before.

### cos_admin

Two modes, chosen by whether you fill the section in:

```json
"cos_admin": { "username": "", "password": "" }        → the harness creates its own
"cos_admin": { "username": "…", "password": "…" }      → an existing admin is borrowed
```

**Empty** — the harness creates a namespaced `cos_admin`, grants it the realm
role, uses it for org approval and item publishing, and sweeps it with
everything else: the Keycloak account, its database rows, and its audit rows.
Creating one needs realm-admin on `keycloak.admin_client_id`.

**Filled in** — that account is used instead, and everything downstream is
unchanged: org approval, publishing, the admin audit assertions.

Teardown then treats the account and its audit trail as two separate questions.

*The account is never deleted.* Its username is in the protected list, its
Keycloak id is resolved at sign-in and added to the same list, and that id is
kept out of `user_ids` — the set the Keycloak delete and every user-keyed
`DELETE` run on. So the user, its `user_table` row, its credits, its clients and
its policies all survive.

*What it owns is deleted*, when `run.delete_audit_rows` is on. Those rows record
the harness approving its own organisations and publishing its own items — test
residue that happens to carry an administrator's name. They get statements of
their own, keyed on `cos_admin_ids`, and `run.delete_all_cos_admin_data` picks
which set runs:

| `delete_all_cos_admin_data` | What the sweep deletes |
| --- | --- |
| `false` (default) | **Every table, restricted to this run.** 22 statements: each matches the account's id AND the item, organisation or name anchor that makes the row this run's — `policy` on `item_id`, `organization_users` on `organization_id`, the log tables on `asset_id`/`org_id`/namespaced names, and so on. Rows the account owns for its own reasons survive. |
| `true` | **Every table, unrestricted.** 35 statements: the 22 above with the anchors dropped, plus the thirteen tables that have no anchor to scope by — `credit_transactions`, `credit_requests`, `user_credits`, `subscriptions`, `app_constraints`, `app_credentials`, `client_credentials`, `request_messages`, `leaderboard_dirty_queue`, `kyc_transactions`, `access_rule_allowed_user`, `resource_servers`, `acl_servers`. |

`user_table` and the Keycloak account survive in both cases — that is the whole
point of the split. `true` strips the account back to bare otherwise: no
credits, no registered apps, no organisation memberships, no resource servers.

### Owning columns, not every mention

Each statement keys on the column that means *this row belongs to the account* —
usually `user_id`, sometimes `owner_id`, `provider_id` or `requested_by` where
that is the owning column, and both sides for a two-party row like `policy`
(`owner_id`/`consumer_id`) or `request` (`provider_id`/`consumer_id`).

Columns that name the account as the *counterparty* are deliberately not
matched: `user_activity_audit_log.asset_provider_id` is somebody else's action
on an asset this account provides, `credit_transactions.transacted_by` is
another user's transaction the admin moved, `compute_role.approved_by` is
another user's role the admin signed off. Deleting on those removes other
users' rows — collateral damage, not cleanup. Where those rows belong to the
run's own users they are already deleted by that user's own sweep.

This is also what keeps the statements portable: the columns deployments
disagree about (`delegate_id` vs `delegator_id`, `asset_provider_id`) were all
counterparty columns, so dropping them removed the drift at its source rather
than papering over it.

**`true` deletes `resource_servers` and `acl_servers` rows the account owns.**
These are registrations the harness never creates and cannot recreate, and the
deployment resolves against them — deleting them on a shared stack is an outage,
not a cleanup. That is what confines this flag to a staging or throwaway stack
where the cos_admin exists to run this harness and nothing else.

Those twelve unanchored tables are absent from `false` because there is nothing
to scope them by, and the harness never reaches most of them through the
cos_admin anyway: it registers no client for that account, sends no request
message and provisions no credits.

Most of the scoped statements duplicate work the main sweep already does — its
item and org branches match a row whoever owns it. They are written out anyway
so that "what a run deletes for a borrowed cos_admin" reads in one place instead
of being inferred from twenty-odd `OR` clauses elsewhere.

### Schema drift between deployments

Staging, dev and a fresh stack do not carry the same tables, or even the same
column names — `user_activity_audit_log` names the delegate `delegate_id` on one
deployment and `delegator_id` on another, and `leaderboard_dirty_queue` exists
on dev but not in v2.3.

So the sweep reads `information_schema` first and adapts each statement to the
deployment in front of it:

- a statement naming a table this deployment lacks is **skipped**, and reported
  as `no such table on this deployment, skipped: ...`
- a term naming a column this deployment lacks is **dropped from its `OR`
  group**, reported as `no <column> column on this deployment, matched on the
  rest` — so `user_activity_audit_log` still sweeps on `user_id` and
  `asset_provider_id` even where `delegate_id` does not exist

Both moves only ever make a statement match *less*. If pruning would empty a
whole group the statement is skipped outright rather than run without it —
dropping an anchor group is exactly what would turn a scoped delete into an
unscoped one, and that must never happen quietly. Neither case is counted as a
sweep problem; a genuine SQL error still is.

The statement set itself targets **stable/v2.3**, the schema staging runs. The
guard is what keeps a run green on a deployment that is ahead of or behind it —
`leaderboard_dirty_queue`, for instance, exists on dev but not in v2.3, so the
statement is simply skipped there.

Teardown prints which scope it used, and `--sweep-only` resolves the same id
from the configured username so a crashed run's rows are reachable too. The flag
needs `run.delete_audit_rows`; it widens the cos_admin sweep rather than
enabling one, and startup rejects the combination that would silently do
nothing.

Prefer the borrowed mode where a short-lived super-admin with a stored password
is unacceptable; prefer the created mode where the run must leave no trace on an
account it does not own.

## The prefix

`run.prefix` namespaces every artefact the harness creates — usernames, emails,
org name, item name — combined with a per-run timestamp and a random suffix:

```
e2e-dev-202608190547-ccbc-requester
e2e-dev-202608190547-ccbc-org
```

This is what makes teardown a deterministic sweep rather than id bookkeeping that
breaks the moment a step fails mid-flow. It also lets two people run against the
same deployment without colliding, and lets a later run reap orphans a crashed
one left behind.

It is validated to 2–31 lowercase letters, digits and hyphens. A loose or empty
prefix would let the sweep match real data, so it is rejected.

Generated accounts use `run.email_domain`, `example.invalid` by default —
reserved by RFC 2606 and never resolving, so the platform's notification emails
cannot reach a real inbox. Override it for realms that validate the domain.

## Cleanup

Ordering matters — an item cannot be removed after its owner:

| Order | What | How |
|---|---|---|
| 1 | policy, catalogue item | ControlPlane API |
| 2 | consumers | `DELETE /iudx/v2/auth/user/delete` — cascades to Keycloak and DB |
| 3 | organisation | `DELETE .../organisations/{id}` — only when the DB sweep is off; the API cannot remove an org whose admin still exists |
| 4 | requester (org admin), and the cos admin if the harness created one | Keycloak Admin API — self-delete refuses org admins |
| 5 | everything else | Postgres sweep, if `run.sweep_database` |
| 6 | verify | re-query Keycloak and Postgres; fail the run if anything survived |

Teardown runs whether the flow passed or failed. `run.verify_cleanup` makes
"cleaned up" something the harness asserts rather than assumes.

`run.sweep_older_than_hours` is an age guard on leftovers belonging to *other*
runs sharing the prefix. It defaults to 0 — no guard, sweep everything under the
prefix — which is what a test harness wants, since it only ever creates
disposable data. Raise it only if several people share one prefix on one
deployment and a sweep could hit a run still in flight; giving each person their
own `run.prefix` is the better isolation.

### Why the sweep is not optional

The APIs cannot fully remove two things:

- **Policies soft-delete.** `PUT /iudx/acl/apd/v2/policy` flips the status from
  `ACTIVE` to `DELETE` and keeps the row, deliberately, for traceability.
- **Access requests are retained** in `request` after approval.

So an API-only teardown leaves both behind permanently. The sweep hard-deletes
them, along with `access_rule` (and its cascading children), `request_messages`,
`client_credentials`, `user_interactions`, `item_votes`,
`asset_visibility_snapshot`, the four organisation tables and `user_table` —
children before parents, because `policy.owner_id` and
`provider_requests.user_id` are real foreign keys.

Rows are matched three ways, which is what makes the sweep robust:

- **By prefix** on the namespaced columns (`organizations.name`,
  `policy.user_emailid`, `request.consumer_email_id`) — this reaches orphans
  from crashed runs, where no id was ever recorded.
- **By Keycloak user id**, read before anything is deleted. Keycloak is the
  authority on users; the ids it returns *are* the database's user ids
  (`UserAccessHandler.java:65` stores `dxUser.sub()` as `user_table._id`).
- **By id** captured during the run, for the item and organisation.

### The OGC database is swept separately

Everything above is the ControlPlane database, `postgres` — `iudx_v2_auth`. The
OGC server keeps its collections in its own, `ogc_postgres`, and nothing in
there carries the prefix: a collection is keyed by the item uuid, and its
feature table is *named* that uuid. There is no namespaced column for a `LIKE`
to match, so the ControlPlane sweep cannot reach a single OGC row.

During a run this does not matter — teardown removes each lane's collection by
id. Between runs it matters a lot, and `--sweep-only` runs
`_sweep_ogc_collections` for exactly that. It anchors three ways:

- **By prefix on `collections_details.title`.** Every collection the harness
  onboards is titled `<namespace> <configured label>` — the OGC equivalent of
  `organizations.name`, and the only anchor that still works once a run's users
  are gone. The title column is `varchar(100)`, so the namespace goes first and
  the label is what a trim cuts.
- **By owner**, `ri_details.role_id` — the provider's Keycloak id, resolved by
  prefix before `_teardown_keycloak_users` destroys those accounts. This reaches
  a collection whatever its title says.
- **By the configured label and description together, matched exactly** (not by
  wildcard, and both halves required). This is what reaches collections
  onboarded before the title carried a namespace. A title alone is not specific
  enough to delete on — someone may well have called a collection `test` — but a
  collection carrying both this harness's label *and* its "Safe to delete."
  description is unambiguously one of ours.

A collection whose title is not namespaced *and* whose owner is already gone
from Keycloak *and* whose label has since been reconfigured is reachable by none
of the three and has to be removed by hand.

The sweep runs under `--sweep-only` only, never in a normal teardown: teardown
already removes its own collections by id, and it is the only thing that knows
`ogc_vector_delete.keep_on_failure` is holding a failed onboarding for
inspection. A prefix-wide pass there would delete the very wreckage that flag
preserves.

### Elasticsearch has no anchor of its own

An NGSI-LD item's data index is named `<index_prefix><item id>` — `iudx-v2__` +
a uuid — and carries nothing else. No prefix, no owner, no label. So unlike the
catalogue, the OGC database or Keycloak, Elasticsearch offers a sweep nothing to
match on, and an index whose catalogue item is gone is invisible to every other
pass: the NGSI-LD teardown is reached *through* the item, and once that is
deleted nothing on the platform names the index.

The anchor has to come from outside, and there is exactly one: `request`. Its
`consumer_email_id` carries the namespaced email and its `item_id` the item the
access request was raised against, so every item that ever had data — data means
phase 04, which means it passed phase 03 — has a row there. `_resolve_item_ids`
reads those ids **before `_sweep_database` deletes the rows**, and
`_sweep_elasticsearch_indices` deletes an index only when its uuid is in that
set.

Matching on ids rather than on a name pattern is what makes this safe: an index
this harness never created cannot be named by the set, whatever else shares the
deployment. It follows that a sweep with no ids resolved deletes nothing at all
rather than falling back to a wildcard — and says so.

`ngsild_delete.dry_run` applies here too: it reports what it would delete, with
document counts, and touches nothing.

The limit is the same one the anchor implies. An index whose `request` row has
already been swept — by an earlier run of the sweep, before this existed — is
past recovering automatically and has to be removed by hand.

### Why a raster collection could not be deleted at all

`collections_details` is the parent of eight tables, and only four of them
cascade on delete. `vector_deletion.py` clears `collections_enclosure` but none
of `stac_collections_part`, `stac_items_assets` or `stac_collections_assets` —
and it runs every step in one transaction. So a raster collection whose STAC
items are still in the database fails the `DELETE` with a foreign-key violation,
the rollback undoes the `ri_details` and `collections_enclosure` deletes too, and
**nothing at all** is removed. The script reports a failed transaction and the
collection is permanently undeletable by this harness.

Normally the STAC API teardown empties those tables first. It could not run
under `--sweep-only` at all: it needs the item owner's token, and it was asking
Keycloak for *this* run's provider — an account `--sweep-only` never creates. It
now uses the orphan owner's own token, the one the catalogue sweep already holds.

For the case where even that is unavailable — the owner is deleted, or signs in
with a different password — `ogc_vector_delete` now clears those three tables
over its own connection before running the script. A collection that never had
STAC rows loses nothing by the attempt.

### Why user_table is not the anchor

`user_table` is populated lazily, by `UserAccessHandler` — and that handler is
attached only to the ACL routes (`UPDATE_ACCESS_REQUEST`, `CREATE_POLICY`,
`DELETE_POLICY`, `VERIFY`). A run that fails in phase 01 or 02 never reaches any
of them, so its users have Keycloak accounts and organisation rows but **no
`user_table` row at all**. Anchoring on `user_table` would silently resolve to
an empty id set, and every user-keyed `DELETE` would become a no-op rather than
an error — leaving `client_credentials`, `user_interactions`, `item_votes` and
`organization_users` behind with nothing to report.

Identities are therefore resolved from Keycloak **before any deletion runs**.
Both the Keycloak sweep and the DB sweep destroy the records that make those ids
discoverable, so resolving late would find nothing.

If `run.sweep_database` is off, the harness prints a warning at startup saying
policy and access-request rows will be left behind. It needs `postgres.enabled`,
and `run.delete_audit_rows` in turn needs `run.sweep_database`.

### Audit rows

`user_activity_audit_log`, `user_activity_log` and `activity_audit_log` are
append-only by design, and
the audit Elasticsearch index belongs to a downstream service. On a shared
deployment leave `run.delete_audit_rows` false and let the harness assert on
those rows instead — that turns cleanup debt into real coverage of the
RabbitMQ to Elasticsearch path. Set it true only on a stack you own.

`run.delete_audit_rows` covers three log tables for every user the run creates:
`user_activity_audit_log`, `user_activity_log` and `activity_audit_log` — the
last a second, separate audit table, not a view over the first. Backup tables
are never swept; they exist for your own testing and are not platform state. A
borrowed cos_admin gets the same three, at the reach
`run.delete_all_cos_admin_data` sets (see [cos_admin](#cos_admin)). The
Elasticsearch audit index is never swept for anyone.
