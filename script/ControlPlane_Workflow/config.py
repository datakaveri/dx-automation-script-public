#!/usr/bin/env python3
"""
Configuration for the ControlPlane onboarding E2E harness.

Nothing about a deployment is baked into the harness. A run is described by a
config file — config.json beside this module, or a path passed in — and every
value in it can be overridden without editing the file, so the same harness runs
against local docker, dev, staging or anything else.

Precedence (highest wins):

    --set flag  >  environment variable  >  config file  >  built-in default

Environment overrides address any key by its path, upper-cased, joined with a
double underscore, under the DXE2E_ prefix:

    DXE2E_KEYCLOAK__URL=https://kc.example.com
    DXE2E_POSTGRES__PASSWORD=...
    DXE2E_RESOURCE_SERVERS__OGC__URL=ogc.example.com
    DXE2E_RUN__PREFIX=nightly

config.json is gitignored, so it may hold secrets — but values may instead
reference the environment as ${VAR} or ${VAR:-fallback}, resolved at load time.
Interpolation walks the whole tree before anything looks at `enabled`, so give
optional secrets a fallback: ${E2E_PG_PASS:-}.

    python3 -m ControlPlane_Workflow.config     # print the resolved config
"""

import copy
import json
import os
import re
import sys
from pathlib import Path

ENV_PREFIX = "DXE2E_"
HERE = Path(__file__).parent

def _entry_dir():
    """The directory the entry point was run from — main/, normally.

    The harness is a package under e2e/ while the config that describes a
    deployment lives beside the script that runs it, so config.json is looked
    for there first and only then in the package itself.
    """
    try:
        if sys.argv and sys.argv[0]:
            return Path(sys.argv[0]).resolve().parent
    except (OSError, IndexError):
        pass
    return HERE


# The deployment being tested. config.example.json is the committed template;
# config.json is the real one and is gitignored, since it holds credentials.
def _config_locations():
    """Where a config file is looked for, in order.

    Beside the entry point first — main/config.json for a normal run — then the
    working directory and its main/, which is what `python3 -m e2e.config` from
    the repository root needs, and finally the package itself.
    """
    cwd = Path.cwd()
    # HERE is script/ControlPlane_Workflow, so the repository root is two up.
    root = HERE.parent.parent
    seen, ordered = set(), []
    for directory in (_entry_dir(), cwd, cwd / "main", root / "main", HERE):
        resolved = directory.resolve()
        if resolved not in seen:
            seen.add(resolved)
            ordered.append(directory)
    return ordered

# Resource servers the policy is verified against. Keys are stable short names;
# the config overrides the connection details but does not invent new servers.
RESOURCE_SERVER_KEYS = ("ngsild", "gateway", "ogc", "file")

# Built-in defaults. A config file only states what differs from these.
DEFAULTS = {
    "control_plane": {
        "base_url": None,
        "timeout_seconds": 30,
    },
    "acl": {
        # Policy/ACL server. Defaults to control_plane.base_url when the
        # deployment fronts both behind one host.
        "base_url": None,
        # Written onto every catalogue item as `apdURL`. A RESTRICTED item with
        # no apdURL is unreachable: getItemWithAccessChecks reads it straight
        # off the Elasticsearch document (ItemServiceImpl.java:339) and refuses
        # with "Access denied, APD URL missing", so no token can ever be minted
        # for it. The APD is the ACL server, so this defaults to base_url with
        # the scheme stripped — the form real items on the platform carry.
        "apd_url": None,
    },
    "keycloak": {
        "url": None,
        "realm": "iudx",
        # Service account used for the Admin API: creating and deleting the test
        # users. Reuse the deployment's own admin client (keycloakAdminClientId /
        # keycloakAdminClientSecret) rather than minting a second credential.
        "admin_client_id": None,
        "admin_client_secret": None,
        # Public client used to mint end-user tokens by password grant.
        "user_client_id": "postman-client",
        "user_client_secret": None,
        # Realm role names, overridable for deployments that renamed them.
        "roles": {
            "cos_admin": "cos_admin",
            "org_admin": "org_admin",
            "provider": "provider",
            "delegate": "delegate",
        },
    },
    # Leave username/password empty and the harness creates its own namespaced
    # cos_admin, then sweeps it — account and audit rows alike — which keeps the
    # run self-contained. Creating one needs realm-admin on
    # keycloak.admin_client_id.
    #
    # Set both to borrow an existing platform administrator instead. The account
    # is then never namespaced and never deleted, but with run.delete_audit_rows
    # on its user_activity_audit_log rows are still swept: they are the
    # harness's actions, not the administrator's. That sweep is not scoped to
    # this run, so keep run.delete_audit_rows false unless the account's history
    # is disposable. Prefer borrowing where a short-lived super-admin with a
    # stored password is unacceptable.
    "cos_admin": {
        "username": None,
        "password": None,
    },
    # Already-deployed resource servers. verify_path is a GET the granted token
    # must be able to read and an ungranted token must not.
    "resource_servers": {
        "ngsild": {
            "name": "NGSI-LD",
            "type": "ngsi-ld",
            "scheme": "https",
            "url": None,
            "verify_path": None,
            "access_types": ["api"],
            "query_types": ["ATTR"],
            "dataset_type": "DATASET",
            # What the no-policy token must get. An endpoint that serves public
            # metadata cannot refuse anyone, so an empty list means "this path
            # is not access-controlled, do not assert a refusal" — see ogc.
            "expect_denied": [401, 403],
            "enabled": True,
        },
        "gateway": {
            "name": "GATEWAY",
            "type": "gateway",
            "scheme": "https",
            "url": None,
            "verify_path": None,
            "access_types": ["api"],
            "query_types": ["ATTR"],
            "dataset_type": "DATASET",
            "expect_denied": [401, 403],
            "enabled": True,
        },
        "ogc": {
            "name": "OGC",
            "type": "ogc",
            "scheme": "https",
            "url": None,
            # {item_id} is substituted; a path carrying it is fetched as
            # /collections/<id> rather than with an ?id= parameter.
            "verify_path": "/collections/{item_id}",
            # A raster item is read through the STAC API instead.
            "stac_verify_path": "/stac/collections/{item_id}/items",
            # Empty for the same reason as expect_denied: the STAC items
            # listing answers 200 to any token, so there is no refusal to
            # assert. Neither OGC path is access-controlled at the read the
            # harness makes — phase 05 proves the data is served, not that it
            # is protected.
            "stac_expect_denied": [],
            "access_types": ["api"],
            # The one switch that decides which OGC pipeline a run exercises:
            # true onboards rasters through the STAC scripts, false onboards a
            # vector collection. It drives query_types below, so the two can
            # never disagree. Leave it null to set query_types by hand.
            "raster": False,
            # Derived from `raster` unless that is null: ["STAC"] for raster,
            # ["FEATURE"] for vector.
            "query_types": ["FEATURE"],
            "dataset_type": "DATASET",
            # /collections/<id> is the OGC collection description: public
            # metadata by design, so it answers 200 to any token and a refusal
            # cannot be asserted on it. Authorisation shows up on the data
            # itself (/collections/<id>/items), not on its description.
            "expect_denied": [],
            "enabled": True,
        },
        "file": {
            "name": "FILE",
            "type": "file",
            "scheme": "https",
            # May carry its own scheme and path prefix
            # (https://host/files-connect-api); a scheme is only prepended when
            # the url lacks one.
            "url": None,
            # The item-scoped read on Files Connect. There is no list route —
            # `download` is the endpoint that takes a databank id, and it
            # refuses an unauthorised token, which is what phase 05 asserts.
            "verify_path": "/v1/databanks/{item_id}/download",
            "access_types": ["file"],
            "query_types": [],
            "dataset_type": "DATASET",
            "expect_denied": [401, 403],
            "enabled": True,
        },
    },
    # RabbitMQ. Only needed to observe that audit events were published; the
    # flow itself never talks to the broker directly.
    "databroker": {
        "enabled": False,
        "host": None,
        "management_port": 15672,
        "username": None,
        "password": None,
        "vhost": "IUDX-V2-INTERNAL",
        "auditing_exchange": "auditing",
        "use_tls": True,
    },
    # Publishing NGSI-LD records into the item's own exchange. The catalogue
    # creates one exchange per NGSI-LD item, named with the item id, and the
    # resource server serves what was published there — so without this step a
    # data-plane GET returns an empty result whether or not the policy works.
    #
    # The publishing itself is not reimplemented: the harness runs
    # script/NGSILD_Automation_Script/ngsild_publish_v1.py, the same script that
    # is run by hand against real deployments.
    "ngsild_publish": {
        "enabled": True,
        # Relative paths resolve against the e2e directory, then the repo root.
        "script": "../script/NGSILD_Automation_Script/ngsild_publish_v1.py",
        # host/username/password fall back to the databroker section when null:
        # it is the same broker, reached with the same admin credentials. The
        # port and vhost do differ — this is the AMQPS data plane, not the
        # management API on the internal vhost.
        "host": None,
        "port": 24567,
        "username": None,
        "password": None,
        "vhost": "IUDX-V2",
        "cert_path": "../script/NGSILD_Automation_Script/rabbitmq-ca.crt",
        # false accepts a broker certificate whose subject does not match its
        # hostname; the chain is verified either way.
        "check_hostname": False,
        # Queue bound to the item's exchange, so a published message has
        # somewhere to route — the publisher fails loudly when it does not.
        "queue_name": "database",
        # JSON array of records to publish; null uses the publisher's built-in
        # sample data. Relative paths resolve like `script` above.
        "data_file": None,
        # Publish this many packets, cycling the source records; null publishes
        # each record once.
        "count": None,
        # Pass --verbose to the publisher and echo its whole log.
        "verbose": False,
        # Pause after a confirmed publish. The broker confirming a message is
        # not the resource server being able to serve it: the platform's
        # consumer still has to drain the queue and write to Elasticsearch,
        # which is what creates iudx-v2__<item id>. Reading immediately gets
        # "No data found for this index".
        "settle_seconds": 2,
        # The exchange is created by the catalogue as the item is onboarded, so
        # a publish can arrive fractionally early. Raise this if that race shows
        # up on a slow deployment.
        "retries": 0,
        "retry_delay_seconds": 5,
    },
    # The gateway resource server does not serve from storage: it publishes the
    # request onto a queue named with the item id and waits for a reply on
    # reply_to. Nothing answers unless an adaptor is consuming from that queue,
    # so a gateway read against a deployment with no adaptor hangs until the
    # client gives up — which is what a read timeout on /dataplane/rsp is.
    #
    # script/GATEWAY_Automation_Script/gateway.py is that adaptor. Phase 05
    # starts it for the item's queue, waits for it to attach, makes the calls,
    # and stops it afterwards.
    "gateway_adaptor": {
        "enabled": True,
        "script": "../script/GATEWAY_Automation_Script/gateway.py",
        # Note the vhost: the adaptor consumes on the internal vhost, not the
        # data-plane one ngsild_publish writes to. host/username/password fall
        # back to ngsild_publish and then databroker.
        "host": None,
        "port": 24567,
        "username": None,
        "password": None,
        "vhost": "IUDX-V2-INTERNAL",
        "cert_path": "../script/GATEWAY_Automation_Script/rabbitmq-ca.crt",
        "check_hostname": False,
        # The queue the catalogue created for the item. null uses the item id,
        # which is what it is named.
        "queue_name": None,
        # The upstream the adaptor answers with. The default is the public
        # dummy API the script ships with: the point of the phase is that the
        # gateway path carries a reply, not what the reply contains.
        "api_url": "https://dummyjson.com/users",
        "results_key": "users",
        # How long to let it attach to the queue before the first call.
        "startup_seconds": 2,
        "verbose": False,
    },
    # The gateway equivalent of ngsild_delete: the queue named with the item id
    # and the provider's broker user, neither of which goes away with the
    # catalogue item. Needs no Elasticsearch, so unlike ngsild_delete it is on
    # by default.
    "gateway_delete": {
        "enabled": True,
        "script": "../script/GATEWAY_Automation_Script/delete_rmq.py",
        # All of these fall back to gateway_adaptor — it is the same broker,
        # vhost and queue the adaptor consumes from — and then to
        # ngsild_publish/databroker for the credentials.
        "host": None,
        "port": None,
        "username": None,
        "password": None,
        "vhost": None,
        "cert_path": None,
        "check_hostname": None,
        # Management API, for deleting the user. Falls back to
        # ngsild_delete.mgmt_url, which is the same API.
        "mgmt_url": None,
        "mgmt_verify": None,
        # null uses the item id, which is what the queue is named.
        "queue_name": None,
        # null resolves the provider's Keycloak id from this run's requester.
        "delete_user": None,
        "verbose": False,
    },
    # The OGC resource server's own database. It is a different database, a
    # different schema and — for now — a different user from the ControlPlane
    # one in `postgres`, which is why teardown against `postgres` found nothing:
    # the collection rows were never in it.
    #
    # Shared by every OGC teardown, vector and raster alike, so the connection
    # is described once and each script is handed the same one.
    "ogc_postgres": {
        # host/port/user/password fall back to the postgres section — the same
        # server, reached as the admin for now.
        "host": None,
        "port": None,
        # The database the OGC server keeps its collections in — the one
        # vector_deletion.py points at, and not the ControlPlane database.
        "database": "ogc_rs_v2",
        # Only set this if the OGC tables live outside the connection's default
        # search_path. The deletion scripts issue unqualified SQL, so a schema
        # named here is applied as the connection's search_path rather than
        # being written into their statements. null leaves the default alone.
        "schema": None,
        "user": None,
        "password": None,
    },
    # Onboarding a vector collection for an OGC item. Unlike NGSI-LD there is
    # nothing to publish: the GeoPackage is uploaded through the OGC processes
    # API and an onboarding job has to report SUCCESSFUL before the collection
    # exists at all.
    #
    # OGC vector and STAC share the `ogc` server type and are told apart by
    # query type, so this covers items whose OGC query types do not include
    # STAC.
    "ogc_vector": {
        "enabled": True,
        "script": "../script/OGC_Automation_Script/Vector_Automation/Creation/vector_creation.py",
        # null derives it from resource_servers.ogc.
        "base_url": None,
        # The GeoPackage to onboard. There is no default worth inventing — the
        # run needs a real file — so this is required once OGC vector is in play.
        "gpkg_path": None,
        "bucket_name": None,
        "region": None,
        # The two process ids are constant across deployments, so they live in
        # ogc_vector.py rather than here.
        "batch_size": 5,
        "label": "E2E vector collection",
        "description": "Automated end-to-end test asset. Safe to delete.",
        # Whose token the onboarding runs as. The provider owns the item, so
        # that is the one with rights to onboard against it — "requester" is the
        # provider in this flow.
        "token_user": "requester",
        # Onboard as an existing platform provider instead of this run's
        # throwaway one, by password grant against keycloak.user_client_id.
        # Leave null to use the run's own provider (token_user below).
        #
        # Note what this does NOT change: the catalogue item still belongs to
        # the run's provider and its organisation. The OGC server checks that
        # the caller owns the resource or shares its organisation, so borrowing
        # an account from another organisation trades one failure for another.
        "provider_username": None,
        "provider_password": None,
        # Insert a roles row for the provider in the OGC database before
        # onboarding. ri_details.role_id is a foreign key onto roles(user_id),
        # and a provider created minutes ago has no row there — which the
        # onboarding process reports only as "Failed to onboard the collection
        # in db.". Teardown removes what this inserted.
        "ensure_provider_role": True,
        # "identity" hands over the provider's own Keycloak token. That is the
        # right credential for onboarding: an item-scoped token is minted from a
        # *policy*, and a provider holds no policy on their own item — asking
        # for one returns 403 "No access found via delegation or direct access".
        # "resource" mints that item-scoped token instead, for a deployment
        # whose processes API expects the data-plane form.
        "token_kind": "identity",
        # The job reports SUCCESSFUL before the collection is necessarily
        # queryable; phase 05 reads it straight afterwards.
        "settle_seconds": 15,
        # Off by default, and not to be raised casually: a retry re-runs the
        # whole script, and ogr2ogr appends to the collection table it already
        # created — a failed run retried once leaves the features loaded twice.
        # Only worth turning on if a deployment is shown to reject a resource it
        # has not yet learned about.
        "retries": 0,
        "retry_delay_seconds": 15,
        "verbose": False,
    },
    # Onboarding a raster (STAC) collection for an OGC item. Two scripts, in
    # order: the STAC collection and its items are posted to the API, then the
    # GeoTIFFs those items describe are uploaded to the bucket. Neither half is
    # useful without the other.
    "ogc_raster": {
        "enabled": True,
        "ingest_script": "../script/OGC_Automation_Script/Raster_Automation/Creation/stac_injestion.py",
        "upload_script": "../script/OGC_Automation_Script/Raster_Automation/S3Upload/S3.py",
        # Folder of GeoTIFFs to onboard. They are symlinked into a directory
        # named with the item id per run, because the STAC asset href is
        # "<collection id>/<file>" while the uploader keys objects as
        # "<directory name>/<file>" — the two agree only when the directory is
        # named after the collection.
        "tif_dir": "../script/OGC_Automation_Script/Raster_Automation/5ddcbdd8-3040-48f3-9a0c-5916c0c0d3be",
        # null derives it from resource_servers.ogc, as ogc_vector does.
        "base_url": None,
        "title": "E2E raster collection",
        "description": "Automated end-to-end test asset. Safe to delete.",
        # bucket/region fall back to ogc_vector: one bucket, both pipelines.
        "bucket": None,
        "region": None,
        # Only for an S3-compatible store that is not AWS.
        "endpoint": None,
        "aws_access_key_id": None,
        "aws_secret_access_key": None,
        "timeout_seconds": 60,
        # Between posting the items and uploading what they point at.
        "ingest_settle_seconds": 5,
        # After the upload, before the data plane is read.
        "upload_settle_seconds": 15,
        "token_user": "requester",
        "token_kind": "identity",
        "verbose": False,
    },
    # Deleting the STAC items again. The collection's own rows go with the
    # ogc_vector_delete pass, which is keyed by the same item id.
    "ogc_raster_delete": {
        "enabled": True,
        "script": "../script/OGC_Automation_Script/Raster_Automation/Deletion/stac_deletion.py",
        "batch_size": 100,
        "delete_delay": 0.2,
        "request_timeout": 60,
        "verbose": False,
    },
    # The files themselves, in S3. The OGC teardowns clean the platform's own
    # state; the bucket keeps `<item id>.gpkg` for a vector item and an
    # `<item id>/` folder of GeoTIFFs for a raster one until something removes
    # them. Runs once per item, before the item that names them is deleted.
    "ogc_s3_cleanup": {
        "enabled": True,
        "script": "../script/OGC_Automation_Script/Extra_To_Clean_S3/s3_deleteion.py",
        # All four fall back to the sections that put the files there:
        # ogc_raster first, then ogc_vector for bucket and region.
        "bucket": None,
        "region": None,
        "aws_access_key_id": None,
        "aws_secret_access_key": None,
        "verbose": False,
    },
    # The other half: the collection's rows and its table, in the OGC database.
    # Deleting the catalogue item leaves both behind.
    "ogc_vector_delete": {
        "enabled": True,
        "script": "../script/OGC_Automation_Script/Vector_Automation/Deletion/vector_deletion.py",
        # The connection comes from ogc_postgres, so it cannot drift from the
        # one the raster teardown uses.
        #
        # When onboarding failed, leave whatever it did manage to create in
        # place. The failure message is a catch-all, so the half-built state is
        # the evidence — and deleting it makes every failed run look identical.
        # Turn this off to keep failed runs self-cleaning.
        "keep_on_failure": True,
        "verbose": False,
    },
    # Uploading a file into the item's databank. A catalogue item is a databank
    # — adex:DataBank is the type the harness creates — and the Files Connect
    # API stores objects under it, so a file item has nothing behind it until
    # something is uploaded.
    "file_upload": {
        "enabled": True,
        "script": "../script/FILE_Automation_Script/creation/file_creation.py",
        # null derives it from resource_servers.file.
        "base_url": None,
        "api_version": "v1",
        # The file to upload. The script ships a small CSV, which is enough to
        # prove the path works.
        "file_path": "../script/FILE_Automation_Script/creation/postman-upload.csv",
        # Object key inside the databank; null uses the file's own name.
        "key": None,
        # null lets the script guess from the file name.
        "content_type": None,
        "verify_tls": True,
        "request_timeout_seconds": 120,
        "max_retries": 3,
        "part_size_mb": 100,
        # The zip/report job the file server runs after an upload. Waiting for
        # it is what makes a green run mean the file is actually usable.
        "processing_enabled": True,
        "processing_type": "all",
        "processing_uploaded_only": False,
        "processing_wait": True,
        "processing_poll_seconds": 5,
        "processing_timeout_seconds": 600,
        # Whose token uploads. The provider owns the databank.
        "token_user": "requester",
        "token_kind": "identity",
        # After the upload, before the data plane is read.
        "settle_seconds": 5,
        "verbose": False,
    },
    # The other half: the uploaded objects, removed before the item that holds
    # them. Connection details come from file_upload — one server, one config.
    "file_delete": {
        "enabled": True,
        "script": "../script/FILE_Automation_Script/deletion/file_deletion.py",
        "verbose": False,
    },
    # Tearing down what onboarding an NGSI-LD item created outside the
    # catalogue: the Elasticsearch index, the RabbitMQ exchange, and the
    # provider's catalogue-created broker user. Deleting the catalogue item
    # removes none of them, and afterwards nothing on the platform names them.
    #
    # Runs script/NGSILD_Automation_Script/ngsild_delete_v1.py, first in
    # teardown — while the item it belongs to still exists.
    #
    # Off by default: it needs Kibana credentials, which no other part of the
    # harness does, so a deployment that has not been given them should skip the
    # step loudly rather than fail every run at load time.
    "ngsild_delete": {
        "enabled": False,
        "script": "../script/NGSILD_Automation_Script/ngsild_delete_v1.py",
        # Elasticsearch is reached through the Kibana console proxy, so these
        # are Kibana's host and a user with delete rights on the index.
        "kibana_host": None,
        "kibana_username": None,
        "kibana_password": None,
        # RabbitMQ HTTP management API — the base URL, which on most
        # deployments is a path behind the gateway rather than a port.
        "mgmt_url": None,
        # Fall back to ngsild_publish, then databroker: one broker, one admin
        # credential. The management API needs the 'administrator' tag.
        "username": None,
        "password": None,
        "vhost": None,
        # Index names use a double underscore: iudx-v2__<exchange name>.
        "index_prefix": "iudx-v2__",
        # true, false, or a path to a CA file.
        "mgmt_verify": "true",
        # The catalogue-created broker user, named with the provider's Keycloak
        # id. null resolves it from this run's requester, which is what a
        # self-contained run wants.
        "delete_user": None,
        # Report what would be deleted without deleting it.
        "dry_run": False,
        "verbose": False,
    },
    # The sandbox (notebook/compute) server — checked, never onboarded to.
    #
    # It is deliberately NOT a `resource_servers` entry. Those go into the
    # catalogue item's resourceServer array and are read with an item-scoped
    # token; the sandbox has no per-item route at all, and its middleware
    # validates a Keycloak *identity* token by `azp`, so it would refuse that
    # token every time. sandbox.py has the full reasoning.
    #
    # Off by default, so a deployment without a sandbox stays green.
    "sandbox": {
        "enabled": False,
        # Host, or a full base URL. A scheme is prepended only when the url
        # lacks one, exactly as for a resource server.
        "scheme": "https",
        "url": None,
        # Registered outside the auth middleware, so it needs no credentials.
        "health_path": "/v1/health",
        # The `status` the health body must carry. null asserts the 200 alone —
        # but a 200 reading "degraded" is precisely what that would wave through.
        "expect_status": "ok",
        # An auth-controlled route, called with no Authorization header. Its
        # refusal is what proves the middleware is in front of the API, which
        # /v1/health cannot say: it is registered outside that middleware and
        # answers 200 either way.
        "verify_path": "/v1/notebook/list",
        # What that unauthenticated call must get back. [] skips the check.
        "expect_denied": [401, 403],
        # Two ways to turn "the door is locked" into "the right key opens it".
        # Both optional; with neither set, no authenticated call is made.
        #
        # An authenticated read here is safe: unlike the community layer's, the
        # sandbox middleware only validates the token — it writes nothing — and
        # listNotebooks is read-only. The heavy provisioning (namespace, Kubeflow
        # profile, 50Gi PVC) happens on notebook *create*, which this never calls.
        #
        # bearer_token: a token pasted in, from whichever realm the sandbox
        # trusts. Use this to learn what a deployment expects without running
        # the whole flow.
        "bearer_token": None,
        # token_user: the key of a user this run creates ("consumer",
        # "requester", …), whose identity token is used instead. Needs phase 00
        # to have run, so it stands down with a note under --only "07 sandbox".
        #
        # Whether it works is a property of the deployment: the middleware wants
        # the token's `azp` to equal its API_KEYCLOAK_CLIENT_ID and the account
        # to be email-verified, plus KYC-verified when API_KYC_ENABLED is on.
        # The sandbox says which of those failed and the harness passes that
        # message through — the fastest way to find out what it wants.
        "token_user": None,
        # The Keycloak client to mint that user's token with. The sandbox
        # refuses any token whose `azp` is not its own API_KEYCLOAK_CLIENT_ID,
        # and that is rarely the client the rest of the harness uses — on dev
        # the harness signs in with `frontend-client` while the sandbox wants
        # `angular-client`, and a token from the wrong one comes back 401
        # "Invalid client". null reuses keycloak.user_client_id.
        "token_client_id": None,
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    # The community-layer server — discussion and challenge. Checked, never
    # written to, for the same reason as `sandbox`: no route takes an item id,
    # and its authoriser validates a Keycloak *identity* token by audience and
    # issuer, not the item-scoped token phase 05 mints. community.py explains.
    #
    # Off by default, so a deployment without it stays green.
    "community": {
        "enabled": False,
        "scheme": "https",
        # Host plus the proxy subpath the service is mounted under — it runs
        # behind ROOT_PATH ("/community" on dev), so this normally carries a
        # path as well as a host.
        "url": None,
        # Not a liveness stub: it runs SELECT 1 against Postgres and head_bucket
        # against S3, answering 200 only when both pass and 500 otherwise.
        "health_path": "/healthz",
        # Dependencies the health body must report healthy. null means "every
        # one it reports", which keeps working if the service adds another.
        # Naming them is what makes a failure say which dependency is down.
        "expect_dependencies": ["PostgreSQL DB", "AWS S3"],
        # The two products the service can mount. ACTIVATED_SERVICES on the
        # deployment decides which routers exist at all — a Discussion-only
        # deployment answers 404 on every /challenge route — so these mirror it.
        #
        # public_path is a token-optional read: it proves the router is mounted
        # and serving real data. verify_path is a token-required one, called
        # with no Authorization header: its refusal proves the protected routes
        # are actually protected, which a public 200 says nothing about.
        "services": {
            "discussion": {
                "enabled": True,
                "public_path": "/discussion/tags/popular",
                "verify_path": "/discussion/recent/bookmarked",
                "expect_denied": [401, 403],
            },
            "challenge": {
                "enabled": True,
                "public_path": "/challenge/all",
                "verify_path": "/challenge/participated",
                "expect_denied": [401, 403],
            },
        },
        # Optional, and null by default for a reason: the authoriser inserts a
        # User row into the discussion and/or challenge database on every
        # authenticated request, and no route deletes a user. Unauthenticated
        # calls never reach that code, so the checks above leave nothing behind;
        # setting a token trades that for a stronger assertion. See
        # docs/community-layer.md.
        "bearer_token": None,
        "timeout_seconds": 30,
        "verify_tls": True,
    },
    # Used for the residue sweep and for asserting rows landed. Read-only unless
    # run.sweep_database is on.
    "postgres": {
        "enabled": False,
        "host": None,
        "port": 5432,
        "database": None,
        "schema": "aaa",
        "user": None,
        "password": None,
        "sslmode": "prefer",
    },
    "run": {
        # Every artefact the harness creates is named with this prefix. It is
        # what makes teardown a deterministic sweep instead of id bookkeeping,
        # and what lets two people run against one deployment without colliding.
        "prefix": "e2e",
        # Password given to every user the harness creates.
        "user_password": "E2eTest@Pass1",
        # Domain for generated accounts. example.invalid is reserved by RFC 2606
        # and never resolves, so the platform's notification emails cannot reach
        # a real inbox. Override for realms that validate the domain.
        "email_domain": "example.invalid",
        "cleanup": True,
        # Re-query after teardown and fail the run if anything survived.
        "verify_cleanup": True,
        # Delete residual rows the APIs leave behind (granted org-create
        # requests, and orphans from crashed runs). Needs postgres.enabled.
        "sweep_database": False,
        # Age guard on leftovers belonging to *other* runs sharing this prefix.
        # 0 means no guard: sweep everything under the prefix, which is what a
        # test harness wants — it only ever creates disposable data. Raise it
        # above 0 only if several people share one prefix on one deployment and
        # a sweep could hit a run still in flight. Giving each person their own
        # run.prefix is the better isolation.
        "sweep_older_than_hours": 0,
        # Audit rows are append-only by design. On a shared deployment leave
        # this false and assert on them instead of deleting them.
        "delete_audit_rows": False,
        # How far the sweep reaches into a *borrowed* cos_admin's rows. The
        # account is never deleted either way — this is only about what it owns.
        #
        # False: every table, restricted to this run — each statement matches
        # the account's id AND the item, organisation or name anchor that makes
        # the row this run's. What it owns for its own reasons survives.
        #
        # True: every table, unrestricted, including the ones with no anchor to
        # scope by — credits, subscriptions, apps, clients, KYC — and the
        # resource_servers / acl_servers registrations the account owns. Those
        # last two the harness cannot recreate and the deployment resolves
        # against, so True is an outage on a shared stack. The Keycloak account
        # and its user_table row survive either way, but nothing else does.
        # For a staging or throwaway stack whose cos_admin exists only to run
        # this harness; destructive anywhere else.
        #
        # Ignored when no cos_admin is configured — the one the harness creates
        # is namespaced and swept whole either way.
        "delete_all_cos_admin_data": False,
        # How long to wait for async audit propagation (RMQ -> consumer -> ES).
        "audit_timeout_seconds": 60,
        # Whether phase 04 calls the data-plane servers. Turn this off to run
        # ControlPlane and ACL standalone: onboarding, catalogue, access request
        # and policy all still run and are verified, and no resource server is
        # contacted. The catalogue item is unaffected — it always declares the
        # enabled servers, because its schema requires a non-empty
        # resourceServer array.
        "verify_resource_servers": True,
        # How much of each resource server's response body phase 05 prints.
        # Every server answers in its own shape and a 200 with zero records is
        # what an unpublished item looks like, so the body is worth seeing. 0
        # prints the one-line summary only.
        "response_preview_chars": 800,
        # Where to write HTML reports when --report is not given. Each run is
        # written as <namespace>.html, so runs never overwrite one another and
        # a report can always be matched back to the artefacts it describes.
        # Created if missing. Relative paths resolve against the working
        # directory; leave null to write no report unless --report is passed.
        "report_dir": None,
        # Filename inside report_dir. Null names each report after the run's
        # namespace, so runs never overwrite one another. Set a fixed name to
        # keep exactly one file, overwritten every run — which is what you want
        # while iterating, and what you do not want on a nightly schedule where
        # yesterday's failure is the thing you need to read.
        "report_file": None,
    },
}

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SECRET_HINTS = ("secret", "password", "token")


class ConfigError(Exception):
    pass


def _interpolate(value):
    """Resolve ${VAR} / ${VAR:-fallback} in a string against the environment."""
    if not isinstance(value, str):
        return value

    def sub(match):
        name, fallback = match.group(1), match.group(2)
        found = os.environ.get(name)
        if found is not None:
            return found
        if fallback is not None:
            return fallback
        raise ConfigError(
            f"config references ${{{name}}} but {name} is not set in the environment"
        )

    return _VAR_RE.sub(sub, value)


def resolve_path(value):
    """Resolve a config path against the harness directory, then script/, then
    the repository root.

    Paths in the config name files that live in this repository — the publisher
    script, its CA certificate — so they are written relative to the checkout,
    not to whatever directory the harness happens to be run from.
    """
    if not value:
        return None
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    # The package sits at script/ControlPlane_Workflow, so a path written
    # relative to the harness ("../script/…"), to script/ itself, or to the
    # repository root all resolve.
    for base in (HERE, HERE.parent, HERE.parent.parent):
        resolved = base / candidate
        if resolved.exists():
            return resolved.resolve()
    return HERE / candidate


def _walk(node, fn):
    if isinstance(node, dict):
        return {k: _walk(v, fn) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, fn) for v in node]
    return fn(node)


def _deep_merge(base, override):
    """Merge override into base. Dicts merge by key; lists replace wholesale."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _as_bool(value):
    """Read a boolean that may have arrived from --set or the environment as text."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def _coerce(raw, current):
    """Coerce an override string to the type of the value it replaces."""
    if isinstance(current, bool):
        lowered = raw.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        raise ConfigError(f"expected a boolean, got {raw!r}")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, (dict, list)):
        return json.loads(raw)
    return raw


def _assign(config, path, value, origin):
    """Set config[path[0]][path[1]]... coercing to the existing value's type."""
    node = config
    for part in path[:-1]:
        if not isinstance(node.get(part), dict):
            raise ConfigError(f"{origin} does not address a known config section")
        node = node[part]
    leaf = path[-1]
    if leaf not in node:
        raise ConfigError(f"{origin} does not address a known config key")
    node[leaf] = _coerce(value, node[leaf])


def _apply_env(config):
    """Overlay DXE2E_-prefixed environment variables onto the config tree."""
    for env_key in sorted(os.environ):
        if not env_key.startswith(ENV_PREFIX):
            continue
        path = env_key[len(ENV_PREFIX):].lower().split("__")
        _assign(config, path, os.environ[env_key], env_key)
    return config


def _validate(config):
    """Fail once, listing everything wrong, before the run touches a deployment."""
    problems = []

    if not config["control_plane"]["base_url"]:
        problems.append("control_plane.base_url is required")

    keycloak = config["keycloak"]
    for key in ("url", "realm", "admin_client_id", "admin_client_secret", "user_client_id"):
        if not keycloak.get(key):
            problems.append(f"keycloak.{key} is required")

    cos = config["cos_admin"]
    if bool(cos.get("username")) != bool(cos.get("password")):
        problems.append(
            "cos_admin needs username and password together, or neither — with "
            "neither, the harness creates and sweeps its own cos_admin"
        )

    prefix = config["run"]["prefix"]
    if not isinstance(prefix, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,30}", prefix):
        problems.append(
            "run.prefix must be 2-31 chars of lowercase letters, digits and hyphens; "
            "it is the namespace teardown sweeps on, so a loose prefix risks "
            "matching real data"
        )

    servers = config["resource_servers"]
    unknown = set(servers) - set(RESOURCE_SERVER_KEYS)
    if unknown:
        problems.append(f"unknown resource_servers: {', '.join(sorted(unknown))}")
    if not any(s.get("enabled") for s in servers.values()):
        problems.append("at least one resource server must be enabled")
    for key, server in servers.items():
        if not server.get("enabled"):
            continue
        for field in ("name", "type", "url", "verify_path"):
            if not server.get(field):
                problems.append(f"resource_servers.{key}.{field} is required when enabled")

    postgres = config["postgres"]
    if postgres["enabled"]:
        for key in ("host", "database", "user", "password"):
            if not postgres.get(key):
                problems.append(f"postgres.{key} is required when postgres.enabled is true")

    databroker = config["databroker"]
    if databroker["enabled"]:
        for key in ("host", "username", "password"):
            if not databroker.get(key):
                problems.append(f"databroker.{key} is required when databroker.enabled is true")

    publish = config["ngsild_publish"]
    # Only an NGSI-LD item gets an exchange, so a run with no NGSI-LD resource
    # server skips the phase at runtime — and must not be made to configure a
    # broker it will never open a connection to.
    ngsild_in_play = any(
        key == "ngsild" or str(server.get("type", "")).lower() in ("ngsi-ld", "ngsild")
        for key, server in servers.items()
        if server.get("enabled")
    )
    if publish["enabled"] and ngsild_in_play:
        # host/username/password may come from either section, so report the
        # pair rather than a key the reader may not have written.
        for key in ("host", "username", "password"):
            if not (publish.get(key) or databroker.get(key)):
                problems.append(
                    f"ngsild_publish.{key} is required when ngsild_publish.enabled is true "
                    f"(or set databroker.{key})"
                )
        for key in ("port", "vhost", "queue_name"):
            if not publish.get(key):
                problems.append(f"ngsild_publish.{key} is required when ngsild_publish.enabled is true")
        for key in ("script", "cert_path"):
            found = resolve_path(publish.get(key))
            if not found or not found.is_file():
                problems.append(f"ngsild_publish.{key} does not exist: {publish.get(key)}")
        if publish.get("data_file"):
            found = resolve_path(publish["data_file"])
            if not found.is_file():
                problems.append(f"ngsild_publish.data_file does not exist: {publish['data_file']}")
        if publish.get("count") not in (None, ""):
            try:
                if int(publish["count"]) < 1:
                    problems.append("ngsild_publish.count must be at least 1")
            except (TypeError, ValueError):
                problems.append(f"ngsild_publish.count is not an integer: {publish['count']!r}")

    gateway_in_play = any(
        key == "gateway" or str(server.get("type", "")).lower() in GATEWAY_TYPES
        for key, server in servers.items()
        if server.get("enabled")
    )
    adaptor = config["gateway_adaptor"]
    if adaptor["enabled"] and gateway_in_play and config["run"]["verify_resource_servers"]:
        for key in ("host", "username", "password"):
            if not (adaptor.get(key) or publish.get(key) or databroker.get(key)):
                problems.append(
                    f"gateway_adaptor.{key} is required when gateway_adaptor.enabled "
                    f"is true (or set ngsild_publish.{key} / databroker.{key})"
                )
        for key in ("port", "vhost", "api_url", "results_key"):
            if not adaptor.get(key):
                problems.append(
                    f"gateway_adaptor.{key} is required when gateway_adaptor.enabled is true"
                )
        for key in ("script", "cert_path"):
            found = resolve_path(adaptor.get(key))
            if not found or not found.is_file():
                problems.append(f"gateway_adaptor.{key} does not exist: {adaptor.get(key)}")

    gateway_delete = config["gateway_delete"]
    if gateway_delete["enabled"] and gateway_in_play:
        for key in ("host", "username", "password"):
            if not (
                gateway_delete.get(key) or adaptor.get(key)
                or publish.get(key) or databroker.get(key)
            ):
                problems.append(
                    f"gateway_delete.{key} is required when gateway_delete.enabled "
                    f"is true (or set gateway_adaptor.{key} / databroker.{key})"
                )
        if not (gateway_delete.get("mgmt_url") or config["ngsild_delete"].get("mgmt_url")):
            problems.append(
                "gateway_delete.mgmt_url is required when gateway_delete.enabled is "
                "true (or set ngsild_delete.mgmt_url — it is the same API)"
            )
        found = resolve_path(gateway_delete.get("script"))
        if not found or not found.is_file():
            problems.append(
                f"gateway_delete.script does not exist: {gateway_delete.get('script')}"
            )
        cert = resolve_path(gateway_delete.get("cert_path") or adaptor.get("cert_path"))
        if not cert or not cert.is_file():
            problems.append("gateway_delete needs a readable cert_path (its own or the adaptor's)")

    ogc_vector_in_play = any(
        is_ogc_vector(server) or (key == "ogc" and is_ogc_vector(dict(server, type="ogc")))
        for key, server in servers.items()
        if server.get("enabled")
    )
    # The organisation name is prefix + a unique part + "-org" and must fit in
    # 24 characters, or OGC onboarding fails deep inside the resource server
    # with 22001, reported only as "Failed to onboard the collection in db.".
    # 14 leaves room for a unique part that still differs between runs.
    if ogc_vector_in_play and isinstance(prefix, str) and len(prefix) > 14:
        problems.append(
            f"run.prefix is {len(prefix)} characters; with an OGC vector server "
            f"enabled it must be 14 or fewer, because the organisation name is "
            f"built from it and the OGC record table caps provider contacts at "
            f"100 characters"
        )

    vector = config["ogc_vector"]
    if vector["enabled"] and ogc_vector_in_play:
        if not (vector.get("base_url") or servers.get("ogc", {}).get("url")):
            problems.append(
                "ogc_vector.base_url is required when ogc_vector.enabled is true "
                "(or set resource_servers.ogc.url)"
            )
        if bool(vector.get("provider_username")) != bool(vector.get("provider_password")):
            problems.append(
                "ogc_vector needs provider_username and provider_password together, "
                "or neither — with neither, the run's own provider onboards"
            )
        for key in ("gpkg_path", "bucket_name", "region"):
            if not vector.get(key):
                problems.append(f"ogc_vector.{key} is required when ogc_vector.enabled is true")
        for key in ("script", "gpkg_path"):
            if vector.get(key):
                found = resolve_path(vector[key])
                if not found or not found.is_file():
                    problems.append(f"ogc_vector.{key} does not exist: {vector[key]}")

    file_in_play = any(
        is_file_server(server) for server in servers.values() if server.get("enabled")
    )
    upload = config["file_upload"]
    if upload["enabled"] and file_in_play:
        if not (upload.get("base_url") or servers.get("file", {}).get("url")):
            problems.append(
                "file_upload.base_url is required when file_upload.enabled is true "
                "(or set resource_servers.file.url)"
            )
        for key in ("script", "file_path"):
            found = resolve_path(upload.get(key))
            if not found or not found.is_file():
                problems.append(f"file_upload.{key} does not exist: {upload.get(key)}")

    file_delete = config["file_delete"]
    if file_delete["enabled"] and file_in_play:
        found = resolve_path(file_delete.get("script"))
        if not found or not found.is_file():
            problems.append(f"file_delete.script does not exist: {file_delete.get('script')}")

    ogc_raster_in_play = any(
        is_ogc_raster(server) for server in servers.values() if server.get("enabled")
    )
    raster = config["ogc_raster"]
    if raster["enabled"] and ogc_raster_in_play:
        for key in ("ingest_script", "upload_script"):
            found = resolve_path(raster.get(key))
            if not found or not found.is_file():
                problems.append(f"ogc_raster.{key} does not exist: {raster.get(key)}")
        found = resolve_path(raster.get("tif_dir"))
        if not found or not found.is_dir():
            problems.append(f"ogc_raster.tif_dir is not a directory: {raster.get('tif_dir')}")
        if not (raster.get("bucket") or config["ogc_vector"].get("bucket_name")):
            problems.append(
                "ogc_raster.bucket is required when ogc_raster.enabled is true "
                "(or set ogc_vector.bucket_name)"
            )
        for key in ("aws_access_key_id", "aws_secret_access_key"):
            if not raster.get(key):
                problems.append(
                    f"ogc_raster.{key} is required when ogc_raster.enabled is true — "
                    f"the uploader writes to the bucket directly"
                )

    s3_cleanup = config["ogc_s3_cleanup"]
    if s3_cleanup["enabled"] and (ogc_vector_in_play or ogc_raster_in_play):
        raster_cfg, vector_cfg = config["ogc_raster"], config["ogc_vector"]
        if not (s3_cleanup.get("bucket") or raster_cfg.get("bucket") or vector_cfg.get("bucket_name")):
            problems.append(
                "ogc_s3_cleanup.bucket is required when an OGC server is enabled "
                "(or set ogc_raster.bucket / ogc_vector.bucket_name)"
            )
        for key in ("aws_access_key_id", "aws_secret_access_key"):
            if not (s3_cleanup.get(key) or raster_cfg.get(key)):
                problems.append(
                    f"ogc_s3_cleanup.{key} is required when an OGC server is "
                    f"enabled (or set ogc_raster.{key}) — the bucket is written "
                    f"to directly"
                )
        found = resolve_path(s3_cleanup.get("script"))
        if not found or not found.is_file():
            problems.append(f"ogc_s3_cleanup.script does not exist: {s3_cleanup.get('script')}")

    raster_delete = config["ogc_raster_delete"]
    if raster_delete["enabled"] and ogc_raster_in_play:
        found = resolve_path(raster_delete.get("script"))
        if not found or not found.is_file():
            problems.append(f"ogc_raster_delete.script does not exist: {raster_delete.get('script')}")

    vector_delete = config["ogc_vector_delete"]
    if vector_delete["enabled"] and ogc_vector_in_play:
        ogc_db = config["ogc_postgres"]
        for key in ("host", "user", "password"):
            if not (ogc_db.get(key) or postgres.get(key)):
                problems.append(
                    f"ogc_postgres.{key} is required when an OGC teardown is "
                    f"enabled (or set postgres.{key})"
                )
        if not ogc_db.get("database"):
            problems.append("ogc_postgres.database is required when an OGC teardown is enabled")
        found = resolve_path(vector_delete.get("script"))
        if not found or not found.is_file():
            problems.append(
                f"ogc_vector_delete.script does not exist: {vector_delete.get('script')}"
            )

    delete = config["ngsild_delete"]
    if delete["enabled"] and ngsild_in_play:
        for key in ("kibana_host", "kibana_username", "kibana_password", "mgmt_url"):
            if not delete.get(key):
                problems.append(f"ngsild_delete.{key} is required when ngsild_delete.enabled is true")
        for key in ("username", "password"):
            if not (delete.get(key) or publish.get(key) or databroker.get(key)):
                problems.append(
                    f"ngsild_delete.{key} is required when ngsild_delete.enabled is true "
                    f"(or set ngsild_publish.{key} / databroker.{key})"
                )
        found = resolve_path(delete.get("script"))
        if not found or not found.is_file():
            problems.append(f"ngsild_delete.script does not exist: {delete.get('script')}")

    sandbox = config["sandbox"]
    if sandbox["enabled"]:
        if not sandbox.get("url"):
            problems.append("sandbox.url is required when sandbox.enabled is true")
        if not sandbox.get("health_path"):
            problems.append("sandbox.health_path is required when sandbox.enabled is true")
        # The refusal check and the authenticated read both call verify_path, so
        # it is required as soon as either is asked for.
        if (sandbox.get("expect_denied") or sandbox.get("bearer_token")) and not sandbox.get(
            "verify_path"
        ):
            problems.append(
                "sandbox.verify_path is required when sandbox.expect_denied or "
                "sandbox.bearer_token is set — both of them call it"
            )

    if sandbox["enabled"] and sandbox.get("token_user"):
        known = ("requester", "consumer", "nopolicy", "cosadmin", "gwrequester")
        if sandbox["token_user"] not in known:
            problems.append(
                f"sandbox.token_user is {sandbox['token_user']!r}, which is not a "
                f"user this run creates; expected one of {', '.join(known)}"
            )

    community = config["community"]
    if community["enabled"]:
        if not community.get("url"):
            problems.append("community.url is required when community.enabled is true")
        if not community.get("health_path"):
            problems.append("community.health_path is required when community.enabled is true")
        services = community.get("services") or {}
        unknown = set(services) - {"discussion", "challenge"}
        if unknown:
            problems.append(
                f"unknown community.services: {', '.join(sorted(unknown))} "
                f"(the service mounts discussion and challenge only)"
            )
        if not any((s or {}).get("enabled") for s in services.values()):
            problems.append(
                "at least one community.services entry must be enabled when "
                "community.enabled is true — otherwise only the health check runs"
            )
        for name, service in services.items():
            if not (service or {}).get("enabled"):
                continue
            # Both the refusal check and the authenticated read call verify_path.
            if (service.get("expect_denied") or community.get("bearer_token")) and not service.get(
                "verify_path"
            ):
                problems.append(
                    f"community.services.{name}.verify_path is required when its "
                    f"expect_denied or community.bearer_token is set — both call it"
                )

    if config["run"]["sweep_database"] and not postgres["enabled"]:
        problems.append("run.sweep_database needs postgres.enabled")
    if config["run"]["delete_audit_rows"] and not config["run"]["sweep_database"]:
        problems.append("run.delete_audit_rows needs run.sweep_database")
    if config["run"]["delete_all_cos_admin_data"] and not config["run"]["delete_audit_rows"]:
        problems.append(
            "run.delete_all_cos_admin_data needs run.delete_audit_rows — it "
            "widens the cos_admin sweep, it does not enable one"
        )

    if problems:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems))


def _resolve_config_path(config_file=None):
    """The config to load: an explicit path, else config.json beside this file."""
    if config_file:
        candidate = Path(config_file)
        if candidate.is_file():
            return candidate
        for directory in _config_locations():
            named = directory / f"{config_file}.json"
            if named.is_file():
                return named
        raise ConfigError(
            f"no config file at {candidate}, nor {config_file}.json beside the "
            f"entry point or the harness"
        )
    for directory in _config_locations():
        candidate = directory / "config.json"
        if candidate.is_file():
            return candidate
    first = _config_locations()[0]
    raise ConfigError(
        f"no config.json found — looked beside the entry point and in "
        f"{', '.join(str(d) for d in _config_locations()[1:])}. Copy "
        f"config.example.json to {first / 'config.json'} and fill it in"
    )


def load(config_file=None, overrides=None):
    """Build the effective config for a run.

    config_file -- path to a config file, or None for config.json beside this
                   module
    overrides   -- "dotted.path=value" strings from --set flags
    """
    path = _resolve_config_path(config_file)
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as err:
        raise ConfigError(f"{path} is not valid JSON: {err}") from err

    config = _deep_merge(DEFAULTS, raw)
    config = _walk(config, _interpolate)
    config = _apply_env(config)

    for override in overrides or []:
        if "=" not in override:
            raise ConfigError(f"--set expects dotted.path=value, got {override!r}")
        dotted, value = override.split("=", 1)
        _assign(config, dotted.strip().split("."), value, f"--set {dotted.strip()}")

    # The OGC raster toggle drives that server's query types, so a run cannot
    # declare STAC while onboarding vectors, or the reverse. Only an explicit
    # true/false takes over; null leaves query_types as written.
    ogc = config["resource_servers"].get("ogc")
    if isinstance(ogc, dict) and ogc.get("raster") is not None:
        ogc["query_types"] = ["STAC"] if _as_bool(ogc["raster"]) else ["FEATURE"]

    # ACL rides on the control plane host unless the deployment splits them.
    if not config["acl"]["base_url"]:
        config["acl"]["base_url"] = config["control_plane"]["base_url"]
    # Items store the APD as a bare host+path, without a scheme.
    if not config["acl"]["apd_url"]:
        config["acl"]["apd_url"] = re.sub(r"^https?://", "", config["acl"]["base_url"])

    # Reports belong beside the entry point, not wherever the run happened to
    # be started from — a relative report_dir would otherwise mean a different
    # directory each time.
    report_dir = config["run"].get("report_dir")
    if report_dir and not Path(report_dir).is_absolute():
        config["run"]["report_dir"] = str(_entry_dir() / report_dir)

    _validate(config)
    config["_config_file"] = str(path)
    return config


def warnings(config):
    """Non-fatal notes worth printing before a run starts."""
    notes = []
    if config["run"]["cleanup"] and not config["run"]["sweep_database"]:
        notes.append(
            "run.sweep_database is off: the APIs only soft-delete policies "
            "(PUT /policy flips ACTIVE to DELETE) and retain access requests, so "
            "those rows will be left behind in Postgres. Enable postgres and "
            "run.sweep_database for a complete teardown."
        )
    if not config["run"]["cleanup"]:
        notes.append("run.cleanup is off: every artefact this run creates will be left in place.")
    return notes


# Resource server types that get a per-item RabbitMQ exchange and Elasticsearch
# index. Only an item on one of these has anything to publish to or tear down.
NGSILD_TYPES = ("ngsi-ld", "ngsild")

# Resource server types served by an RPC adaptor rather than out of storage.
GATEWAY_TYPES = ("gateway",)

# OGC. Vector collections and raster (STAC) are onboarded and torn down by
# different scripts, and the query type is what separates them: STAC is raster,
# FEATURE and ATTR are vector.
OGC_TYPES = ("ogc",)
STAC_QUERY_TYPE = "stac"
VECTOR_QUERY_TYPES = ("feature", "attr")


# The file server: a databank the Files Connect API stores objects under.
FILE_TYPES = ("file",)


def is_file_server(server):
    """Whether a resourceServer entry is a file one."""
    return str(server.get("type", "")).lower() in FILE_TYPES


def file_server(config):
    """The enabled file resource server, or None if the run has none."""
    for key, server in enabled_resource_servers(config):
        if key == "file" or is_file_server(server):
            return key, server
    return None


def gateway_server(config):
    """The enabled gateway resource server, or None if the run has none.

    A gateway item is served by an adaptor consuming from a queue named with the
    item id: no adaptor running, no answer, so the harness has to start one.
    """
    for key, server in enabled_resource_servers(config):
        if key == "gateway" or str(server.get("type", "")).lower() in GATEWAY_TYPES:
            return key, server
    return None


def is_ogc_vector(server):
    """Whether a resourceServer entry is an OGC *vector* one.

    Vector and raster share the OGC server type and are told apart by query
    type: STAC is raster, FEATURE and ATTR are vector. STAC is checked first, so
    an entry carrying both is treated as raster and left to the STAC scripts.

    An OGC entry with no query types at all falls back to vector — the safe way
    round, since a missing declaration should show up as a vector run that fails
    loudly rather than a step silently skipped.
    """
    kind = str(server.get("type", "")).lower()
    if kind not in OGC_TYPES:
        return False
    queries = server.get("query_types") or server.get("queryTypes") or []
    if isinstance(queries, str):
        queries = [queries]
    queries = [str(q).lower() for q in queries]
    if STAC_QUERY_TYPE in queries:
        return False
    if not queries:
        return True
    return any(q in VECTOR_QUERY_TYPES for q in queries)


def is_ogc_raster(server):
    """Whether a resourceServer entry is an OGC *raster* one: OGC, query type STAC."""
    kind = str(server.get("type", "")).lower()
    if kind not in OGC_TYPES:
        return False
    queries = server.get("query_types") or server.get("queryTypes") or []
    if isinstance(queries, str):
        queries = [queries]
    return any(str(q).lower() == STAC_QUERY_TYPE for q in queries)


def ogc_raster_server(config):
    """The enabled OGC raster (STAC) resource server, or None."""
    for key, server in enabled_resource_servers(config):
        if is_ogc_raster(server):
            return key, server
    return None


def ogc_vector_server(config):
    """The enabled OGC vector resource server, or None if the run has none."""
    for key, server in enabled_resource_servers(config):
        if key == "ogc" and is_ogc_vector(dict(server, type=server.get("type", "ogc"))):
            return key, server
        if is_ogc_vector(server):
            return key, server
    return None


def ngsild_server(config):
    """The enabled NGSI-LD resource server, or None if the run has none.

    The type is what is checked, not the config key, because the type is what
    the catalogue item actually carries.
    """
    for key, server in enabled_resource_servers(config):
        if key == "ngsild" or str(server.get("type", "")).lower() in NGSILD_TYPES:
            return key, server
    return None


def enabled_resource_servers(config):
    """The resource servers this run uses, in a stable order."""
    return [
        (key, config["resource_servers"][key])
        for key in RESOURCE_SERVER_KEYS
        if config["resource_servers"].get(key, {}).get("enabled")
    ]


# Keys that match a secret hint but name something else: `token_user` is which
# account to act as, `delete_user` is whose broker user to remove. Masking those
# values would blank ordinary words like "requester" everywhere they appear.
_NOT_SECRET_KEYS = (
    "token_user", "token_kind", "delete_user", "user", "username",
    "kibana_username", "user_client_id", "admin_client_id",
)


def _is_secret_key(key):
    lowered = key.lower()
    if lowered in _NOT_SECRET_KEYS or lowered.endswith(("_user", "_username", "_kind")):
        return False
    return any(hint in lowered for hint in _SECRET_HINTS + ("access_key",))


def secret_values(config):
    """Every credential the resolved config holds, for masking elsewhere.

    Key-based, like `redact`, but returning the values rather than a masked
    copy — so they can be recognised wherever they surface: a script's stdout, a
    URL, an error body.
    """
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, (dict, list)):
                    walk(value)
                elif isinstance(value, str) and value.strip() and _is_secret_key(key):
                    found.append(value.strip())
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(config)
    return found


def redact(config):
    """A copy safe to log or attach to a report: every secret-ish leaf masked."""

    def scrub(node):
        if isinstance(node, dict):
            return {
                key: "***"
                if any(hint in key.lower() for hint in _SECRET_HINTS) and value
                else scrub(value)
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [scrub(v) for v in node]
        return node

    return scrub(config)


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Resolve and print the E2E config.")
    parser.add_argument("config_file", nargs="?", help="path to a config file (default: config.json)")
    parser.add_argument(
        "--set", action="append", default=[], metavar="PATH=VALUE",
        help="override a config key, e.g. --set run.prefix=nightly",
    )
    args = parser.parse_args()

    try:
        print(json.dumps(redact(load(args.config_file, args.set)), indent=2))
    except ConfigError as err:
        print(f"error: {err}", file=sys.stderr)
        sys.exit(1)