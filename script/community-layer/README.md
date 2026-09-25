# Community layer checks

Phase 08 of the E2E harness. Checks that `dx-community-layer` —
`github.com/datakaveri/dx-community-layer` — is healthy and that both products
it carries, Discussion and Challenge, serve and refuse correctly.

```
community_check.py    the checks, imported by the harness as `community_check`
```

Not part of `ControlPlane_Workflow`: the community layer is a separate
deployment with its own repository, so its checks sit here beside the other
per-server automation. The harness puts this directory on the import path
(`ControlPlane_Workflow/__init__.py`) and calls `check_community(ctx)`.

The directory name carries a hyphen, which a Python package name cannot — hence
the module inside it is `community_check`, and it is the directory rather than
the package that goes on the path.

## Run it

```bash
python3 main/e2e.py --only "08 community"
```

Configured under `community` in `main/config.json`; `enabled: false` skips it.

## What it checks

| Call | Asserts |
|---|---|
| `GET /healthz` | 200, **and** every dependency it reports is up |
| `GET /discussion/tags/popular` | 200 |
| `GET /discussion/recent/bookmarked`, no auth | 401 / 403 |
| `GET /challenge/all` | 200 |
| `GET /challenge/participated`, no auth | 401 / 403 |

`/healthz` is not a liveness stub: it runs `SELECT 1` against Postgres and
`head_bucket` against S3, answering 200 only when both pass. The check asserts
the per-dependency map rather than the status, so a failure names what broke.

## Two products, two switches

The service mounts each product only when it appears in its
`ACTIVATED_SERVICES`, so a Discussion-only deployment answers **404, not 401**,
on every `/challenge` route. The config mirrors that:

```json
"services": {
  "discussion": { "enabled": true,  ... },
  "challenge":  { "enabled": false, ... }
}
```

Either can be turned off independently; at least one must stay on, or the phase
is only a health check and startup says so.

## Why every call is unauthenticated

The authoriser does more than validate. On every *authenticated* request
`update_user_info` inserts a `User` row into the discussion and/or challenge
database, and **no route deletes a user**. With this harness's throwaway user
that is one orphan row per run, per database, keyed to a Keycloak id that no
longer exists.

An unauthenticated call never reaches that code, so these checks leave nothing
behind — deliberately. `community.bearer_token` opts in, and should name a
fixed long-lived account rather than the run's own user. See
**[../../docs/community-layer.md](../../docs/community-layer.md)**.
