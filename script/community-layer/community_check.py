#!/usr/bin/env python3
"""
Check the community-layer server: discussion and challenge.

`datakaveri/dx-community-layer` is a FastAPI service carrying two independent
products behind one process — Discussion and Challenge — mounted at `/discussion`
and `/challenge`. Like the sandbox it is not a catalogue resource server: no
route takes an item id, and its authoriser validates a Keycloak *identity* token
against `KEYCLOAK_AUDIENCE`/`KEYCLOAK_ISSUER`, not the item-scoped token phase 05
mints. So it is checked here rather than declared on a catalogue item.

Unlike the sandbox, it gives us something genuinely worth asserting without any
credentials at all, because it has three kinds of route:

    /healthz                         no auth, and it checks its own dependencies
    a public read   (`public_path`)  token optional — serves real data to anyone
    a strict read   (`verify_path`)  token required — 401 without one

`/healthz` is the good one. It is not a liveness stub: `routes/utility.py` runs
`SELECT 1` against Postgres and `head_bucket` against S3, returns 200 only when
both pass, and 500 with a per-dependency map when either fails. So it answers
"is this service actually able to work", which is more than most health routes
do — and this module asserts the map rather than the status alone, so a failure
says *which* dependency is down instead of just "500".

The public/strict pair is checked per service, because
`ACTIVATED_SERVICES` decides which routers `main.py` mounts at all: a deployment
running Discussion only answers 404 on every `/challenge` route. `services.*.
enabled` mirrors that, so the harness asserts what a deployment actually runs.

**Nothing here creates anything.** That is deliberate and worth keeping: the
authoriser has a side effect — `update_user_info` inserts a `User` row into the
discussion and/or challenge database on *every authenticated request*, and no
route deletes a user. An unauthenticated call never reaches it (the public
bearer returns early with `user_id=None`; the strict one raises 401 first), so
these checks leave no row behind. `bearer_token` is the opt-in that trades that
guarantee for a stronger assertion — see docs/community-layer.md.
"""

from ControlPlane_Workflow.client import ApiClient

# The two products the service can mount, and the order they are reported in.
SERVICES = ("discussion", "challenge")


def base_url(config):
    """The community-layer base URL, whether or not `url` carries a scheme.

    Deployments run it behind a proxy subpath (`ROOT_PATH`, `/community` on
    dev), so the configured url normally carries a path as well as a host.
    """
    community = config["community"]
    url = str(community.get("url") or "")
    if url.startswith(("http://", "https://")):
        return url.rstrip("/")
    return f"{community.get('scheme') or 'https'}://{url}".rstrip("/")


def _client(ctx):
    community = ctx.config["community"]
    return ApiClient(
        base_url(ctx.config),
        ctx.recorder,
        timeout=community.get("timeout_seconds") or 30,
        verify_tls=bool(community.get("verify_tls", True)),
    )


def check_health(ctx, client):
    """GET /healthz and require every dependency it reports to be up.

    The route answers 200 only when Postgres and S3 both pass and 500 otherwise,
    so the status alone would catch an outage. The dependency map is asserted
    anyway because it names the thing that broke: "AWS S3 unreachable" is a
    different call-out from "the database is down", and a 500 says neither.

    Returns the dependency map as reported.
    """
    community = ctx.config["community"]
    path = community["health_path"]

    payload = client.get(path, "community: health", expect=(200,))

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        # A 200 with no dependency map: nothing to assert beyond the status,
        # which the expect=(200,) above already did.
        return {}

    required = community.get("expect_dependencies")
    # null/empty means "whatever the service reports, all of it must be up".
    names = list(required) if required else list(data)

    missing = [name for name in names if name not in data]
    if missing:
        raise AssertionError(
            f"community {path} did not report on {', '.join(missing)} — "
            f"it reported {', '.join(sorted(data)) or 'nothing'}. Either the "
            f"service changed its health payload or community.expect_dependencies "
            f"names something it does not check"
        )

    down = [name for name in names if not data.get(name)]
    if down:
        raise AssertionError(
            f"community {path} returned 200 but reports {', '.join(down)} "
            f"unhealthy — the service is answering without being able to work"
        )
    return data


def check_service(ctx, client, name):
    """Check one mounted product: a public read, then a refusal.

    The public read is the positive half — it proves the router is mounted and
    the product is serving real data out of its own database. The refusal is the
    negative half, and it is the one that cannot be faked: a public 200 says
    nothing about whether the protected routes are protected.

    Returns a one-line summary, or None when this service is switched off.
    """
    service = ctx.config["community"]["services"].get(name) or {}
    if not service.get("enabled"):
        return None

    parts = []

    public_path = service.get("public_path")
    if public_path:
        payload = client.get(public_path, f"community/{name}: public read", expect=(200,))
        # Every response is the service's own envelope; `success` false inside a
        # 200 is a shape this API does use, so the status is not the whole story.
        if isinstance(payload, dict) and payload.get("success") is False:
            raise AssertionError(
                f"community/{name} GET {public_path} returned 200 but the body "
                f"says success=false: {payload.get('message')!r}"
            )
        parts.append(f"public:{_last_status(ctx)}")

    expect = tuple(service.get("expect_denied") or ())
    verify_path = service.get("verify_path")
    if expect and verify_path:
        # No token: the point is the missing Authorization header.
        client.get(verify_path, f"community/{name}: unauthenticated refused", expect=expect)
        parts.append(f"unauthenticated:{_last_status(ctx)}")
    else:
        parts.append("unauthenticated:n/a (check off)")

    if ctx.config["community"].get("bearer_token") and verify_path:
        token = str(ctx.config["community"]["bearer_token"]).strip()
        client.get(
            verify_path, f"community/{name}: authenticated read", token=token, expect=(200,)
        )
        parts.append(f"authenticated:{_last_status(ctx)}")

    return f"{name} " + " ".join(parts)


def _last_status(ctx):
    return ctx.recorder.entries[-1]["status"] if ctx.recorder.entries else "?"


def check_community(ctx):
    """Run every enabled community-layer check. Returns a one-line summary."""
    client = _client(ctx)
    target = base_url(ctx.config)

    dependencies = check_health(ctx, client)
    healthy = ", ".join(sorted(dependencies)) if dependencies else "no dependency map"
    parts = [f"health:ok ({healthy})"]

    for name in SERVICES:
        summary = check_service(ctx, client, name)
        parts.append(summary if summary else f"{name} skipped (disabled)")

    return f"{target} — " + " | ".join(parts)
