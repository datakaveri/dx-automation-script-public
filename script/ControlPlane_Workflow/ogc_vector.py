#!/usr/bin/env python3
"""
Onboard a vector collection for an OGC item, and tear it down again.

An OGC item is not served from anything the harness creates by publishing. The
GeoPackage has to be uploaded and onboarded through the OGC processes API: a
pre-signed S3 URL is requested, the file is PUT to it, a collection-onboarding
process is triggered, and its job is polled until it reports SUCCESSFUL. Until
that job succeeds the collection does not exist and a read returns nothing.

`script/OGC_Automation_Script/Vector_Automation/Creation/vector_creation.py`
already does all of that, so the harness runs it with a config naming this run's
item as the resource id.

Two things about these scripts differ from the NGSI-LD and gateway ones:

  * Their config is JSON, not INI, and neither takes a --config flag. Creation
    reads `./config.json` from its working directory; deletion reads
    `db_config.json` from *its own* directory, so it is run as a copy inside the
    run directory rather than having credentials written into the checkout.

  * Neither reports failure through its exit code. Creation logs errors and
    exits 0 whatever happened; deletion returns normally after logging a failed
    transaction. So success is read out of their output, and a run that produced
    no evidence of success is treated as a failure rather than a pass.
"""

import json
import re
import time

import psycopg2
from psycopg2 import sql

from . import script_runner
from .client import ApiClient
from .config import resolve_path

# Both scripts exit 0 regardless, so the exit code only distinguishes "ran" from
# "crashed"; the verdict comes from the output.
# Fixed platform process ids: the same on every deployment, so they are not
# config a run can get wrong. If one ever changes, it changes here.
PRESIGNED_URL_PROCESS_ID = "5eee0ed4-2abb-4f24-9ffb-e69362bc0777"
COLLECTION_ONBOARDING_PROCESS_ID = "9a3eadda-167d-4043-98be-246f3c66cb7a"

CREATE_MEANING = {
    script_runner.EXIT_OK: "onboarding script ran",
    script_runner.EXIT_FAILED: "onboarding script crashed",
}

DELETE_MEANING = {
    script_runner.EXIT_OK: "deletion script ran",
    script_runner.EXIT_FAILED: "deletion script crashed",
}


def create_settings(config):
    """Creation settings, with the OGC server's own URL as the fallback base."""
    vector = config["ogc_vector"]
    server = config["resource_servers"]["ogc"]

    base_url = vector.get("base_url")
    if not base_url and server.get("url"):
        url = server["url"]
        base_url = url if url.startswith("http") else f"{server.get('scheme', 'https')}://{url}"

    return {
        "base_url": base_url,
        "bucket_name": vector["bucket_name"],
        "region": vector["region"],
        "batch_size": vector["batch_size"],
        "gpkg_path": str(resolve_path(vector["gpkg_path"])) if vector.get("gpkg_path") else None,
    }


def _ogc_connection(ctx):
    """A connection to the OGC server's own database."""
    values = delete_settings(ctx.config)
    options = f"-c search_path={values['schema']}" if values["schema"] else None
    return psycopg2.connect(
        host=values["host"], port=values["port"], dbname=values["database"],
        user=values["user"], password=values["password"],
        options=options, connect_timeout=15,
    )


def ensure_provider_role(ctx, user_id):
    """Give the provider a `roles` row in the OGC database, if it has none.

    `ri_details.role_id` is a foreign key onto `roles(user_id)`, and every
    provider who has ever onboarded successfully on this deployment already has
    that row. A provider created minutes ago does not, so onboarding fails
    inside the transaction and is reported as the catch-all "Failed to onboard
    the collection in db.".

    The platform is supposed to write this row itself as part of onboarding, so
    this is a workaround for the harness's throwaway providers, not something a
    real provider needs. Returns True if a row was actually inserted, so
    teardown removes only what this created.
    """
    connection = _ogc_connection(ctx)
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO roles (user_id, role) VALUES (%s::uuid, 'PROVIDER') "
                "ON CONFLICT DO NOTHING",
                (user_id,),
            )
            return cursor.rowcount > 0
    finally:
        connection.close()


def remove_provider_role(ctx, user_id):
    """Remove a roles row this run created. Runs after the collection's own rows
    are gone, since ri_details references it."""
    connection = _ogc_connection(ctx)
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM roles WHERE user_id = %s::uuid", (user_id,))
            return cursor.rowcount
    finally:
        connection.close()


# collections_details.title. The namespace has to survive a trim, so it goes
# first and the human-readable label is what gets cut.
TITLE_LIMIT = 100

# collections_details is the parent of eight tables and only four of them
# cascade on delete. vector_deletion.py clears collections_enclosure but none of
# the three STAC ones, so a raster collection whose items are still in the
# database fails the DELETE with a foreign-key violation — and because the
# script runs every step in one transaction, the rollback undoes the ri_details
# and collections_enclosure deletes too. Nothing at all is removed, and the
# collection becomes permanently undeletable by this harness.
#
# The STAC API teardown normally empties these first. This is what makes the
# deletion work anyway when that could not run: an orphan whose owner is gone
# from Keycloak has no token to call that API with.
BLOCKING_CHILDREN = (
    ("stac_items_assets", "collection_id"),
    ("stac_collections_assets", "stac_collections_id"),
    ("stac_collections_part", "collection_id"),
)


def collection_title(ctx, label):
    """The collection title, namespaced so the sweep has something to match on.

    The OGC database carries no column the run prefix would otherwise reach --
    a collection is keyed by the item uuid and its table is *named* the uuid --
    so the title is its anchor, exactly as organizations.name is in the
    ControlPlane database.
    """
    return f"{ctx.namespace} {label}"[:TITLE_LIMIT]


def clear_blocking_children(ctx, item_id):
    """Delete the STAC rows that would otherwise fail the collection delete.

    Returns {table: rows removed}. Runs with autocommit so that a deployment
    missing one of these tables loses only that statement rather than poisoning
    the transaction the rest of them share.
    """
    removed = {}
    connection = _ogc_connection(ctx)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            for table, column in BLOCKING_CHILDREN:
                try:
                    cursor.execute(
                        sql.SQL("DELETE FROM {} WHERE {} = %s::uuid").format(
                            sql.Identifier(table), sql.Identifier(column)
                        ),
                        (item_id,),
                    )
                except psycopg2.Error:
                    continue  # not every deployment carries every STAC table
                if cursor.rowcount:
                    removed[table] = cursor.rowcount
    finally:
        connection.close()
    return removed


def find_collections(ctx, pattern, user_ids=(), labels=()):
    """Collections in the OGC database that belong to this harness.

    Returns [(id, title, owner_id, has_stac)]. `has_stac` decides which shape
    the S3 cleanup looks for, and separates a raster collection from a vector
    one without the catalogue item that would otherwise say which it is.

    Three anchors, because no single one reaches everything:

      * `pattern` matches the namespaced title every collection onboarded after
        this was written carries — the durable anchor, and the only one that
        works once a run's users are gone;
      * `user_ids` matches ri_details.role_id, the owner's Keycloak id, which
        reaches a collection whatever its title says, for as long as that
        account still exists;
      * `labels` are (title, description) pairs taken from the config, matched
        exactly and *together*. They are what reaches collections left by runs
        that predate the namespaced title. Both halves are required because a
        title alone is not specific enough to delete on — someone may well have
        called a collection "test" — whereas a collection carrying both this
        harness's label and its "Safe to delete." description is unambiguously
        one of ours. A pair with either half unset is skipped for the same
        reason.
    """
    clauses = [
        "cd.title LIKE %(pattern)s",
        "cd.description LIKE %(pattern)s",
        "rd.role_id = ANY(%(user_ids)s::uuid[])",
    ]
    params = {"pattern": pattern, "user_ids": [str(u) for u in user_ids]}
    for index, (title, description) in enumerate(labels):
        if not title or not description:
            continue
        clauses.append(
            f"(cd.title = %(title{index})s AND cd.description = %(description{index})s)"
        )
        params[f"title{index}"] = title
        params[f"description{index}"] = description

    connection = _ogc_connection(ctx)
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT cd.id::text, cd.title, rd.role_id::text "
                "FROM collections_details cd "
                "LEFT JOIN ri_details rd ON rd.id = cd.id "
                "WHERE " + " OR ".join(clauses),
                params,
            )
            found = cursor.fetchall()

            cursor.execute("SELECT to_regclass('stac_collections_part') IS NOT NULL")
            has_stac_table = cursor.fetchone()[0]

            rows = []
            for collection_id, title, owner_id in found:
                stac = False
                if has_stac_table:
                    cursor.execute(
                        "SELECT EXISTS (SELECT 1 FROM stac_collections_part "
                        "WHERE collection_id = %s::uuid)",
                        (collection_id,),
                    )
                    stac = bool(cursor.fetchone()[0])
                rows.append((collection_id, title, owner_id, stac))
            return rows
    finally:
        connection.close()


def ogc_vector_create(ctx, item_id, token):
    """Upload and onboard the GeoPackage as `item_id`'s collection.

    Raises AssertionError unless the onboarding job reported SUCCESSFUL — the
    script exits 0 either way, so the exit code alone would let a failed
    onboarding pass as a working one.

    Retried, because the item is seconds old when this runs: a resource server
    that learns about catalogue items asynchronously can reject the onboarding
    for a resource it has not seen yet, and that resolves itself with time. A
    failure that is not a race simply fails again, more slowly.
    """
    vector = ctx.config["ogc_vector"]
    attempts = int(vector["retries"]) + 1
    for attempt in range(1, attempts + 1):
        try:
            return _create_once(ctx, item_id, token, attempt, attempts)
        except AssertionError:
            if attempt == attempts:
                raise
            delay = int(vector["retry_delay_seconds"])
            print(
                f"    onboarding attempt {attempt}/{attempts} failed; "
                f"retrying in {delay}s",
                flush=True,
            )
            time.sleep(delay)


def _create_once(ctx, item_id, token, attempt=1, attempts=1):
    """One upload-and-onboard run."""
    vector = ctx.config["ogc_vector"]
    values = create_settings(ctx.config)

    document = {
        "base_url": values["base_url"],
        "bucket_name": values["bucket_name"],
        "region": values["region"],
        "presigned_url_process_id": PRESIGNED_URL_PROCESS_ID,
        "collection_onboarding_process_id": COLLECTION_ONBOARDING_PROCESS_ID,
        "batch_size": values["batch_size"],
        "token": token,
        "files": [
            {
                "file_path": values["gpkg_path"],
                "label": collection_title(ctx, vector["label"]),
                "description": vector["description"],
                # The resource id the collection is onboarded under: the item.
                "ri_uuid": item_id,
            }
        ],
    }

    result = script_runner.run(
        ctx,
        resolve_path(vector["script"]),
        CREATE_MEANING,
        # No --config: the script reads ./config.json from its working directory.
        files={"config.json": json.dumps(document, indent=2)},
        cwd_in_temp=True,
        # It writes its own timestamped log file and puts the useful errors
        # there rather than on stdout.
        collect=("*.log",),
        label=f"ogc vector onboarding {item_id}"
              + (f" (attempt {attempt}/{attempts})" if attempts > 1 else ""),
        target=f"{values['base_url']}/processes → {item_id}",
        verbose=bool(vector["verbose"]),
        request={
            "item": item_id,
            "gpkg": values["gpkg_path"],
            "bucket": values["bucket_name"],
        },
    )

    if not result.ok:
        raise AssertionError(
            f"OGC vector onboarding crashed (exit {result.code}) for {item_id}"
        )
    if "Status: SUCCESSFUL" not in result.output or "Status: FAILED" in result.output:
        # The script prints the job's status and nothing else, so the reason the
        # platform rejected the GeoPackage is only in the job record itself.
        detail = _job_detail(ctx, values["base_url"], token, result.output)
        raise AssertionError(
            f"OGC vector onboarding did not report SUCCESSFUL for {item_id} — "
            f"the collection was not created{detail}"
        )
    return True


def _job_detail(ctx, base_url, token, output):
    """Fetch the failed job so the run says *why*, not just that it failed.

    Best effort: a job that cannot be read must not replace the real failure
    with an error about reading it.
    """
    match = re.search(r"Job ([0-9a-fA-F-]{36}) Status: FAILED", output)
    if not match:
        return " (no job id in the script's output; its log above is the only account)"

    job_id = match.group(1)
    try:
        client = ApiClient(base_url, ctx.recorder, ctx.config["control_plane"]["timeout_seconds"])
        payload = client.get(
            f"/jobs/{job_id}",
            f"read failed onboarding job {job_id}",
            token=token,
            expect=(200, 401, 403, 404),
        )
    except Exception as error:  # noqa: BLE001 - diagnostics must not mask the failure
        return f" (job {job_id}; its record could not be read: {error})"

    body = json.dumps(payload, default=str)[:800]
    if isinstance(payload, dict):
        reason = payload.get("message") or payload.get("detail") or payload.get("description")
        if reason:
            # The message is the platform's summary; the rest of the record
            # sometimes carries the step that actually broke.
            return f" — job {job_id}: {reason}\n      full job record: {body}"
    return f" — job {job_id}: {body}"


def delete_settings(config):
    """The OGC server's own database connection, shared by every OGC teardown.

    Host and credentials fall back to the `postgres` section — the same server,
    reached as the admin — but the database and schema are the OGC server's own.
    """
    ogc_db = config["ogc_postgres"]
    postgres = config["postgres"]

    return {
        "host": ogc_db.get("host") or postgres.get("host"),
        "port": int(ogc_db.get("port") or postgres.get("port") or 5432),
        "database": ogc_db["database"],
        # Optional: unset means the connection's own default search_path.
        "schema": ogc_db.get("schema"),
        "user": ogc_db.get("user") or postgres.get("user"),
        "password": ogc_db.get("password") or postgres.get("password"),
    }


def ogc_vector_delete(ctx, item_id):
    """Delete the collection's rows and its table.

    Returns a ScriptResult with `ok` reflecting the script's own account of the
    transaction, not merely that it ran. Nothing is raised: this is teardown.

    The STAC children go first, over our own connection: they are the rows that
    would fail the script's DELETE and roll its whole transaction back. A
    collection that never had any loses nothing by the attempt.
    """
    try:
        cleared = clear_blocking_children(ctx, item_id)
    except psycopg2.Error as error:
        # Never fatal, and never silent: the script runs anyway and reports the
        # foreign-key violation this would have prevented.
        print(f"    ogc vector teardown: could not clear STAC rows ({error})")
    else:
        if cleared:
            detail = ", ".join(f"{n} from {t}" for t, n in sorted(cleared.items()))
            print(f"    ogc vector teardown: cleared {detail}")

    delete = ctx.config["ogc_vector_delete"]
    values = delete_settings(ctx.config)

    document = {
        "host": values["host"],
        "port": values["port"],
        "database": values["database"],
        "databaseUser": values["user"],
        "databasePassword": values["password"],
        # The collection id, which is the item id. Step 4 of the script drops a
        # table of this name.
        "record_id": item_id,
    }

    result = script_runner.run(
        ctx,
        resolve_path(delete["script"]),
        DELETE_MEANING,
        # The script looks for db_config.json *beside itself*, so it runs as a
        # copy in the run directory rather than having credentials written into
        # the checkout.
        files={"db_config.json": json.dumps(document, indent=2)},
        copy_script=True,
        # A configured schema is set on the connection rather than in the SQL —
        # the script's statements are unqualified and its config has no schema
        # field. libpq reads PGOPTIONS, which is what psycopg2 connects through.
        env={"PGOPTIONS": f"-c search_path={values['schema']}"} if values["schema"] else None,
        label=f"ogc vector teardown {item_id}",
        target=(
            f"postgres://{values['host']}:{values['port']}/{values['database']}"
            + (f"?schema={values['schema']}" if values["schema"] else "")
            + f" → {item_id}"
        ),
        verbose=bool(delete["verbose"]),
        request={
            "item": item_id,
            "database": values["database"],
            "schema": values["schema"],
        },
    )

    if result.ok and "Cleanup completed successfully" not in result.output:
        # It logs the failure and returns normally, so a zero exit on its own
        # would report a rolled-back transaction as a successful teardown.
        result.code = script_runner.EXIT_FAILED
        result.meaning = "deletion reported a failed transaction"
    return result
