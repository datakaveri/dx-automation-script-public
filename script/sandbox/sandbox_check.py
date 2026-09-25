#!/usr/bin/env python3
"""
Check that the sandbox server is up and enforcing authentication.

The sandbox (`datakaveri/sandbox-connect-api`) looks like the other servers the
harness verifies and is not one: it is a notebook/compute service, not a
catalogue resource server. Three things follow from that, and they are the whole
reason this is a phase of its own rather than a fifth `resource_servers` entry:

  * **It knows nothing about catalogue items.** Every route is
    `/v1/notebook/...`, `/v1/bookings/...`, `/v1/profile/create` — none of them
    takes an item id, in the path or the query. `verify_path` in this harness is
    item-addressed by construction, so there is nothing for it to address.

  * **It wants an identity token, not a resource one.** `authMiddleware` reads
    the JWT's `azp` and refuses anything whose authorized party is not the
    client named by `API_KEYCLOAK_CLIENT_ID`, then requires `email_verified`
    and — when `API_KYC_ENABLED` is set — `kyc_verified`. The item-scoped token
    phase 05 mints comes from ControlPlane, not from that client, so a granted
    read there could never return 200.

  * **It leaves nothing behind.** No exchange, no collection, no databank, so
    no teardown branch has anything to remove.

So what is checked here is that the service is *running as expected*, which for
a server the harness cannot authenticate against means two things, both of which
need no credentials at all:

    GET /v1/health          -> 200, {"status":"ok","version":"v1"}
    GET /v1/notebook/list   -> 401, with no Authorization header

The first proves the process is up and serving; the second proves the auth
middleware is actually in front of the API, which a health check alone does not
say — `/v1/health` is deliberately registered *outside* `authMiddleware`, so it
answers 200 even on a build whose auth is broken or absent.

Both routes are registered identically on `stable/v2.3` and on `dev` (their
`cmd/api/router.go` are byte-identical), which is what makes this pair safe to
assert against either deployment. The branches differ only in a blocked-email-
domain check inside the middleware, and that sits behind the token parse — an
unauthenticated request never reaches it, so the 401 above is the same on both.

The optional third step turns "the door is locked" into "the right key opens
it": the same auth-controlled route, read *with* a token, required to answer
200. `bearer_token` supplies one directly; `token_user` uses the identity token
of a user this run creates. Both are null by default, because whether either
works is a property of the deployment — the middleware wants the token's `azp`
to match its own client id, and the account to be email- and (when
`API_KYC_ENABLED`) KYC-verified.

That read is safe to make. The sandbox middleware validates the token and writes
nothing — unlike the community layer's, which inserts a user row — and
listNotebooks only reads. The heavy provisioning (namespace, Kubeflow profile,
50Gi PVC) happens on notebook *create*, which nothing here calls. When the token
is refused, the sandbox's own message says which requirement failed, and
TOKEN_HINTS turns that into the reason.
"""

from ControlPlane_Workflow.client import ApiClient

# What /v1/health reports when the service is healthy. The handler is a literal
# `{"status": "ok", "version": app.env.Version}`, so this is an exact match
# rather than a prefix or a truthiness check.
HEALTHY = "ok"


def base_url(config):
    """The sandbox base URL, whether or not `url` carries a scheme.

    Same rule as `flow.server_base` applies to a resource server: configs get
    written both ways, and prepending a scheme to a URL that has one produces
    `https://https://…`, which fails as an unresolvable host rather than as an
    obvious mistake. Duplicated rather than imported because `flow` imports this
    module, not the other way round.
    """
    sandbox = config["sandbox"]
    url = str(sandbox.get("url") or "")
    if url.startswith(("http://", "https://")):
        return url.rstrip("/")
    return f"{sandbox.get('scheme') or 'https'}://{url}".rstrip("/")


def _client(ctx):
    sandbox = ctx.config["sandbox"]
    client = ApiClient(
        base_url(ctx.config),
        ctx.recorder,
        timeout=sandbox.get("timeout_seconds") or 30,
        verify_tls=bool(sandbox.get("verify_tls", True)),
    )
    return client


def check_health(ctx, client):
    """GET the health route. Returns the version it reported.

    Raises AssertionError when the body does not say the service is healthy —
    a 200 carrying `{"status":"degraded"}` is a pass by status and a failure in
    fact, which is the case a status-only check would wave through.
    """
    sandbox = ctx.config["sandbox"]
    path = sandbox["health_path"]

    payload = client.get(path, "sandbox: health", expect=(200,))

    expected = sandbox.get("expect_status")
    if not expected:
        # The body check is switched off; the 200 is the whole assertion.
        return _version(payload)

    status = payload.get("status") if isinstance(payload, dict) else None
    if str(status).lower() != str(expected).lower():
        raise AssertionError(
            f"sandbox {path} returned 200 but reported status "
            f"{status!r}, expected {expected!r} — the service is answering "
            f"without being healthy"
        )
    return _version(payload)


def _version(payload):
    if isinstance(payload, dict) and payload.get("version"):
        return str(payload["version"])
    return None


def check_auth_enforced(ctx, client):
    """Call an auth-controlled route with no token and require a refusal.

    This is what the health route cannot tell you. `/v1/health` is registered on
    the root mux, outside `authMiddleware`, precisely so a monitor can reach it
    — so it answers 200 whether or not the API behind it is protected. Asking an
    API route without credentials is the check that the middleware is in place.

    Returns the status it was refused with, or None when the check is off.
    """
    sandbox = ctx.config["sandbox"]
    expect = tuple(sandbox.get("expect_denied") or ())
    if not expect:
        return None

    path = sandbox["verify_path"]
    # No token argument: the point is the missing Authorization header.
    client.get(path, "sandbox: unauthenticated request refused", expect=expect)
    return ctx.recorder.entries[-1]["status"] if ctx.recorder.entries else None


# What the sandbox says when it refuses a token, and what each answer means.
# The middleware checks in this order and returns a distinct message for each,
# so a single failed call says exactly which requirement was not met — which is
# far more useful than "401", and is why the body is quoted back on failure.
TOKEN_HINTS = (
    ("invalid token", "the token is not signed by the Keycloak this sandbox "
                      "trusts (its API_KEYCLOAK_PUBLIC_KEY / realm differs)"),
    ("invalid client", "the Keycloak is right but the client is not: the token's "
                       "`azp` must equal the sandbox's API_KEYCLOAK_CLIENT_ID"),
    ("token expired", "the token had already expired when it was sent"),
    ("email is not verified", "the account is not email-verified"),
    ("kyc is not verified", "API_KYC_ENABLED is on and the account has no "
                            "kyc_verified claim — see script/user_creation for "
                            "how that attribute is set"),
    ("not allowed for this email domain", "the account's email domain is in "
                                          "API_BLOCKED_EMAIL_DOMAINS"),
)


def _explain(error):
    """Turn the sandbox's refusal into the reason behind it, where we know it."""
    text = str(error).lower()
    for needle, meaning in TOKEN_HINTS:
        if needle in text:
            return meaning
    return None


def _auth_token(ctx):
    """The token for the authenticated read: an explicit one, or a run user's.

    Returns (token, description), or (None, reason) when there is nothing to
    send — which is not a failure, just a check that stands down.
    """
    config = ctx.config["sandbox"]

    token = str(config.get("bearer_token") or "").strip()
    if token:
        return token, "configured bearer_token"

    key = config.get("token_user")
    if not key:
        return None, None

    user = ctx.users.get(key)
    if user is None:
        return None, f"sandbox.token_user names {key!r}, which this run has no user for"
    if not user.user_id:
        # Phase 00 never ran — `--only "07 sandbox"`, most likely.
        return None, (
            f"sandbox.token_user is {key!r} but that user does not exist yet; "
            f"phase 00 has not run (set sandbox.bearer_token to test this on "
            f"its own)"
        )

    client_id = config.get("token_client_id")
    if client_id:
        # The token's `azp` is the client it was minted with, and the sandbox
        # accepts only its own API_KEYCLOAK_CLIENT_ID. The harness's usual
        # client is not necessarily that one, so the token is re-minted with
        # the client the sandbox expects rather than reused.
        token = ctx.kc.user_token(user.username, user.password, client_id=client_id)
        return token, f"identity token of {key} ({user.username}) via {client_id}"

    if not user.token:
        return None, f"sandbox.token_user is {key!r} but that user has no token yet"
    return user.token, f"identity token of {key} ({user.username})"


def check_authenticated_read(ctx, client):
    """Read the protected route *with* a token, and require 200.

    This is the half that proves the API works rather than merely that it is
    guarded. It is safe to run: the sandbox middleware validates the token and
    writes nothing (unlike the community layer's, which inserts a user row), and
    listNotebooks only reads. Nothing is provisioned — that happens on notebook
    create, which the harness never calls.

    Returns the status, or None when no token was available.
    """
    token, description = _auth_token(ctx)
    if not token:
        if description:
            print(f"    sandbox: authenticated read skipped — {description}", flush=True)
        return None

    path = ctx.config["sandbox"]["verify_path"]
    print(f"    sandbox: authenticated read as {description}", flush=True)
    try:
        client.get(path, "sandbox: authenticated read", token=token, expect=(200,))
    except Exception as error:  # noqa: BLE001 - re-raised with the reason attached
        meaning = _explain(error)
        if meaning:
            raise AssertionError(
                f"sandbox refused the token for GET {path}: {meaning}.\n"
                f"      the server's own answer: {error}"
            ) from error
        raise
    return ctx.recorder.entries[-1]["status"] if ctx.recorder.entries else None


def check_sandbox(ctx):
    """Run every enabled sandbox check. Returns a one-line result summary."""
    client = _client(ctx)
    target = base_url(ctx.config)

    version = check_health(ctx, client)
    parts = [f"health:ok{f' ({version})' if version else ''}"]

    denied = check_auth_enforced(ctx, client)
    parts.append(f"unauthenticated:{denied}" if denied else "unauthenticated:n/a (check off)")

    allowed = check_authenticated_read(ctx, client)
    if allowed:
        parts.append(f"authenticated:{allowed}")

    return f"{target} — " + ", ".join(parts)
