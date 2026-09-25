# dx-automation-script

End-to-end testing for the DX platform: one command drives the complete
onboarding workflow against a live deployment — users, organisation, catalogue
item, access policy, data onboarding, data-plane reads and audit trail — then
removes everything it created.

```bash
python3 main/e2e.py
```

Exit code is 0 only when every phase passed **and** nothing survived teardown.

---

## What a run does

| Phase | What it proves |
|---|---|
| `00 sign in` | Keycloak users can be created and signed in |
| `01 organisation onboarding` | request → cos-admin approval → roles land in the token |
| `02 catalogue` | a RESTRICTED item is created, indexed and published ACTIVE |
| `03 access request` | consumer requests, provider grants, a policy exists |
| `04 data onboarding` | data is actually put behind the item — see below |
| `05 resource servers` | a granted token reads it; an ungranted one is refused |
| `06 auditing` | the RabbitMQ → Elasticsearch audit path delivered |
| `07 sandbox` | the sandbox server is up and refusing unauthenticated callers |
| `08 community` | the community layer is healthy and both products serve and refuse correctly |
| teardown | every artefact is removed, and the run fails if anything survives |

Phase 04 dispatches on what the item declares, so one harness covers four very
different pipelines:

| Server | Onboarding | Read in phase 05 | Teardown |
|---|---|---|---|
| **NGSI-LD** | publish to the item's RabbitMQ exchange | `/ngsi-ld/v1/entities?id=` | exchange, ES index, broker user |
| **Gateway** | none — an RPC adaptor is started for the reads | `/rsp/ngsi-ld/v1/entities?id=` | queue, broker user |
| **OGC vector** | GeoPackage → pre-signed S3 → collection onboarding job | `/collections/<item>` | collection rows, table |
| **OGC raster** | STAC items → GeoTIFF upload to S3 | `/stac/collections/<item>/items` | STAC items, collection rows |
| **File** | multipart upload into the item's databank | `/v1/databanks/<item>/download` | uploaded objects |

The harness never reimplements this work: it runs the same scripts in
`script/*_Automation_Script/` that are used by hand, generating their config per
run and reading their exit codes and logs.

### Two providers, when NGSI-LD and the gateway are both enabled

The catalogue names the RabbitMQ user it creates after the item's **provider**,
not after the item. One provider owning both an NGSI-LD item and a gateway one
therefore ends up with a single broker user standing for both — and teardown has
two scripts queued to delete it. The first removes it; the second is left
working on a user that is no longer there.

So when both servers are enabled a run creates a **second provider**, with its
own organisation and its own gateway-only item, while every other server stays
on the first provider's item. Each teardown then deletes its own broker user.
Phases 01, 02, 03 and 05 simply run once per provider; the consumer requests
access to both items and reads each with its own resource token.

With only one of the two enabled nothing changes: one provider, one item,
carrying every enabled server as before.

---

## Layout

```
main/e2e.py                     the entry point — the only thing you run
main/config.json                your deployment (gitignored; holds credentials)
main/config.example.json        the committed template
main/reports/                   HTML reports land here

script/ControlPlane_Workflow/   the harness itself (imported as a package)
script/NGSILD_Automation_Script/    publish + teardown for NGSI-LD
script/GATEWAY_Automation_Script/   the RPC adaptor + teardown
script/OGC_Automation_Script/       vector and raster (STAC) pipelines
script/FILE_Automation_Script/      file upload + deletion
script/sandbox/                     sandbox server checks (phase 07)
script/community-layer/             community layer checks (phase 08)
script/user_creation/               standalone create/delete scripts per user role
script/challenge/                   standalone challenge scripts: create, set times, delete

docs/sandbox.md                     what the sandbox check covers, and cannot
docs/community-layer.md             the same for the community layer
docs/challenge-scripts.md           runbook for the challenge scripts
docs/challenge-round-walkthrough.md step-by-step: a full round (create → submit → evaluate → purge) in one sitting
```

The last two servers are checked, never onboarded to, and are separate
deployments with their own repositories — so their checks sit beside the other
per-server automation rather than inside the ControlPlane package.

`script/ControlPlane_Workflow/README.md` documents the harness in depth: every
config section, why teardown is a prefix sweep, and the platform quirks each
step works around.

---

## Reports

Every run writes a self-contained HTML page listing each request, its status,
timing and body, alongside the artefacts it created and the teardown result:

**[main/reports/e2e-report.html](main/reports/e2e-report.html)**

**Bodies are never recorded.** A report lists what was called, its status and
its timing — not the request, not the response, not an excerpt of a failed one.
Bodies are dropped at capture time rather than hidden at render time, so a
report simply has no content that could carry a credential. When you need them,
they are in the run's terminal output, which prints each response as it arrives.

Whatever *is* written — logs, terminal output, the artefact table — is redacted.
Every secret in the config is registered at startup and masked wherever it later
appears — including inside a script's raw
output or a URL the platform handed back — alongside patterns for things the
config never sees: JWTs, pre-signed S3 signatures, AWS key ids, and
`password=`/`secret:` pairs in free text. Identifiers are deliberately left
readable: item, organisation and user ids are per-run and disposable, and
without them a report cannot be matched to what it describes.

`run.report_file` keeps one file that each run overwrites — handy while
iterating. Leave it `null` and each report is named after the run's namespace
instead, so nothing is overwritten, which is what a scheduled run wants.

---

## Setup

```bash
cp main/config.example.json main/config.json     # then fill it in
python3 -m pip install requests psycopg2-binary pika boto3 rasterio
python3 main/e2e.py
```

Only `requests` is always needed. `psycopg2` is required when `postgres.enabled`
is set, `pika` and `boto3`/`rasterio` only by the automation scripts the enabled
resource servers use.

Useful flags:

```bash
python3 main/e2e.py --set run.cleanup=false      # leave artefacts in place
python3 main/e2e.py --only "04"                  # run one phase
python3 main/e2e.py --sweep-only                 # reap leftovers from a crash
python3 main/e2e.py --report run.html            # write the report elsewhere
python3 -m ControlPlane_Workflow.config          # print the resolved config, secrets masked
```

Any key can be overridden without editing the file — `--set path.to.key=value`,
or the environment (`DXE2E_KEYCLOAK__URL=…`). Values in the config may reference
the environment as `${VAR}` or `${VAR:-fallback}`, so a committed config need
never hold a secret.

---

## Configuration

`main/config.json` describes one deployment. Nothing is hardcoded in the
harness, so pointing at a different stack is a config change and nothing else.

### Connections

| Section | What it configures |
|---|---|
| `control_plane` | ControlPlane base URL and request timeout |
| `acl` | Policy/ACL server; defaults to `control_plane.base_url`, `apd_url` to that without the scheme |
| `keycloak` | URL, realm, the admin client used to provision users, the public client used to mint their tokens, and role names |
| `postgres` | ControlPlane database — residue sweep and row assertions |
| `ogc_postgres` | The OGC server's own database (`ogc_rs_v2`), shared by both OGC teardowns |
| `databroker` | RabbitMQ management API, for observing audit publication |

### Who runs the workflow

| Section | What it configures |
|---|---|
| `cos_admin` | **Empty:** the harness creates its own namespaced cos_admin and sweeps it. **Filled in:** that account is borrowed — used for org approval and publishing, never namespaced, never deleted, and its audit rows are never swept. |
| `run.prefix` | Namespaces every artefact, and is what teardown sweeps on |
| `run.user_password`, `run.email_domain` | Credentials and domain for the users each run creates |

### Which servers a run exercises

`resource_servers` holds the four deployed servers (`ngsild`, `gateway`, `ogc`,
`file`). Each carries a `url`, a `verify_path` for phase 05, and
`expect_denied` — the statuses an ungranted token must get, or `[]` for a path
that serves public data and cannot refuse anyone.

Set `enabled: false` to skip a server; at least one must stay enabled, since the
catalogue item's schema requires a non-empty `resourceServer` array. For OGC,
one switch picks the pipeline:

```json
"ogc": { "raster": true }    →  STAC / raster
"ogc": { "raster": false }   →  vector
```

It derives the item's query types, so the declared type, the onboarding path,
the read path and the teardown all move together.

### The sandbox server

`sandbox` is checked, not onboarded to — which is why it is its own config
section rather than a fifth `resource_servers` entry. The sandbox is a
notebook/compute service: it has no per-item route at all, and its middleware
validates a Keycloak **identity** token by `azp`, so the item-scoped token phase
05 mints could never read it. Declaring it on a catalogue item would be
declaring something untrue.

Phase 07 therefore asserts the two things that need no credentials:

| Call | Asserts |
|---|---|
| `GET /v1/health` | 200, and a body saying `"status": "ok"` — the service is up |
| `GET /v1/notebook/list`, no `Authorization` header | 401/403 — auth is in front of the API |

The second is the one that earns its place. `/v1/health` is registered *outside*
the auth middleware so a monitor can always reach it, which means it answers 200
even on a build whose auth is broken or absent; only an unauthenticated call to a
real API route shows the middleware is there.

Both routes are registered identically on `stable/v2.3` and `dev` — their
`cmd/api/router.go` is byte-identical — so one config checks either. The two
branches differ only in a blocked-email-domain rule inside the middleware, and
that sits *after* the token parse, so an unauthenticated request never reaches
it and the 401 is the same on both.

A third, optional call reads `verify_path` *with* a token and requires 200 —
proving the API works, not just that it is guarded. `sandbox.bearer_token`
supplies one directly; `sandbox.token_user` uses the identity token of a user
the run creates. Both are null by default, because whether either is accepted
depends on the deployment: the sandbox wants the token's `azp` to match its own
client id, and the account to be email- and KYC-verified. It refuses with a
different message for each of those, and the harness turns that message into the
reason — so one call says what a deployment actually wants. The read itself is
safe: the sandbox middleware writes nothing and the listing only reads.

Creating a notebook and tearing it down again is **not** part of this — the
sandbox has no delete route for the Kubeflow profile, namespace or 50Gi
workspace PVC that creation provisions, and the namespace is named after the
caller's Keycloak id, so a run using its own throwaway user would strand all
three every time. [docs/sandbox.md](docs/sandbox.md) records the full evidence
and what would have to change.

### The community layer

`community` is checked the same way and for the same reason as `sandbox` — it is
an identity-token API with no per-item route, so it is not a catalogue resource
server. It carries two products behind one process, Discussion and Challenge,
and phase 08 covers both:

| Call | Asserts |
|---|---|
| `GET /healthz` | 200 **and** every dependency it reports is up |
| `GET /discussion/tags/popular`, `GET /challenge/all` | 200 — the router is mounted and serving real data |
| `GET /discussion/recent/bookmarked`, `GET /challenge/participated`, no auth header | 401/403 — the protected routes are protected |

`/healthz` is worth having: it runs `SELECT 1` against Postgres and
`head_bucket` against S3, and answers 500 when either fails. The harness asserts
the per-dependency map rather than the status, so a failure says *which*
dependency is down.

The service mounts each product only when it is listed in its
`ACTIVATED_SERVICES`, so a Discussion-only deployment answers 404 — not 401 — on
every `/challenge` route. `community.services.<name>.enabled` mirrors that.

Every call is deliberately unauthenticated: the authoriser inserts a `users` row
on each *authenticated* request and no route deletes a user, so a token would
leave a row behind per run. [docs/community-layer.md](docs/community-layer.md)
has the detail, including a live 500 on one public route worth reporting.

### Per-server onboarding and teardown

| Section | Runs |
|---|---|
| `ngsild_publish` | `ngsild_publish_v1.py` — records into the item's exchange |
| `ngsild_delete` | `ngsild_delete_v1.py` — ES index, exchange, broker user. **Off by default:** the only step needing Elasticsearch credentials |
| `gateway_adaptor` | `gateway.py` — the RPC consumer, run only for the length of phase 05 |
| `gateway_delete` | `delete_rmq.py` — queue and broker user |
| `ogc_vector` / `ogc_vector_delete` | GeoPackage onboarding and its database rows |
| `ogc_raster` / `ogc_raster_delete` | STAC ingestion + GeoTIFF upload, and item deletion |
| `ogc_s3_cleanup` | The files themselves — `<item>.gpkg` or the `<item>/` folder of tiffs |
| `file_upload` / `file_delete` | Files Connect multipart upload and object deletion |

Values that have no sensible default and must be supplied when the matching
server is enabled: `ogc_vector.gpkg_path`, `bucket_name`, `region`;
`ogc_raster.aws_access_key_id`, `aws_secret_access_key`;
`ngsild_delete.kibana_username`, `kibana_password`. Everything else falls back
sensibly — one broker credential, one bucket, one database connection, reused
across the sections that need them.

### Cleanup

| Key | Effect |
|---|---|
| `run.cleanup` | Remove everything the run created (default on) |
| `run.verify_cleanup` | Re-query afterwards and fail the run if anything survived |
| `run.sweep_database` | Also delete rows the APIs only soft-delete — policies and access requests |
| `run.delete_audit_rows` | Sweep this run's audit rows, across all four log tables; a borrowed cos_admin's are swept too, scoped to the run |
| `run.sweep_older_than_hours` | Age guard for leftovers belonging to other runs sharing the prefix |

Teardown runs in a `finally` block, so artefacts are removed even when a phase
fails — which is exactly when leaving them behind hurts most. Data-plane objects
go first, while the item that names them still exists; `--sweep-only` applies
the same logic to orphans from a crashed run.
