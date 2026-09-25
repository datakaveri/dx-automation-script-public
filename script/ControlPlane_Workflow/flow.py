#!/usr/bin/env python3
"""
The onboarding workflow, one function per phase.

    00  Sign in            requester, cos admin, consumer, consumer-no-policy
    01  Org onboarding     submit -> list -> approve -> refresh token -> confirm
    02  Catalogue          create item (RS array) -> publish ACTIVE (COS)
    03  Access request     consumer request -> provider approve -> policy
    04  NGSI-LD publish    records -> the item's own exchange (NGSI-LD items only)
    05  Resource servers   mint token -> GET per server (200) | no-policy (403)
    06  Auditing           consumer / cos admin trails
    07  Sandbox            health + unauthenticated refusal (no lane, no item)
    08  Community          healthz + per-service public read / refusal

Phases 01 to 03 and 05 run once per *lane* — a provider, their organisation and
the one item they own. A run has one lane, and two when the gateway server is
enabled alongside NGSI-LD: the broker user the catalogue creates is named after
the item's provider, so a single provider owning both kinds of item leaves one
broker user for two teardowns to delete, and the second finds it already gone.
See gateway_needs_own_provider.

Every artefact is named with the run namespace, which is what lets teardown
sweep deterministically instead of tracking ids that go missing when a phase
fails partway through.
"""

import base64
import json
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .client import ApiClient, field, rows_of, scrub
from . import gateway_adaptor
from .file_server import file_upload
from .ogc_raster import ogc_raster_create
from .ogc_vector import ensure_provider_role, ogc_vector_create
from .config import (
    GATEWAY_TYPES,
    NGSILD_TYPES,
    enabled_resource_servers,
    file_server,
    gateway_server,
    is_file_server,
    is_ogc_raster,
    is_ogc_vector,
    ngsild_server,
    ogc_raster_server,
    ogc_vector_server,
)
from .kc import ROLES_AFTER_ORG_APPROVAL, Keycloak
from .ngsild_publish import ngsild_publish
# Not package modules: these two servers are separate deployments, so their
# checks live in script/sandbox/ and script/community-layer/, which the
# package puts on the import path (see __init__.py).
from sandbox_check import check_sandbox
from community_check import check_community

# Both roles are granted together by the org-create approval, per
# OrganizationLifecycleServiceImpl.java:168-169 — so the org admin can onboard
# catalogue items without any join or provider-role flow.
ORG_APPROVAL_ROLES = ROLES_AFTER_ORG_APPROVAL

# The OGC record table stores provider contacts as
# {"providerOrg":"<name>","additionalInfoURL":"<org uuid>"} in a varchar(100)
# column: 40 characters of JSON plus a 36-character uuid leave 24 for the
# organisation name. A longer one fails onboarding with 22001 "value too long",
# which the process reports only as "Failed to onboard the collection in db." —
# so the name is kept short here rather than debugged there again.
ORG_NAME_LIMIT = 24

# How long to wait for Elasticsearch to make a newly created item searchable.
# The write path checks existence with a search, so this is a refresh-interval
# race rather than a fixed, knowable delay.
ITEM_INDEX_TIMEOUT = 60


class User:
    def __init__(self, key, username, email, password):
        self.key = key
        self.username = username
        self.email = email
        self.password = password
        self.user_id = None
        self.token = None

    def __repr__(self):
        return f"<User {self.key} {self.username}>"


def gateway_needs_own_provider(config):
    """Whether the gateway item has to be owned by a provider of its own.

    The catalogue names the RabbitMQ user it creates after the item's *provider*
    — their Keycloak id — not after the item. So one provider owning both an
    NGSI-LD item and a gateway one gets a single broker user standing for both,
    and teardown then has two scripts queued to delete it: the first removes it
    and the second is left looking for a user that is no longer there.

    Giving the gateway item a provider of its own gives each teardown its own
    user to delete. Only NGSI-LD and gateway items get a broker user at all, so
    nothing else here forces a second provider — with one of the two enabled the
    run stays as it was, one provider owning one item.
    """
    return bool(ngsild_server(config)) and bool(gateway_server(config))


class Lane:
    """A provider, their organisation, and the one catalogue item they own.

    A run has one lane, and two when the gateway item needs a provider of its
    own (see gateway_needs_own_provider). Everything a phase records about "the
    item" lives here, so a second item is a second lane rather than a second
    copy of the flow.
    """

    def __init__(self, key, provider, servers, item_name, org_name):
        self.key = key
        self.provider = provider
        # The resource servers this lane's item declares: [(key, server)], the
        # same shape enabled_resource_servers returns.
        self.servers = servers
        self.item_name = item_name
        self.org_name = org_name

        # Populated as the flow proceeds; consumed by teardown in reverse order.
        self.org_request_id = None
        self.org_id = None
        self.item_id = None
        # resourceServer entries as the catalogue returned them for the created
        # item ({"type", "query_types"}, lowercased). None until phase 02 reads
        # the item back, and stays None if the response did not carry them.
        self.item_servers = None
        self.access_request_id = None
        self.policy_ids = []

    @property
    def suffix(self):
        """What to append to a call label or result key to name this lane.

        Empty for the only lane a single-item run has, so its report reads
        exactly as it always did.
        """
        return "" if self.key == "primary" else f" ({self.key})"

    def server_keys(self):
        return [key for key, _ in self.servers]

    def __repr__(self):
        return f"<Lane {self.key} {self.item_name}>"


class RunContext:
    """Everything a phase needs, plus the ids teardown will need afterwards."""

    def __init__(self, config, recorder):
        self.config = config
        self.recorder = recorder

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        token = secrets.token_hex(2)
        self.namespace = f"{config['run']['prefix']}-{stamp}-{token}"
        # Kept for org_name, which has to fit in 24 characters.
        self._stamp, self._token = stamp, token

        timeout = config["control_plane"]["timeout_seconds"]
        self.cp = ApiClient(config["control_plane"]["base_url"], recorder, timeout)
        self.acl = ApiClient(config["acl"]["base_url"], recorder, timeout)
        self.kc = Keycloak(config, recorder)

        password = config["run"]["user_password"]
        domain = config["run"]["email_domain"]
        # A cos_admin is created only when the config does not name an existing
        # one. Keeping it in `users` means the same namespace prefix, the same
        # sweep, and the same id resolution cover it with no special cases.
        self.owns_cos_admin = not config["cos_admin"].get("username")
        keys = ("requester", "consumer", "nopolicy")
        if self.owns_cos_admin:
            keys += ("cosadmin",)
        # A provider for the gateway item, when it may not share the requester's
        # broker user.
        self.split_gateway = gateway_needs_own_provider(config)
        if self.split_gateway:
            keys += ("gwrequester",)
        # Username and email are deliberately the same string. Realms with
        # registrationEmailAsUsername set (dev has it) overwrite the username
        # with the email on create, so a distinct username would be silently
        # discarded — the user could then not be read back, and the password
        # grant would reject the login. Using the email for both works whether
        # or not the realm has that setting, and still carries the namespace
        # prefix that teardown sweeps on.
        self.users = {
            key: User(
                key,
                f"{self.namespace}-{key}@{domain}",
                f"{self.namespace}-{key}@{domain}",
                password,
            )
            for key in keys
        }

        # The COS admin already exists on the platform; never namespaced, never swept.
        self.cos_admin_token = None

        # One lane per provider, each owning one catalogue item. The gateway
        # server is split into its own lane only when it would otherwise share a
        # broker user with an NGSI-LD item.
        servers = enabled_resource_servers(config)
        if self.split_gateway:
            gateway_key = gateway_server(config)[0]
            primary_servers = [(k, s) for k, s in servers if k != gateway_key]
            gateway_servers = [(k, s) for k, s in servers if k == gateway_key]
        else:
            primary_servers, gateway_servers = servers, []

        self.lanes = [
            Lane(
                "primary",
                self.users["requester"],
                primary_servers,
                self._item_name(""),
                self._org_name(""),
            )
        ]
        if gateway_servers:
            self.lanes.append(
                Lane(
                    "gateway",
                    self.users["gwrequester"],
                    gateway_servers,
                    self._item_name("gw"),
                    self._org_name("gw"),
                )
            )

        # Set when OGC onboarding fails, so teardown can leave the wreckage for
        # inspection instead of deleting the evidence.
        self.ogc_onboarding_failed = False
        # Provider id this run inserted into the OGC database's roles table, if
        # any — teardown removes only what it created.
        self.ogc_role_user = None
        # Keycloak id of a borrowed OGC provider, if one is configured. Teardown
        # and the sweep check this and refuse to delete it.
        self.ogc_provider_sub = None
        # Same, for a cos_admin borrowed from the platform rather than created.
        self.borrowed_cos_admin_id = None
        # Object keys uploaded into the item's databank, for teardown.
        self.file_keys = []
        # clientId/clientSecret per user key: registered once, reused for every
        # token that user mints.
        self.client_credentials = {}
        self.results = {}

    def _org_name(self, tag):
        """One lane's organisation name, short enough for the OGC record table.

        Namespaced like everything else — teardown sweeps on the prefix — but
        capped at ORG_NAME_LIMIT. The prefix is kept whole and the run's unique
        part is trimmed, so two runs still differ and the sweep still matches.
        `tag` is what keeps one lane's organisation apart from another's.
        """
        tail = f"-{tag}org" if tag else "-org"
        full = f"{self.namespace}{tail}"
        if len(full) <= ORG_NAME_LIMIT:
            return full

        prefix = self.config["run"]["prefix"]
        room = ORG_NAME_LIMIT - len(prefix) - 1 - len(tail)
        unique = f"{self._stamp[-6:]}{self._token}"[-room:] if room > 0 else ""
        return f"{prefix}-{unique}{tail}" if unique else f"{prefix}{tail}"

    def _item_name(self, tag):
        return f"{self.namespace}-{tag}-item" if tag else f"{self.namespace}-item"

    # The primary lane's artefacts, under the names the rest of the flow already
    # used for them. Everything that is genuinely per-item — the phases, and
    # teardown — walks ctx.lanes instead.

    @property
    def primary(self):
        return self.lanes[0]

    @property
    def gateway_lane(self):
        """The lane owning the gateway item, when it has one of its own."""
        for lane in self.lanes:
            if lane.key == "gateway":
                return lane
        return None

    @property
    def org_name(self):
        return self.primary.org_name

    @property
    def item_name(self):
        return self.primary.item_name

    @property
    def org_id(self):
        return self.primary.org_id

    @property
    def org_request_id(self):
        return self.primary.org_request_id

    @property
    def item_id(self):
        return self.primary.item_id

    @property
    def item_servers(self):
        return self.primary.item_servers

    @property
    def access_request_id(self):
        return self.primary.access_request_id

    @property
    def policy_ids(self):
        return self.primary.policy_ids

    def user(self, key):
        return self.users[key]


def _log(message):
    print(f"    {message}", flush=True)


# --------------------------------------------------------------- 00 sign in


def phase_00_signin(ctx):
    """Create the test users in Keycloak and sign everybody in."""
    for user in ctx.users.values():
        user.user_id = ctx.kc.create_user(user.username, user.email, user.password)
        user.token = ctx.kc.user_token(user.username, user.password)
        _log(f"created {user.username} ({user.user_id})")

    expected = ctx.config["keycloak"]["roles"]["cos_admin"]

    if ctx.owns_cos_admin:
        cos_user = ctx.user("cosadmin")
        ctx.kc.assign_realm_role(cos_user.user_id, expected)
        # The role has to be in the token, and the one minted at creation
        # predates the assignment.
        cos_user.token = ctx.kc.user_token(cos_user.username, cos_user.password)
        ctx.kc.await_roles(cos_user.user_id, [expected])
        ctx.cos_admin_token = cos_user.token
        _log(f"created cos admin {cos_user.username}")
        return

    cos = ctx.config["cos_admin"]
    ctx.cos_admin_token = ctx.kc.user_token(cos["username"], cos["password"])
    existing = ctx.kc.find_user(cos["username"])
    if not existing:
        raise AssertionError(f"cos admin {cos['username']} not found in Keycloak")
    roles = ctx.kc.realm_roles(existing["id"])
    if expected not in roles:
        raise AssertionError(
            f"cos admin {cos['username']} lacks the {expected} role (has: {', '.join(roles)}); "
            "org approval will 403"
        )
    # Remembered so teardown can tell the two halves apart: the account itself
    # is never deleted, while the audit rows it accumulated standing in for the
    # harness are swept with the rest of the run when run.delete_audit_rows is on.
    ctx.borrowed_cos_admin_id = existing["id"]
    _log(f"signed in existing cos admin {cos['username']} ({existing['id']}) — never swept")


# -------------------------------------------------------- 01 org onboarding


def phase_01_organisation(ctx):
    """Submit an org create request, approve it, and confirm the role change.

    Once per lane. The org-create approval is what grants the provider role, so
    a second provider needs a second organisation of its own — there is no join
    flow that would put it in the first one.
    """
    for lane in ctx.lanes:
        _onboard_organisation(ctx, lane)


def _onboard_organisation(ctx, lane):
    """One lane's organisation, from request to confirmed membership."""
    requester = lane.provider

    ctx.cp.post(
        "/iudx/v2/auth/organisations/requests",
        f"submit org create request{lane.suffix}",
        token=requester.token,
        json_body={
            "name": lane.org_name,
            "entity_type": "private",
            "org_sector": "technology",
            "website_link": "https://example.invalid",
            "address": "E2E harness, automated run",
            "certificate_path": "/e2e/cert.pdf",
            "pancard_path": "/e2e/pan.pdf",
            "emp_id": f"E2E{ctx.lanes.index(lane) + 1:03d}",
            "job_title": "Automation",
            # Per provider, not per run: the platform rejects a second request
            # carrying a manager email another one already used, with 409
            # "Manager email is already in use for another organisation
            # request" — so a shared one would let only the first lane onboard.
            "manager_email": f"{ctx.namespace}-{requester.key}-manager@example.invalid",
            "organisation_documents": "/e2e/org.pdf",
        },
    )
    _log(f"submitted org create request for {lane.org_name}")

    lane.org_request_id = _find_org_request_id(ctx, lane)
    _log(f"cos admin sees request {lane.org_request_id}")

    ctx.cp.post(
        "/iudx/v2/auth/organisations/requests/approve",
        f"approve org create request{lane.suffix}",
        token=ctx.cos_admin_token,
        json_body={"req_id": lane.org_request_id, "status": "granted"},
    )
    _log("approved")

    # Approval writes roles into Keycloak; the requester's existing token predates
    # that write, so it must be re-minted before any org-scoped call.
    roles = ctx.kc.await_roles(requester.user_id, ORG_APPROVAL_ROLES)
    _log(f"keycloak roles now: {', '.join(roles)}")

    requester.token = ctx.kc.user_token(requester.username, requester.password)

    attributes = ctx.kc.attributes(requester.user_id)
    org_attr = attributes.get("organisation_id")
    if not org_attr:
        raise AssertionError(
            f"{requester.key} has no organisation_id attribute after approval"
        )
    lane.org_id = org_attr[0] if isinstance(org_attr, list) else org_attr
    _log(f"organisation {lane.org_id}")

    members = ctx.cp.get(
        f"/iudx/v2/auth/organisations/{lane.org_id}/users",
        f"confirm org membership{lane.suffix}",
        token=requester.token,
    )
    ctx.results[f"org_members{lane.suffix}"] = (
        len(members) if isinstance(members, list) else 0
    )


def _find_org_request_id(ctx, lane):
    """Locate our pending request in the COS admin's list, by namespaced name."""
    page = 1
    while page <= 20:
        payload = ctx.cp.get(
            "/iudx/v2/auth/organisations/requests",
            f"list pending org requests{lane.suffix}",
            token=ctx.cos_admin_token,
            params={"status": "pending", "page": page, "size": 100},
        )
        rows = rows_of(payload)
        for row in rows:
            if field(row, "name") == lane.org_name:
                return field(row, "id", "requestId")
        if len(rows) < 100:
            break
        page += 1
    raise AssertionError(f"pending org request {lane.org_name} not visible to cos admin")


# ------------------------------------------------------------ 02 catalogue


def phase_02_catalogue(ctx):
    """Create each lane's item across its resource servers, then publish it."""
    for lane in ctx.lanes:
        _create_item(ctx, lane)


def _create_item(ctx, lane):
    """One lane's catalogue item, created by its own provider and published."""
    requester = lane.provider
    servers = lane.servers

    created = ctx.cp.post(
        "/iudx/v2/cat/item",
        f"create catalogue item{lane.suffix}",
        token=requester.token,
        json_body={
            "name": lane.item_name,
            "type": ["adex:DataBank"],
            "label": "E2E harness item",
            "shortDescription": "Created by the ControlPlane E2E harness.",
            "description": "Automated end-to-end test asset. Safe to delete.",
            "tags": ["e2e", "automated"],
            "accessPolicy": "RESTRICTED",
            "organizationId": lane.org_id,
            "fileFormat": "xlsx",
            "industry": "Testing",
            "yearRange": "2024-2025",
            "uploadFrequency": "Daily",
            "license": "CC-BY 4.0",
            # Without this a RESTRICTED item can never be accessed: the access
            # check reads apdURL off the document itself, not from config.
            "apdURL": ctx.config["acl"]["apd_url"],
            # A non-empty mediaURL is what sets dataUploadStatus true; without it
            # the item stays invisible even once publishStatus is ACTIVE.
            "mediaURL": "https://example.invalid/e2e-sample.xlsx",
            "resourceServer": [
                {
                    "name": server["name"],
                    "type": server["type"],
                    "url": server["url"],
                    "accessTypes": server["access_types"],
                    "queryTypes": server["query_types"],
                    "datasetType": server["dataset_type"],
                }
                for _, server in servers
            ],
        },
    )
    lane.item_id = _extract_id(created)
    _log(
        f"item {lane.item_id} ({lane.item_name}) across {len(servers)} "
        f"resource server(s): {', '.join(lane.server_keys()) or 'none'}"
    )

    _await_item_indexed(ctx, lane)

    # Only cos_admin may set publishStatus (ItemController.java:352). Visibility
    # needs ACTIVE *and* dataUploadStatus true, hence the mediaURL above.
    ctx.cp.patch(
        "/iudx/v2/cat/item",
        f"publish item (cos admin){lane.suffix}",
        token=ctx.cos_admin_token,
        params={"id": lane.item_id},
        json_body={"publishStatus": "ACTIVE"},
    )
    _log("published ACTIVE")

    document = ctx.cp.get(
        "/iudx/v2/cat/item",
        f"get catalogue item{lane.suffix}",
        token=requester.token,
        params={"id": lane.item_id},
    )
    # What the item actually ended up declaring, which is what decides whether
    # phase 04 has an exchange to publish to. Read back rather than assumed: a
    # resourceServer entry the catalogue dropped or rewrote is invisible from
    # the request side.
    lane.item_servers = declared_servers(document)
    if lane.item_servers is not None:
        described = ", ".join(
            entry["type"] + (f" ({'/'.join(entry['query_types'])})" if entry["query_types"] else "")
            for entry in lane.item_servers
        )
        _log(f"item declares resource server(s): {described or 'none'}")


def declared_servers(payload):
    """The resourceServer entries on the item document the catalogue returned.

    Returns a list of {"type", "query_types"} with everything lowercased, or
    None when the response does not carry resourceServer at all — an older
    catalogue, or a projection that drops it. That distinction matters: "no
    NGSI-LD server" and "the API did not say" lead to different decisions.

    Query types come along because OGC vector and STAC share one server type and
    are told apart only by them.
    """
    rows = payload if isinstance(payload, list) else [payload]
    for row in rows:
        if not isinstance(row, dict):
            continue
        servers = field(row, "resourceServer", "resourceServers")
        if servers is None:
            continue
        if isinstance(servers, dict):
            servers = [servers]
        if not isinstance(servers, list):
            continue
        entries = []
        for entry in servers:
            if not isinstance(entry, dict):
                if entry is not None:
                    entries.append({"type": str(entry).lower(), "query_types": []})
                continue
            # `name` is the fallback because a deployment that omits `type`
            # still names the server "NGSI-LD".
            kind = field(entry, "type", "resourceServerType", "name")
            queries = field(entry, "queryTypes", "query_types") or []
            if isinstance(queries, str):
                queries = [queries]
            if kind is not None:
                entries.append({
                    "type": str(kind).lower(),
                    "query_types": [str(q).lower() for q in queries],
                })
        return entries
    return None


def declared_server_types(entries):
    """Just the type strings from `declared_servers` output."""
    return [entry["type"] for entry in entries or []]


def _await_item_indexed(ctx, lane, timeout=ITEM_INDEX_TIMEOUT, interval=2):
    """Wait until Elasticsearch can find the newly created item.

    ItemFetchService.fetchForWrite decides whether an item exists with
    `getItem(request).map(res -> res.getTotalHits() > 0)` — a search, not a
    get-by-id. Searches only see a document after the index refreshes, so a
    just-created item is briefly invisible and the publish PATCH fails with
    "Item not found for update".

    Polling the same read path the writer uses is the reliable signal; a fixed
    sleep would either be too short on a loaded cluster or waste time on an
    idle one.
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        payload = ctx.cp.get(
            "/iudx/v2/cat/item",
            f"wait for item to be searchable{lane.suffix}",
            token=lane.provider.token,
            params={"id": lane.item_id},
            expect=(200, 404),
        )
        if isinstance(payload, list) and payload and payload[0]:
            _log(f"item searchable after {attempt} check(s)")
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"item {lane.item_id} was created but is still not searchable after "
                f"{timeout}s — Elasticsearch never indexed it"
            )
        time.sleep(interval)


def _extract_id(payload):
    """Pull an item id out of the several response shapes the API returns."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("id", "itemId"):
            if payload.get(key):
                return payload[key]
    if isinstance(payload, list) and payload:
        return _extract_id(payload[0])
    raise AssertionError(f"could not find an item id in response: {payload!r}")


# ------------------------------------------------------- 03 access request


def phase_03_access_request(ctx):
    """Consumer requests access; the provider grants it, creating the policy.

    Once per lane: an item is only readable in phase 05 through a policy on that
    item, and only its own provider can approve the request.
    """
    for lane in ctx.lanes:
        _request_access(ctx, lane)


def _request_access(ctx, lane):
    """The consumer's access to one lane's item, from request to policy."""
    consumer = ctx.user("consumer")
    requester = lane.provider

    ctx.acl.post(
        "/iudx/acl/apd/v2/access_request",
        f"create access request (consumer){lane.suffix}",
        token=consumer.token,
        json_body={"itemId": lane.item_id, "requestType": "DOWNLOAD"},
    )
    _log(f"access request created for {lane.item_id}")

    lane.access_request_id = _find_access_request_id(ctx, lane)
    _log(f"provider sees request {lane.access_request_id}")

    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    ctx.acl.put(
        "/iudx/acl/apd/v2/access_request",
        f"approve access request (provider){lane.suffix}",
        token=requester.token,
        json_body={
            "requestId": lane.access_request_id,
            "status": "granted",
            "expiryAt": expiry,
        },
    )
    _log("access request granted")

    policies = ctx.acl.get(
        "/iudx/acl/apd/v2/policy/consumer",
        f"verify policy exists (consumer){lane.suffix}",
        token=consumer.token,
    )
    ours = [p for p in rows_of(policies) if str(field(p, "itemId")) == str(lane.item_id)]
    if not ours:
        raise AssertionError(f"no policy for item {lane.item_id} after approval")
    lane.policy_ids = [
        pid for pid in (field(p, "policyId", "id", "_id") for p in ours) if pid
    ]
    _log(f"policy verified ({len(ours)} row(s))")


def _find_access_request_id(ctx, lane):
    payload = ctx.acl.get(
        "/iudx/acl/apd/v2/access_request/provider",
        f"list access requests (provider){lane.suffix}",
        token=lane.provider.token,
    )
    for row in rows_of(payload):
        if str(field(row, "itemId")) == str(lane.item_id):
            return field(row, "requestId", "id")
    raise AssertionError(
        f"provider cannot see an access request for item {lane.item_id}"
    )


# ------------------------------------------------- 04 ngsi-ld publish


def _onboard_ogc_vector(ctx):
    """Upload and onboard the GeoPackage, so the OGC collection exists.

    Without this, /collections/<item> answers 404: the catalogue item does not
    create a collection, the onboarding job does.
    """
    config = ctx.config["ogc_vector"]
    declared, source = item_declares_ogc_vector(ctx)

    if not config["enabled"]:
        ctx.results["ogc_vector"] = "skipped (ogc_vector.enabled off)"
        # Silence here was worth a wasted run: an OGC item whose collection is
        # never onboarded reads as a 404 in phase 05, which looks like a broken
        # server rather than a step that was switched off.
        _log(
            "OGC onboarding disabled" + (
                f" — but the item IS an OGC vector one per {source}, so phase 05 "
                "will read a collection nothing created (expect 404)"
                if declared else "; nothing to onboard"
            )
        )
        return

    if not declared:
        ctx.results["ogc_vector"] = f"skipped (no OGC vector server per {source})"
        _log("item declares no OGC vector server; nothing to onboard")
        return

    if config["provider_username"]:
        # An established provider, by password grant. Its account already has
        # everything the OGC server expects a provider to have.
        token = ctx.kc.user_token(config["provider_username"], config["provider_password"])
        provider_id = _token_sub(token)
        # Recorded so teardown and the sweep can refuse to touch this account.
        ctx.ogc_provider_sub = provider_id
        who = f"{config['provider_username']} (borrowed, never swept)"
    else:
        # Onboarding is a provider action against the provider's own item, so it
        # runs as the item's owner rather than as the consumer that reads it.
        user = ctx.user(config["token_user"])
        token = (
            user.token if config["token_kind"] == "identity"
            else _resource_token(ctx, user)
        )
        provider_id = user.user_id
        who = f"{config['token_kind']} token of {user.key}"

    _log(f"onboarding {config['gpkg_path']} as collection {ctx.item_id} ({who})")

    if config["ensure_provider_role"]:
        # ri_details.role_id references roles(user_id); a provider this run just
        # created has no row there, and the onboarding process reports that only
        # as "Failed to onboard the collection in db.".
        borrowed = bool(config["provider_username"])
        if provider_id and ensure_provider_role(ctx, provider_id) and not borrowed:
            # Only ever queued for removal when this run created the row for a
            # user this run also created. A borrowed account keeps its row.
            ctx.ogc_role_user = provider_id
            _log(f"added a PROVIDER roles row for {provider_id} in the OGC database")

    try:
        ogc_vector_create(ctx, ctx.item_id, token)
    except Exception:
        # Teardown reads this: what a failed onboarding left behind is the only
        # account of which step broke.
        ctx.ogc_onboarding_failed = True
        raise
    ctx.results["ogc_vector"] = f"collection onboarded -> {ctx.item_id}"

    # The job reports SUCCESSFUL before the collection is necessarily
    # queryable; phase 05 reads it immediately afterwards.
    settle = config["settle_seconds"]
    if settle:
        _log(f"waiting {settle}s for the collection to become queryable")
        time.sleep(settle)


def _onboard_ogc_raster(ctx):
    """Ingest the STAC collection and upload its rasters.

    The mirror of the vector step for an OGC item whose query types include
    STAC: without it /stac/collections/<item>/items is empty and the reads in
    phase 05 prove nothing.
    """
    config = ctx.config["ogc_raster"]
    declared, source = item_declares_ogc_raster(ctx)

    if not config["enabled"]:
        ctx.results["ogc_raster"] = "skipped (ogc_raster.enabled off)"
        _log(
            "STAC onboarding disabled" + (
                f" — but the item IS an OGC raster one per {source}, so phase 05 "
                "will read an empty collection"
                if declared else "; nothing to onboard"
            )
        )
        return

    if not declared:
        ctx.results["ogc_raster"] = f"skipped (no OGC raster server per {source})"
        _log("item declares no OGC raster server; nothing to ingest")
        return

    user = ctx.user(config["token_user"])
    token = (
        user.token if config["token_kind"] == "identity"
        else _resource_token(ctx, user)
    )
    # The same roles-row need as the vector path: this provider is minutes old.
    if ctx.config["ogc_vector"]["ensure_provider_role"] and ensure_provider_role(ctx, user.user_id):
        ctx.ogc_role_user = user.user_id
        _log(f"added a PROVIDER roles row for {user.user_id} in the OGC database")

    _log(f"ingesting STAC collection {ctx.item_id} ({config['token_kind']} token of {user.key})")
    count = ogc_raster_create(ctx, ctx.item_id, token)
    ctx.results["ogc_raster"] = f"{count} raster(s) -> {ctx.item_id}"


def _onboard_file(ctx):
    """Upload a file into the item's databank.

    A file item is a databank with nothing in it until something is uploaded, so
    without this the file server has nothing to serve in phase 05.
    """
    config = ctx.config["file_upload"]
    declared, source = item_declares_file(ctx)

    if not config["enabled"]:
        ctx.results["file_upload"] = "skipped (file_upload.enabled off)"
        _log(
            "file upload disabled" + (
                f" — but the item IS a file one per {source}, so its databank "
                "stays empty"
                if declared else "; nothing to upload"
            )
        )
        return

    if not declared:
        ctx.results["file_upload"] = f"skipped (no file resource server per {source})"
        _log("item declares no file resource server; nothing to upload")
        return

    user = ctx.user(config["token_user"])
    token = (
        user.token if config["token_kind"] == "identity"
        else _resource_token(ctx, user)
    )
    _log(f"uploading {config['file_path']} into databank {ctx.item_id} "
         f"({config['token_kind']} token of {user.key})")

    key = file_upload(ctx, ctx.item_id, token)
    # Teardown deletes exactly what this uploaded.
    ctx.file_keys.append(key)
    ctx.results["file_upload"] = f"{key} -> {ctx.item_id}"

    settle = config["settle_seconds"]
    if settle:
        _log(f"waiting {settle}s for the file server to serve it")
        time.sleep(settle)


def _token_sub(token):
    """The `sub` out of a JWT, without verifying it.

    Only used to name the provider for the OGC roles row — the token itself is
    verified by the server that receives it.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("sub")
    except Exception:  # noqa: BLE001 - a token we cannot read is not fatal here
        return None


def _es_index(ctx, lane=None):
    """The Elasticsearch index the resource server reads for this item.

    Named prefix + exchange, and the exchange is the item id. Nothing in the
    harness creates it — the consumer draining the queue does — so naming it in
    the log is what turns "No data found for this index" from a mystery into a
    thing that can be checked.
    """
    lane = lane or ctx.primary
    return f"{ctx.config['ngsild_delete']['index_prefix']}{lane.item_id}"


def item_declares(ctx, matches, config_server, label, lane=None):
    """Whether a lane's item has a server of some kind, and what said so.

    matches       -- predicate over one {"type", "query_types"} entry
    config_server -- the config lookup answering the same question
    lane          -- which item to ask about; the primary one by default, which
                     is the only one a single-item run has

    The entries the catalogue returned in phase 02 are preferred over the ones
    the config asked for. They agree on a healthy deployment — the item is built
    from the config — and where they do not, the item is what the platform
    created its objects from. A response that carried no resourceServer at all
    leaves the config as the only evidence there is.

    Returns (bool, source) so callers can say which answer they acted on.
    """
    lane = lane or ctx.primary
    wanted = _lane_wants(ctx, lane, config_server)
    if lane.item_servers is None:
        return wanted, "config"

    declared = any(matches(entry) for entry in lane.item_servers)
    if declared != wanted:
        # Exactly the case reading the item back exists to catch.
        _log(
            f"warning: config expects {label} "
            f"{'enabled' if wanted else 'disabled'} on {lane.item_name}, but the "
            f"item declares "
            f"{declared_server_types(lane.item_servers) or 'no resource server'}"
        )
    return declared, "the created item"


def _lane_wants(ctx, lane, config_server):
    """Whether the config put this kind of server on *this* lane's item.

    With the gateway split into a lane of its own the question is no longer just
    whether a server is enabled: asked of the gateway item, "is NGSI-LD enabled"
    is yes while "is it on this item" is no, and it is the second that decides
    what phase 04 publishes and what teardown removes.
    """
    found = config_server(ctx.config)
    return bool(found) and found[0] in lane.server_keys()


def _of_type(types):
    return lambda entry: entry["type"] in types


def item_declares_ngsild(ctx, lane=None):
    """Whether the item is an NGSI-LD one: the only kind with its own exchange,
    so this decides both what phase 04 publishes to and what teardown removes."""
    return item_declares(ctx, _of_type(NGSILD_TYPES), ngsild_server, "NGSI-LD", lane)


def item_declares_gateway(ctx, lane=None):
    """Whether the item is a gateway one, and so needs an adaptor running to be
    readable at all."""
    return item_declares(ctx, _of_type(GATEWAY_TYPES), gateway_server, "gateway", lane)


def item_declares_file(ctx, lane=None):
    """Whether the item is a file one — a databank the file server stores under."""
    return item_declares(ctx, is_file_server, file_server, "file server", lane)


def item_declares_ogc_raster(ctx, lane=None):
    """Whether the item is an OGC *raster* one — OGC with a STAC query type."""
    return item_declares(ctx, is_ogc_raster, ogc_raster_server, "OGC raster", lane)


def item_declares_ogc_vector(ctx, lane=None):
    """Whether the item is an OGC *vector* one.

    OGC vector and STAC share the server type and differ only by query type, so
    this is the one gate that has to look past the type.
    """
    return item_declares(ctx, is_ogc_vector, ogc_vector_server, "OGC vector", lane)


def _es_index(ctx, lane=None):
    """The Elasticsearch index the resource server reads for this item.

    Named prefix + exchange, and the exchange is the item id. Nothing in the
    harness creates it — the consumer draining the queue does — so naming it in
    the log is what turns "No data found for this index" from a mystery into a
    thing that can be checked.
    """
    lane = lane or ctx.primary
    return f"{ctx.config['ngsild_delete']['index_prefix']}{lane.item_id}"


def phase_04_data_onboarding(ctx):
    """Put data behind the item, by whatever mechanism its servers use.

    NGSI-LD is published to an exchange; OGC vector is uploaded and onboarded
    through the processes API. An item declaring both gets both, and each step
    stands down on its own when the item does not declare that kind of server —
    so this is where the next server type is added, not phase 05.
    """
    _onboard_ngsild(ctx)
    _onboard_ogc_vector(ctx)
    _onboard_ogc_raster(ctx)
    _onboard_file(ctx)


def _onboard_ngsild(ctx):
    """Publish records into the item's exchange, so the reads have data.

    The exchange is named with the item id and is created by the catalogue as
    the item is onboarded. Publishing here — before phase 05 reads the data
    plane — is what makes an empty response in that phase mean something: with
    no data published, a broken policy and a working one both return nothing.

    Skipped two ways: `ngsild_publish.enabled` turns it off outright, and an
    item that declares no NGSI-LD resource server has no exchange to publish to,
    so the phase stands down rather than failing. That second check reads the
    types the catalogue returned in phase 02, not the ones the config asked for
    — they agree on a healthy deployment, and where they do not, the item is
    what the exchange was (or was not) created from.
    """
    if not ctx.config["ngsild_publish"]["enabled"]:
        ctx.results["ngsild_publish"] = "skipped (ngsild_publish.enabled off)"
        _log("publishing disabled; the data plane will be read as-is")
        return

    declared, source = item_declares_ngsild(ctx)
    if not declared:
        ctx.results["ngsild_publish"] = f"skipped (no NGSI-LD resource server per {source})"
        _log(
            f"no NGSI-LD resource server per {source}, so the item has no "
            "exchange; nothing to publish"
        )
        return

    _log(f"publishing to exchange {ctx.item_id}")
    count = ngsild_publish(ctx, ctx.item_id)
    ctx.results["ngsild_publish"] = (
        f"{count} packet(s) -> {ctx.item_id}" if count else f"confirmed -> {ctx.item_id}"
    )
    _log(f"broker confirmed {count if count else 'the'} packet(s)")

    # A confirmed publish is not a readable record: the platform's consumer
    # still has to drain the queue into Elasticsearch.
    settle = ctx.config["ngsild_publish"]["settle_seconds"]
    if settle:
        _log(f"waiting {settle}s for the consumer to write {_es_index(ctx)}")
        time.sleep(settle)


# ----------------------------------------------------- 05 resource servers


def phase_05_resource_servers(ctx):
    """The end-to-end proof: a granted token reads, an ungranted one does not.

    The negative case matters more than the positive — without it a blanket-allow
    bug passes the whole suite.
    """
    if not ctx.config["run"]["verify_resource_servers"]:
        # ControlPlane and ACL standalone. The policy was still created and
        # verified in phase 03; only the data-plane calls are skipped.
        ctx.results["resource_servers"] = "skipped (verify_resource_servers off)"
        _log("resource server verification disabled; skipping data-plane calls")
        return

    outcomes = {}
    servers = refused = 0
    for lane in ctx.lanes:
        refused += _read_lane(ctx, lane, outcomes)
        servers += len(lane.servers)

    ctx.results["resource_servers"] = outcomes
    _log(f"no-policy token refused by {refused} of {servers} server(s)")


def _read_lane(ctx, lane, outcomes):
    """Read one lane's item from every server it declares.

    Each lane has its own resource token: the token is minted for one item id,
    so a second item cannot be read with the first item's token.

    Returns how many of this lane's servers refused the no-policy token.
    """
    granted_token = _resource_token(ctx, ctx.user("consumer"), lane)

    if item_declares_ngsild(ctx, lane)[0]:
        # What a 400 "No data found for this index" is actually about. The
        # servers answer out of this index; it exists only once the consumer has
        # written to it, so printing it here gives the name to check by hand.
        _log(f"item {lane.item_id} — resource servers read from index {_es_index(ctx, lane)}")
    else:
        _log(f"item {lane.item_id} — reading {', '.join(lane.server_keys())}")

    # A gateway item is answered by an adaptor consuming the item's queue, not
    # out of storage. With none attached the request is never replied to and the
    # read times out, so one runs for as long as the calls do.
    with _gateway_adaptor(ctx, lane):
        for key, server in lane.servers:
            payload = _read_server(ctx, key, server, granted_token, "granted", (200,), lane)
            outcomes[key] = f"granted:{_last_status(ctx)}"
            _log_response(ctx, f"{key} granted", payload)
            # A path-addressed read must come back describing the item asked
            # for; a 200 alone does not say that it did.
            item_scoped = "{item_id}" in (
                server.get("stac_verify_path") if item_declares_ogc_raster(ctx, lane)[0]
                and server.get("stac_verify_path") else server["verify_path"]
            )
            if item_scoped and not _mentions_item(payload, lane.item_id):
                raise AssertionError(
                    f"{key}: GET {server['verify_path'].format(item_id=lane.item_id)} "
                    f"returned 200 but the body never mentions {lane.item_id} — "
                    f"the collection served is not this run's item"
                )
        _log(f"granted token accepted by {len(lane.servers)} server(s)")

        # The no-policy consumer never requested access, so it has no resource
        # token to mint; an identity token must still be refused by every server
        # whose verify_path is access-controlled.
        denied_token = ctx.user("nopolicy").token
        refused = 0
        raster_item = item_declares_ogc_raster(ctx, lane)[0]
        for key, server in lane.servers:
            # A raster item is read through the STAC API, which is
            # access-controlled even where the vector path is not.
            denied_key = (
                "stac_expect_denied"
                if raster_item and server.get("stac_verify_path") else "expect_denied"
            )
            expect = tuple(server.get(denied_key) or ())
            if not expect:
                # Public metadata: refusing nobody is the correct behaviour, so
                # there is nothing here to assert.
                outcomes[key] += " denied:n/a (public path)"
                _log(f"{key}: {server['verify_path']} serves public metadata; no refusal to assert")
                continue
            payload = _read_server(ctx, key, server, denied_token, "no-policy", expect, lane)
            outcomes[key] += f" denied:{_last_status(ctx)}"
            _log_response(ctx, f"{key} no-policy", payload)
            refused += 1

    return refused


@contextmanager
def _gateway_adaptor(ctx, lane):
    """The adaptor, running for the block — or nothing, when it is not needed.

    Skipped unless this lane's item is a gateway one, on the same evidence phase
    04 uses for NGSI-LD: the types the catalogue returned, falling back to
    config.
    """
    config = ctx.config["gateway_adaptor"]
    declared, source = (
        item_declares_gateway(ctx, lane) if config["enabled"] else (False, "config")
    )

    if not config["enabled"] or not declared:
        if config["enabled"]:
            _log(f"no gateway resource server per {source}; no adaptor needed")
        yield None
        return

    queue = config["queue_name"] or lane.item_id
    with gateway_adaptor.running(ctx, queue) as process:
        yield process


def server_base(server):
    """The base URL for a resource server, whether or not `url` carries a scheme.

    Configs are written both ways — `v2.dev.iudx.io` and
    `https://v2.dev.iudx.io/files-connect-api` — and prepending a scheme to the
    second produces `https://https://…`, which fails as an unresolvable host
    rather than as an obvious configuration mistake.
    """
    url = server["url"]
    if url.startswith(("http://", "https://")):
        return url.rstrip("/")
    return f"{server.get('scheme', 'https')}://{url}".rstrip("/")


def _read_server(ctx, key, server, token, who, expect, lane=None):
    """One data-plane GET, against whichever server the config describes.

    Servers disagree on how an item is addressed. NGSI-LD and the gateway take
    it as `?id=<item>`; OGC puts it in the path, `/collections/<item>`. A
    verify_path carrying `{item_id}` is the second kind — it is formatted and no
    id parameter is sent, since the path already says which item is wanted.
    """
    lane = lane or ctx.primary
    base = server_base(server)
    # A raster item is served by the STAC API, not the collections endpoint.
    verify_path = server["verify_path"]
    if server.get("stac_verify_path") and item_declares_ogc_raster(ctx, lane)[0]:
        verify_path = server["stac_verify_path"]
    path, params = _verify_target(verify_path, lane.item_id)
    query = f"?id={lane.item_id}" if params else ""
    _log(f"{key} {who}: GET {base}{path}{query}")
    client = ApiClient(base, ctx.recorder, ctx.config["control_plane"]["timeout_seconds"])
    return client.get(
        path,
        f"{key}: {who} token reads" if expect == (200,) else f"{key}: {who} token refused",
        token=token,
        params=params,
        expect=expect,
    )


def _verify_target(verify_path, item_id):
    """(path, params) for a server's verify_path, item in the path or the query."""
    if "{item_id}" in verify_path:
        return verify_path.format(item_id=item_id), None
    return verify_path, {"id": item_id}


def _mentions_item(payload, item_id):
    """Whether a response actually names the item it was asked about.

    Only meaningful for the item-in-the-path servers: `/collections/<item>`
    answering 200 with somebody else's collection, or with an empty envelope,
    is a pass by status and a failure in fact.
    """
    return str(item_id) in json.dumps(payload, default=str)


def _last_status(ctx):
    """The HTTP status of the call just made, as the recorder saw it."""
    return ctx.recorder.entries[-1]["status"] if ctx.recorder.entries else "?"


def _log_response(ctx, label, payload):
    """Print what a resource server actually returned.

    Each server answers in its own shape, and the count of records matters as
    much as the status — a 200 with zero records is what an unpublished item
    looks like, and is indistinguishable from a working read unless the body is
    shown. With more servers coming, seeing every response beats inferring them
    from a status code.
    """
    entry = ctx.recorder.entries[-1] if ctx.recorder.entries else {}
    _log(f"{label}: {entry.get('status', '?')} in {entry.get('ms', '?')}ms — {_summarise(payload)}")

    limit = ctx.config["run"]["response_preview_chars"]
    if not limit:
        return
    # Scrubbed like the report is: a body that echoes a token should not land
    # in a terminal scrollback either.
    text = payload if isinstance(payload, str) else json.dumps(scrub(payload), default=str)
    if len(text) > limit:
        text = text[:limit] + f"… ({len(text)} chars)"
    for line in text.splitlines() or [""]:
        print(f"        {line}", flush=True)


def _summarise(payload):
    """A one-line shape description of a response body."""
    if payload is None or payload == "":
        return "empty body"
    if isinstance(payload, list):
        return f"{len(payload)} record(s)"
    if isinstance(payload, dict):
        for name in ("results", "result", "entities", "features"):
            if isinstance(payload.get(name), list):
                return f"{len(payload[name])} record(s) under '{name}'"
        return "keys: " + ", ".join(sorted(payload)[:8])
    if isinstance(payload, str):
        return f"{len(payload)} chars of text"
    return type(payload).__name__


def _resource_token(ctx, user, lane=None):
    """Register client credentials for a user, then mint an item-scoped token.

    /iudx/v2/auth/token authenticates with clientId/clientSecret headers rather
    than a bearer token, so the credentials have to be registered first. The
    token is scoped to one item, which is why a second item needs a second one.
    """
    lane = lane or ctx.primary
    credentials = _client_credentials(ctx, user)
    token_response = ctx.cp.post(
        "/iudx/v2/auth/token",
        f"mint resource token ({user.key}){lane.suffix}",
        headers={
            "clientId": credentials["clientId"],
            "clientSecret": credentials["clientSecret"],
        },
        json_body={"itemId": lane.item_id},
    )
    return token_response["access_token"]


def _client_credentials(ctx, user):
    """This user's client credentials, registered on first use.

    A run with two items mints two tokens — one per item — but a user needs only
    one client to mint them with, and registering a second is at best a wasted
    call.
    """
    cached = ctx.client_credentials.get(user.key)
    if cached:
        return cached
    credentials = ctx.cp.post(
        "/iudx/v2/auth/client",
        f"register client ({user.key})",
        token=user.token,
    )
    ctx.client_credentials[user.key] = credentials
    return credentials


# -------------------------------------------------------------- 06 auditing


def phase_06_auditing(ctx):
    """Assert the audit trail arrived, proving the RabbitMQ to Elastic path.

    Audit is published asynchronously, so this polls rather than reading once.
    """
    timeout = ctx.config["run"]["audit_timeout_seconds"]
    deadline = time.monotonic() + timeout
    consumer = ctx.user("consumer")

    while True:
        rows = ctx.cp.get(
            "/iudx/v2/auditing/consumer/activity",
            "consumer activity trail",
            token=consumer.token,
            params={"page": 1, "size": 50},
        )
        entries = rows_of(rows)
        if entries:
            ctx.results["audit_consumer_rows"] = len(entries)
            _log(f"consumer trail: {len(entries)} row(s)")
            break
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"no consumer audit rows within {timeout}s — the RabbitMQ to "
                "Elasticsearch path did not deliver"
            )
        time.sleep(5)

    # Scoped to the requester deliberately. Unfiltered, this endpoint scans the
    # whole platform's history: on dev it takes over 30s and then returns 400.
    # Filtering also makes the assertion mean something — an unfiltered count
    # would be satisfied by anyone else's activity and would prove nothing about
    # this run.
    requester = ctx.user("requester")
    admin_rows = ctx.cp.get(
        "/iudx/v2/auditing/admin/activity",
        "cos admin activity trail (this run's provider)",
        token=ctx.cos_admin_token,
        params={"page": 1, "size": 50, "userId": requester.user_id},
    )
    entries = rows_of(admin_rows)
    if not entries:
        raise AssertionError(
            f"cos admin sees no audit rows for {requester.username} — the org "
            "approval and item publish were not audited"
        )
    ctx.results["audit_admin_rows"] = len(entries)
    actions = sorted({str(field(row, "api", "action", default="?")) for row in entries})
    _log(f"admin trail for provider: {len(entries)} row(s) — {', '.join(actions[:6])}")


# --------------------------------------------------------------- 07 sandbox


def phase_07_sandbox(ctx):
    """Check the sandbox server is up and refusing unauthenticated callers.

    Independent of everything above it: no lane, no item, no user. The sandbox
    is a notebook/compute service rather than a catalogue resource server — it
    has no per-item route and authenticates a Keycloak identity token by `azp`,
    so the item-scoped token phase 05 uses could never read it. See sandbox.py.

    Last, and skipped when disabled, so a deployment without a sandbox — or one
    whose sandbox is momentarily down — does not gate the onboarding flow the
    earlier phases prove.
    """
    if not ctx.config["sandbox"]["enabled"]:
        ctx.results["sandbox"] = "skipped (sandbox.enabled off)"
        _log("sandbox checks disabled")
        return

    summary = check_sandbox(ctx)
    ctx.results["sandbox"] = summary
    _log(summary)


# ------------------------------------------------------------- 08 community


def phase_08_community(ctx):
    """Check the community-layer server: health, then each mounted product.

    Independent of the flow above it, like phase 07 — no lane, no item, no user.
    The community layer carries Discussion and Challenge behind one process and
    is not a catalogue resource server: no route takes an item id, and it
    authenticates a Keycloak identity token by audience and issuer. See
    community.py.

    Creates nothing. The authoriser inserts a User row on every *authenticated*
    request and nothing deletes users, so these checks are deliberately made
    without a token.
    """
    if not ctx.config["community"]["enabled"]:
        ctx.results["community"] = "skipped (community.enabled off)"
        _log("community-layer checks disabled")
        return

    summary = check_community(ctx)
    ctx.results["community"] = summary
    _log(summary)


PHASES = [
    ("00 sign in", phase_00_signin),
    ("01 organisation onboarding", phase_01_organisation),
    ("02 catalogue", phase_02_catalogue),
    ("03 access request", phase_03_access_request),
    ("04 data onboarding", phase_04_data_onboarding),
    ("05 resource servers", phase_05_resource_servers),
    ("06 auditing", phase_06_auditing),
    ("07 sandbox", phase_07_sandbox),
    ("08 community", phase_08_community),
]