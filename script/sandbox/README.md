# Sandbox server checks

Phase 07 of the E2E harness. Checks that `sandbox-connect-api` — the
notebook/compute service, `github.com/datakaveri/sandbox-connect-api` — is up
and enforcing authentication.

```
sandbox_check.py    the checks, imported by the harness as `sandbox_check`
```

Not part of `ControlPlane_Workflow`: the sandbox is a separate deployment with
its own repository and its own Keycloak client, so its checks sit here beside
the other per-server automation. The harness puts this directory on the import
path (`ControlPlane_Workflow/__init__.py`) and calls `check_sandbox(ctx)`.

## Run it

```bash
python3 main/e2e.py --only "07 sandbox"
```

Configured under `sandbox` in `main/config.json`; `enabled: false` skips it.

## What it checks

| Call | Asserts |
|---|---|
| `GET /v1/health` | 200, and the body says `status: "ok"` |
| `GET /v1/notebook/list` with no `Authorization` | 401 / 403 |
| `GET /v1/notebook/list` **with** a token *(optional)* | 200 |

The refusal is the one that earns its place: `/v1/health` is registered
*outside* the auth middleware, so it answers 200 even on a build whose auth is
broken. Only an unauthenticated call to a real API route shows the middleware
is there.

The authenticated read is optional and creates nothing — the middleware
validates the token without writing, and the listing only reads. Provisioning
(namespace, Kubeflow profile, 50Gi PVC) happens on notebook *create*, which this
never calls.

## The client id matters

The sandbox refuses any token whose `azp` is not its own
`API_KEYCLOAK_CLIENT_ID`, and that is not the client the rest of the harness
signs in with. On dev:

| Client | Result |
|---|---|
| `frontend-client` (the harness default) | 401 `Invalid client` |
| `angular-client` | 200 |

So `sandbox.token_client_id` re-mints the run user's token with the client the
sandbox expects. `azp` is a property of how a token was minted, not something
that can be added to one afterwards.

When a token is refused the sandbox says which requirement failed — wrong
signer, wrong client, unverified email, missing KYC — and `TOKEN_HINTS` turns
that into the reason.

## Why creation and teardown are not here

The sandbox has no delete route for the namespace, Kubeflow profile or 50Gi
workspace PVC that creating a notebook provisions, and the namespace is named
after the caller's Keycloak id. See **[../../docs/sandbox.md](../../docs/sandbox.md)**.
