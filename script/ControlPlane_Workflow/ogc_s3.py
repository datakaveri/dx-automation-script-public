#!/usr/bin/env python3
"""
Remove the objects an OGC item left in S3.

The other OGC teardowns clean the platform's own state — collection rows, STAC
items, the ogr2ogr table — but the files themselves stay in the bucket:

  * a vector item leaves `<item id>.gpkg`
  * a raster item leaves a folder `<item id>/` of GeoTIFFs

`script/OGC_Automation_Script/Extra_To_Clean_S3/s3_deleteion.py` finds and
removes either by uuid, and the harness runs it once per item, before the
catalogue item that names those objects is deleted.

Two things about that script shape this module:

  * It prompts for a typed "DELETE" unless `--confirm` is passed. Automation
    always passes it — without it the run would block on stdin forever.

  * It exits 0 whether or not anything was deleted, so the verdict is read from
    its summary (`Errors: 0`, and a non-zero deleted count). "Nothing found" is
    treated as success: a run whose upload failed has nothing in the bucket, and
    teardown should not fail for tidying up after it.
"""

import json
import re

from . import script_runner
from .config import resolve_path

DELETE_MEANING = {
    script_runner.EXIT_OK: "S3 objects removed",
    script_runner.EXIT_FAILED: "the cleanup script could not start",
}

# What each kind of OGC item leaves behind, in the script's own vocabulary.
VECTOR, RASTER = "gpkg", "tif"


def settings(config):
    """Bucket and credentials, falling back to the sections that upload."""
    cleanup = config["ogc_s3_cleanup"]
    raster = config["ogc_raster"]
    vector = config["ogc_vector"]

    def either(key, *fallbacks):
        for source, name in ((cleanup, key), *fallbacks):
            value = source.get(name)
            if value:
                return value
        return None

    return {
        "bucket": either("bucket", (raster, "bucket"), (vector, "bucket_name")),
        "region": either("region", (raster, "region"), (vector, "region")),
        "access_key": either("aws_access_key_id", (raster, "aws_access_key_id")),
        "secret_key": either("aws_secret_access_key", (raster, "aws_secret_access_key")),
    }


def ogc_s3_delete(ctx, item_id, kind):
    """Delete the item's objects from the bucket.

    kind -- VECTOR for `<item>.gpkg`, RASTER for the `<item>/` folder of tiffs

    Returns a ScriptResult; nothing is raised, because this runs during teardown.
    """
    config = ctx.config["ogc_s3_cleanup"]
    values = settings(ctx.config)

    document = {
        "bucket": values["bucket"],
        "region": values["region"],
        "aws_access_key_id": values["access_key"],
        "aws_secret_access_key": values["secret_key"],
    }

    result = script_runner.run(
        ctx,
        resolve_path(config["script"]),
        DELETE_MEANING,
        # The script reads config.json from beside itself, so it runs as a copy
        # in the run directory rather than having AWS keys written into the
        # checkout.
        files={"config.json": json.dumps(document, indent=2)},
        copy_script=True,
        # --confirm is not optional here: without it the script waits on stdin
        # for a typed "DELETE" and the run never returns.
        args=(item_id, kind, "--confirm"),
        collect=("s3_deletion.log",),
        label=f"s3 cleanup {kind} {item_id}",
        target=f"s3://{values['bucket']}/{item_id}{'/' if kind == RASTER else '.gpkg'}",
        verbose=bool(config["verbose"]),
        request={"item": item_id, "kind": kind, "bucket": values["bucket"]},
    )

    if result.ok:
        errors = re.search(r"Errors:\s*(\d+)", result.output)
        if errors and int(errors.group(1)):
            result.code = script_runner.EXIT_FAILED
            result.meaning = f"{errors.group(1)} object(s) could not be deleted"
    return result


def deleted_count(output):
    """How many objects the script reported deleting, for the run log."""
    total = 0
    for field in ("GPKG files deleted", "TIF files deleted"):
        match = re.search(rf"{field}:\s*(\d+)", output)
        if match:
            total += int(match.group(1))
    return total
