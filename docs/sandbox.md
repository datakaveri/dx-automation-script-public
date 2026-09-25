# Sandbox server — what the harness checks, and what it cannot

**Status:** phase 07 ships a read-only check, now including an authenticated
read that returns 200 on dev (see [§4](#4-what-the-dev-deployment-actually-wants--measured-2026-09-10)).
Create / validate / tear-down is
**not** implemented, and cannot be until the sandbox grows the delete routes
listed in [§3](#3-what-blocks-createteardown). This document records why, so the
decision does not have to be re-derived later.

| | |
|---|---|
| Service | `sandbox-connect-api` (Go), `github.com/datakaveri/sandbox-connect-api` |
| Deployed (dev) | `https://v2.dev.sandbox.iudx.io/api` |
| Reviewed at | `stable/v2.3` @ `69f3c122cae7` (2026-09-03) · `dev` @ `46c793cd1037` (2026-08-12) |
| Harness code | `script/sandbox/sandbox_check.py`, phase `07 sandbox` |
| Written | 2026-09-10 |

## Branch sync

`dev`'s tip **is** a merge commit, `merge: synchronize dev and stable/v2.3`, so
the two were in step as of 2026-08-12. `stable/v2.3` has moved ahead since.
For our purposes the difference is exactly one thing:

* `cmd/api/router.go` is **byte-identical** on both branches. Every route this
  document names is in the same place on either.
* `cmd/api/middleware.go` differs only by `isEmailDomainBlocked` — present on
  `stable/v2.3`, absent on `dev`. It returns 403 for an address whose domain is
  listed in `API_BLOCKED_EMAIL_DOMAINS`.

That check sits **after** the token parse, so an unauthenticated request never
reaches it. This is what makes the phase-07 assertions valid against either
deployment without branching on version — but see [§4](#4-open-questions): if
`example.invalid` were ever added to that list, an *authenticated* check using
harness-created accounts would 403 on stable and pass on dev.

## 1. What is checked today

Phase 07, `script/sandbox/sandbox_check.py`. Two calls, neither needing
credentials, both creating nothing:

| Call | Asserts | Config key |
|---|---|---|
| `GET /api/v1/health` | 200 **and** body `status == "ok"` | `sandbox.health_path`, `sandbox.expect_status` |
| `GET /api/v1/notebook/list`, no `Authorization` header | 401 / 403 | `sandbox.verify_path`, `sandbox.expect_denied` |

```
[07 sandbox]
    https://v2.dev.sandbox.iudx.io/api — health:ok (v1), unauthenticated:401
```

The second call is the one that earns its place. `/v1/health` is registered on
the root mux at `router.go:30`, **outside** `authMiddleware`, deliberately, so a
monitor can always reach it — which means it answers 200 even on a build whose
auth is broken or absent. Only an unauthenticated call to a real API route shows
the middleware is actually in front of the API. Pointing `verify_path` at the
health route makes the phase fail, which is the proof the assertion is live.

Run it alone with `python3 main/e2e.py --only "07 sandbox"`. It is
`enabled: false` by default so a deployment without a sandbox stays green.

### Why it is not a `resource_servers` entry

Deliberate, and worth not re-litigating:

* **No route takes an item id.** Everything is `/v1/notebook/…`,
  `/v1/bookings/…`, `/v1/profile/create`. `verify_path` in this harness is
  item-addressed by construction (`?id=<item>` or `{item_id}` in the path);
  there is nothing here for it to address.
* **`authMiddleware` wants an identity token.** It reads the JWT's `azp` and
  refuses anything whose authorized party is not `API_KEYCLOAK_CLIENT_ID`, then
  requires `email_verified`, then `kyc_verified` when `API_KYC_ENABLED` is set.
  The item-scoped token phase 05 mints is issued by ControlPlane, not by that
  client, so a "granted" read could never return 200.
* **It leaves nothing behind** — no exchange, no collection, no databank — so no
  teardown branch has anything to remove.

Putting it in `resource_servers` would push a `sandbox` entry into every
catalogue item's `resourceServer` array, on the way to a read that cannot
succeed.

## 2. What is missing: creation has no matching teardown

`POST /v1/notebook/create` (`handlers.go:80`) provisions far more than a
notebook. `DELETE /v1/notebook/delete` (`handlers.go:668`) removes some of it.

| Created | Where | Removed by delete? |
|---|---|---|
| Notebook k8s resource | `deleteNotebookFromK8s` | **yes** |
| `<name>-pvc`, label-managed notebook PVCs | `deletePVCFromK8s`, `deleteManagedPVCsFromK8s` (`k8s.go:289,310`) | **yes** |
| Platform-token secret | `deletePlatformTokenSecret` | **yes** |
| `notebooks` DB row | `handlers.go:800` | **soft only** — renamed `…__deleted__<id>__<epoch>`, row kept forever |
| **Kubeflow `Profile` CRD** | `createKubeflowProfile` (`k8s.go:331`) | **no** |
| **Namespace**, named `userInfo.Sub` | provisioned by the Profile | **no** |
| **Workspace PVC** — `workspace`, **50Gi**, `ReadWriteMany`, `ceph-filesystem` | `ensureProfileWorkspacePVC` (`k8s.go:69`) | **no** |
| `profiles` DB row | `handlers.go:165` (`ON CONFLICT DO NOTHING`) | **no** |

There is no delete route for any of the bottom four. The router has exactly one
profile route — `POST /v1/profile/create` (`router.go:69`) — and no DELETE, on
either branch. `deleteManagedPVCsFromK8s` matches notebook-scoped labels only;
the workspace PVC carries `sandbox-connect.tgdex.io/profile-workspace: "true"`
and nothing anywhere deletes it.

### 2.1 Why that is fatal for *this* harness specifically

The namespace is `userInfo.Sub` — the caller's Keycloak id. This harness creates
a **throwaway Keycloak user per run** and deletes it during teardown.

So a create/teardown phase driven by the run's own provider would strand, on
every single run:

* one Kubeflow Profile and namespace named after a `sub` that no longer exists
* one 50Gi CephFS PVC inside it
* one `profiles` row
* one soft-deleted `notebooks` row

None of it is reachable afterwards. The prefix sweep cannot see it — nothing
there carries `run.prefix`. `--sweep-only` cannot reap it — there is no API to
call. `run.verify_cleanup` cannot even report it. It is the same trap the
harness README documents for Elasticsearch indices ("nothing on the platform
still names it"), except the residue is cluster storage and the only cleanup is
a cluster admin with `kubectl`.

Ten runs is ten orphan namespaces and 500Gi of unreferenced CephFS claims.

## 3. What blocks create/teardown

In priority order.

1. **No delete endpoint for profile, namespace or workspace PVC.** The blocker.
   Everything else below is solvable on our side; this one is not.
2. **Soft-deleted `notebooks` rows accumulate.** Bounded and probably
   acceptable, but it means "teardown left nothing" is never literally true.
3. **The create route may not be registered at all.** `router.go:40` registers
   `POST /v1/notebook/create` **only when `API_BOOKINGS_ENABLED=false`**. When
   true it is absent (404) and creation goes through `POST /v1/bookings` — GPU
   slot scheduling, a different shape entirely. The shipped configmap has
   `"true"`. The flag is not readable from outside: `/v1/notebook/list`,
   `/v1/categories` and `/v1/bookings` all answer 401 unauthenticated, so the
   mode is invisible without a valid token. A phase that must guess which
   creation call is live is not deterministic across dev and staging.
4. **KYC gate.** `API_KYC_ENABLED` gates every API route except
   `/v1/profile/create` and the JupyterLite paths. Harness users carry no
   `kyc_verified` claim. Solvable — `script/user_creation/*/creation/*.py`
   already sets that Keycloak attribute for the compute role — but it is another
   moving part, and Keycloak replaces the whole attribute map on write.
5. **Realm and client.** `azp` must equal the sandbox's `API_KEYCLOAK_CLIENT_ID`
   and the token must come from its `API_KEYCLOAK_REALM`. The committed
   configmap names `tgdex` / `angular-tgdex-client`; whether the dev deployment
   trusts our `iudx-v2` realm client is **unconfirmed** — see §4.
6. **GPU notebooks need the `compute` role** (`handlers.go:139`). CPU notebooks
   do not, so a CPU notebook is the cheaper target.
7. **Quotas and time.** `API_MAX_RUNNING_CPU` 2, `API_MAX_TOTAL_CPU` 6 per user.
   Fine for a fresh account; the concern is cluster-side accumulation. And an
   image pull plus pod schedule plus PVC bind is minutes, against seconds for
   everything else the harness does.

## 4. What the dev deployment actually wants — measured 2026-09-10

These were open questions; one authenticated call settled them. Verified against
`https://v2.dev.sandbox.iudx.io/api` with a token from `v2.dev.iudx.io/auth`,
realm `iudx-v2`.

| Question | Answer |
|---|---|
| Does the dev sandbox trust our dev Keycloak? | **Yes.** A token from realm `iudx-v2` verifies — the refusal was `Invalid client`, which is reached only *after* the signature check passes |
| Which client does it want? | **`angular-client`.** Not the `frontend-client` the rest of the harness signs in with, and not the `angular-tgdex-client` the committed configmap names |
| Is `API_KYC_ENABLED` on? | **No.** The token carried no `kyc_verified` claim and still got 200; `/v1/notebook/list` is not one of the KYC-exempt routes, so the gate must be off |
| Is `example.invalid` blocked? | **No** — and `dev` has no blocked-domain check at all |

```
frontend-client  -> azp=frontend-client   -> 401 {"detail":"Invalid client"}
angular-client   -> azp=angular-client    -> 200 {"notebooks":[],"next_offset":-1}
```

**Why the client matters more than it looks.** `azp` is the client a token was
*minted* with, so a service that checks it cannot be satisfied by re-using a
token from elsewhere — it has to be minted against that client. Hence
`sandbox.token_client_id`: the harness re-mints the run user's token with the
client the sandbox expects, rather than sending the one every other phase uses.

Dev configuration, as measured:

```json
"sandbox": {
  "token_user": "consumer",
  "token_client_id": "angular-client"
}
```

### Still open

1. **Which creation mode is dev in?** `API_BOOKINGS_ENABLED` decides whether
   `POST /v1/notebook/create` is registered at all (§3.3). Now answerable with
   the token above: `GET /v1/categories` returning 200 means bookings are on;
   the "Bookings and scheduling are disabled" 404 means they are off. Only
   matters if create/teardown is revisited.
2. **Will the sandbox add a profile/namespace delete route?** If yes, full
   create/teardown becomes worth revisiting. If no, §5 option B is the ceiling.
3. **Is `angular-client` stable across deployments?** It is a dev fact, not a
   documented one — the committed configmap names `angular-tgdex-client`, so
   staging may differ. `token_client_id` is per-config for that reason.

## 5. Options, when we come back to this

**A — what ships today.** Health + unauthenticated refusal. Zero footprint,
zero credentials, works on both branches. Already done.

**B — the authenticated GET. Built, and off by default.** `GET
/v1/notebook/list` read *with* a token, required to answer 200. This is the half
that proves the API works rather than merely that it is guarded.

It creates nothing, and that is checked rather than assumed: the sandbox
middleware validates the token and performs no database write (unlike the
community layer's, which inserts a user row — see
[community-layer.md §2.1](community-layer.md#21-authenticating-creates-a-user-row-that-nothing-deletes)),
and `listNotebooks` (`handlers.go:867`) only reads. `createKubeflowProfile` is
reached solely from `createNotebook` and `createProfile`, neither of which the
harness calls.

Two ways to supply the token:

| Config | Token used | When |
|---|---|---|
| `sandbox.bearer_token` | pasted in, any realm | testing what a deployment expects, without running the flow |
| `sandbox.token_user` | the identity token of a user this run creates (`"consumer"`, `"requester"`, …) | a full run, once the deployment is known to trust our realm |

`token_user` needs phase 00 to have run, so under `--only "07 sandbox"` it stands
down with a note rather than failing.

**Whether it passes is a property of the deployment, and unknown for dev.** The
middleware wants the token's `azp` to equal `API_KEYCLOAK_CLIENT_ID`, the
account to be email-verified, and — when `API_KYC_ENABLED` is on — KYC-verified.
The harness creates email-verified accounts but sets no `kyc_verified` claim.

**The refusal is diagnostic, and the harness uses that.** Each of those checks
returns a distinct message, so one failed call says exactly which requirement
was not met. `TOKEN_HINTS` in `sandbox_check.py` maps them:

| Sandbox says | Means |
|---|---|
| `Invalid token` | not signed by the Keycloak this sandbox trusts |
| `Invalid client` | right Keycloak, wrong client — `azp` ≠ `API_KEYCLOAK_CLIENT_ID` |
| `Your email is not verified` | account is not email-verified |
| `KYC is not verified` | `API_KYC_ENABLED` is on, and the account has no `kyc_verified` claim |
| `…not allowed for this email domain` | the domain is in `API_BLOCKED_EMAIL_DOMAINS` (`stable/v2.3` only) |

That is how §4 was answered: one token, one call, and the message named the
requirement that was missing.

**Verified working on dev**, `2026-09-10`:

```
[07 sandbox]
    https://v2.dev.sandbox.iudx.io/api — health:ok (v1), unauthenticated:401, authenticated:200
```

**C — full create / validate / teardown.** Only sane with a **dedicated
long-lived sandbox account**, never the run's throwaway user, so that the
namespace, Profile and 50Gi workspace PVC are created once and reused instead of
orphaned per run. Create a **CPU** notebook, poll
`GET /v1/notebook/status/{name}`, then `DELETE /v1/notebook/delete`. Residue per
run is then bounded to one soft-deleted `notebooks` row. Still needs items 3–5
of §3 resolved, and still costs minutes per run.

Driving C with the run's own throwaway provider is the one shape to avoid — it
is the orphan generator described in §2.1.

## 6. Re-verifying these findings

They are pinned to the SHAs in the header; re-check after any sandbox release.

```bash
# routes, and whether the two branches still agree
for b in stable/v2.3 dev; do
  gh api "repos/datakaveri/sandbox-connect-api/contents/cmd/api/router.go?ref=$b" \
    -q .content | base64 -d > "/tmp/router_${b//\//_}.go"
done
diff /tmp/router_stable_v2.3.go /tmp/router_dev.go     # expected: identical

# is a profile/namespace delete route still absent?
grep -n "profile" /tmp/router_stable_v2.3.go           # expected: POST /v1/profile/create only

# the live deployment
curl -s https://v2.dev.sandbox.iudx.io/api/v1/health           # 200 {"status":"ok",...}
curl -s https://v2.dev.sandbox.iudx.io/api/v1/notebook/list    # 401 missing auth header

# the harness phase
python3 main/e2e.py --only "07 sandbox"
```

## 7. Source references

All paths in `github.com/datakaveri/sandbox-connect-api` at the SHAs above.

| What | Where |
|---|---|
| Health route, outside auth | `cmd/api/router.go:30`, handler at end of same file |
| Bookings-vs-notebook route toggle | `cmd/api/router.go:40-64` |
| Only profile route | `cmd/api/router.go:69` |
| `azp` / `email_verified` / KYC checks, blocked domains | `cmd/api/middleware.go` |
| Notebook creation, profile auto-create, workspace PVC | `cmd/api/handlers.go:80-200` |
| Notebook deletion, soft delete, PVC cleanup | `cmd/api/handlers.go:668-848` |
| Read-only listing | `cmd/api/handlers.go:867` |
| Workspace PVC constants (`workspace`, 50Gi, CephFS, RWX) | `cmd/api/k8s.go:20-23` |
| `ensureProfileWorkspacePVC` — no counterpart exists | `cmd/api/k8s.go:69` |
| `createKubeflowProfile` — no counterpart exists | `cmd/api/k8s.go:331` |
| Deployed config (realm, client, KYC, bookings) | `infra/api/configmap.yaml` |
