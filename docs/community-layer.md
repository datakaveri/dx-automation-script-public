# Community layer — what the harness checks, and what it cannot

**Status:** phase 08 ships a read-only check covering the service health and both
mounted products, Discussion and Challenge. Create / validate / tear-down is
**not** implemented; unlike the sandbox that is a *choice* rather than a hard
block, and [§3](#3-what-blocks-createteardown) says what it would cost.

| | |
|---|---|
| Service | `dx-community-layer` (Python / FastAPI), `github.com/datakaveri/dx-community-layer` |
| Deployed (dev) | `https://v2.dev.iudx.io/community` |
| Reviewed at | `dev` @ `2a384ac639c9` (2026-09-07) |
| Harness code | `script/community-layer/community_check.py`, phase `08 community` |
| Written | 2026-09-10 |

Companion to [sandbox.md](sandbox.md), which covers the same ground for
`sandbox-connect-api`. The two services have the same *shape* of problem — an
identity-token API with no per-item route — but very different severity, and the
contrast is the useful part: see [§4](#4-compared-with-the-sandbox).

## 1. What is checked today

Phase 08, `script/community-layer/community_check.py`. Five calls, none needing
credentials, none creating anything:

| Call | Asserts |
|---|---|
| `GET /healthz` | 200 **and** every dependency it reports is up |
| `GET /discussion/tags/popular` | 200, body `success != false` |
| `GET /discussion/recent/bookmarked`, no auth header | 401 / 403 |
| `GET /challenge/all` | 200, body `success != false` |
| `GET /challenge/participated`, no auth header | 401 / 403 |

```
[08 community]
    https://v2.dev.iudx.io/community — health:ok (AWS S3, PostgreSQL DB)
      | discussion public:200 unauthenticated:401
      | challenge  public:200 unauthenticated:401
```

Run it alone with `python3 main/e2e.py --only "08 community"`. It is
`enabled: false` by default so a deployment without it stays green.

### `/healthz` is a real health check

Worth calling out, because most are not. `src/routes/utility.py` runs
`SELECT 1` against Postgres and `head_bucket` against the S3 bucket, returns
200 only when **both** pass, and 500 with a per-dependency map otherwise. So it
answers "can this service actually work", not merely "is the process alive".

The harness asserts the map, not just the status, so a failure names the thing
that broke — `AWS S3 unhealthy` reads very differently from `500`. Set
`community.expect_dependencies` to null to accept whatever it reports, which
keeps working if the service starts checking a third dependency.

### Why a public read *and* a refusal, per service

The service has three kinds of route, and each check covers a different failure:

* **`/healthz`** — dependencies. Says nothing about whether routing works.
* **`public_path`** — a `http_bearer_header_public` route. Token optional, so it
  serves real data to an anonymous caller. This proves the router is mounted and
  the product is reading its own database. Says nothing about authorisation.
* **`verify_path`** — a `http_bearer_header` route, called with no
  `Authorization` header. Its 401 proves the protected routes are protected,
  which a public 200 cannot show.

Pointing `verify_path` at a public route makes the phase fail, which is the
proof the assertion is live rather than decorative.

### The two products are mounted independently

`src/main.py` includes the Discussion and Challenge routers **only when they
appear in `ACTIVATED_SERVICES`**. A Discussion-only deployment answers 404 on
every `/challenge` route — not 401. `community.services.<name>.enabled` mirrors
that, so the harness asserts what a deployment actually runs; at least one must
stay enabled or the phase is just the health check, and startup says so.

### Why it is not a `resource_servers` entry

The same three reasons as the sandbox, in
[sandbox.md §1](sandbox.md#why-it-is-not-a-resource_servers-entry): no route
takes an item id; `src/middlewares/authorization.py` validates a Keycloak
**identity** token against `KEYCLOAK_AUDIENCE` and `KEYCLOAK_ISSUER`, not the
item-scoped token phase 05 mints; and there is nothing on a catalogue item for
it to correspond to.

## 2. What is missing

### 2.1 Authenticating creates a user row that nothing deletes

The blocker for any authenticated check, and the reason `bearer_token` is null.

`HttpBearerHeader.__call__` does not merely validate a token. On every
**authenticated** request it calls `update_user_info`, which **inserts a `User`
row** into the discussion and/or challenge database (`src/middlewares/
authorization.py`), keyed by the token's `sub`, choosing the database from
whether the path starts with `/challenge`.

There is no route that deletes a user. The only `DELETE` under `/users` is
`/challenge/users/challenges/{id}/bookmark` — unbookmark, not user removal.

This harness creates a throwaway Keycloak user per run. So an authenticated
check driven by the run's own user would leave one orphan `users` row per run,
per database, keyed to a `sub` that no longer exists in Keycloak — invisible to
the prefix sweep (nothing there carries `run.prefix`), unreapable by
`--sweep-only` (no API to call), and unreported by `run.verify_cleanup`.

**This is exactly why the phase-08 checks are unauthenticated.** An
unauthenticated call never reaches that code: the public bearer returns early
with `user_id=None`, and the strict one raises 401 before any database work. The
current checks therefore leave nothing behind, and that property is worth
keeping deliberately rather than by accident.

It is one row, not the sandbox's 50Gi CephFS volume — small enough that a
dedicated long-lived account (never the throwaway user) makes an authenticated
check perfectly reasonable. That is the recommended path in
[§5](#5-options-when-we-come-back-to-this).

### 2.2 Deleting content still leaves an audit row

`delete_discussion_handler` (`src/services/discussion/discussion_services.py`)
is a genuine hard `DELETE` of the `Discussion` row — good, and better than the
sandbox's soft delete. But it first inserts a `DeletedDiscussion` record keyed
on `user_id`, which persists. So "teardown left nothing" would not be literally
true for a create/teardown phase; it would be bounded and small.

### 2.3 A live 500 on a public route

`GET /community/challenge/users/challenges` returns **500 Internal Server
Error** on dev as of 2026-09-10:

```
{"success":false,"status_code":500,"message":"Internal Server Error", ...}
```

It is a public (`http_bearer_header_public`) route, so this is reachable by any
anonymous caller. `GET /challenge/all` — the equivalent listing, and the one the
harness uses — works and returns real data, so this looks like a bug in that
handler rather than an outage. **Worth raising with the community-layer team;**
it is not something the harness can work around.

### 2.4 Smaller notes

* **JWKS URL builds in `/auth`.** `authorization.py` composes
  `f"{KEYCLOAK_URL}/auth/realms/{REALM}/protocol/openid-connect/certs"`, so
  `KEYCLOAK_URL` must **not** already end in `/auth` or the lookup 404s and every
  authenticated request fails with a 500 "User authorization failed". A
  deployment-config trap worth knowing before debugging a token problem.
* **Token audience and issuer must match** `KEYCLOAK_AUDIENCE` /
  `KEYCLOAK_ISSUER`. Whether a token minted by the harness's
  `keycloak.user_client_id` satisfies them on dev is **unconfirmed** — see §6.
* **Redis is on the auth path** (JWKS cache, token cache, user id-map) but is
  *not* covered by `/healthz`, which checks only Postgres and S3. A Redis outage
  would therefore show as authenticated requests failing while `/healthz` still
  reports the service healthy.

## 3. What blocks create/teardown

Unlike the sandbox, nothing here is a hard block. In rough order of cost:

1. **Orphan `users` rows** unless a dedicated long-lived account is used (§2.1).
   Solvable by configuration, not code.
2. **`DeletedDiscussion` rows persist** after content deletion (§2.2). Bounded.
3. **Content deletion is ownership-checked** — `delete_discussion_handler`
   returns 403 unless `discussion.user_id` matches the caller, so teardown must
   run as the same account that created the item. Straightforward, but it means
   a shared fixture account, not one account creating and another cleaning up.
4. **Challenge creation is admin-only** — `POST /challenge/admin/challenge`
   sits behind the admin routes, so a challenge fixture needs a `cos_admin`
   token, while a discussion needs only a consumer.
5. **S3 attachments** would need their own teardown if a fixture used them;
   plain discussions and challenges do not.

So a create/teardown phase here is a real option, where for the sandbox it is
not. It is simply more work than the read-only check, and the read-only check
already covers "is this service running as expected".

## 4. Compared with the sandbox

Same shape of service, very different position:

| | Sandbox | Community layer |
|---|---|---|
| Health route | `/v1/health` — literal `{"status":"ok"}`, checks nothing | `/healthz` — checks Postgres **and** S3, 500 when either is down |
| Anonymous read of real data | none — every API route needs a token | yes, per product (`public_path`) |
| Auth side effect | provisions namespace + Profile + 50Gi PVC | inserts one `users` row |
| Can its own creations be deleted? | **no** — no profile/namespace/PVC delete route exists | **yes** — content is hard-deleted, ownership-checked |
| Create/teardown feasible? | no, blocked upstream | yes, with a fixture account |

## 5. Options, when we come back to this

**A — what ships today.** Health + per-product public read + per-product
refusal. Zero footprint, zero credentials, both products covered.

**B — add an authenticated read** using a **dedicated long-lived account**,
never the run's throwaway user, so the one `users` row it creates is created
once and reused instead of orphaned per run. `community.bearer_token` already
exists in the config for this and is null by default; it adds a 200 assertion on
each service's `verify_path`. **Recommended next step.**

**C — full create / validate / teardown.** Create a discussion as a fixture
account, read it back, delete it; optionally a challenge via a `cos_admin`
token. Residue per run is then one `DeletedDiscussion` row. Needs §3 items 1, 3
and 4 settled.

## 6. Open questions

1. **Is there a fixed community-layer test account on dev?** Same question as
   the sandbox, and one authenticated call with it would settle whether a token
   from our `iudx-v2` realm satisfies `KEYCLOAK_AUDIENCE`/`KEYCLOAK_ISSUER`.
2. **Who owns the 500 on `/challenge/users/challenges`** (§2.3)?
3. **Should `/healthz` cover Redis?** It is on the auth path but unchecked (§2.4).

## 7. Re-verifying these findings

Pinned to the commit in the header; re-check after any community-layer release.

```bash
# the live deployment — health, both products, both halves
B=https://v2.dev.iudx.io/community
curl -s $B/healthz                             # 200, PostgreSQL DB + AWS S3 true
curl -s $B/discussion/tags/popular             # 200, real data
curl -s $B/discussion/recent/bookmarked        # 401
curl -s $B/challenge/all                       # 200, real data
curl -s $B/challenge/participated              # 401
curl -s $B/challenge/users/challenges          # 500 as of 2026-09-10 (§2.3)

# does anything delete a user yet?
cd <dx-community-layer> && git checkout dev
grep -rn "delete" src/routes/ | grep -i user   # expected: only the bookmark DELETE

# the harness phase
python3 main/e2e.py --only "08 community"
```

## 8. Source references

All paths in `github.com/datakaveri/dx-community-layer` at `dev` @ `2a384ac`.

| What | Where |
|---|---|
| Router mounting, gated on `ACTIVATED_SERVICES` | `src/main.py` |
| `/`, `/healthz` — Postgres + S3 checks | `src/routes/utility.py` |
| Strict vs public bearer, and the user-row insert | `src/middlewares/authorization.py` |
| `update_user_info` — the insert with no counterpart | `src/middlewares/authorization.py` |
| Discussion routes (`/discussion`, plus admin/search/comments/attachment) | `src/routes/discussion/` |
| Challenge routes (`/challenge`, plus admin/users/submission/attachment) | `src/routes/challenge/` |
| Hard delete + `DeletedDiscussion` audit row | `src/services/discussion/discussion_services.py` |
| `ROOT_PATH`, `ACTIVATED_SERVICES`, Keycloak audience/issuer | `src/configs/env_config.py` |
