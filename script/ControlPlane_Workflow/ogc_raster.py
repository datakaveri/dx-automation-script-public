#!/usr/bin/env python3
"""
Onboard a STAC (raster) collection for an OGC item, and tear it down again.

An OGC item whose query types include STAC is a raster one, and nothing about
its pipeline resembles the vector path:

  1. `Raster_Automation/Creation/stac_injestion.py` reads the GeoTIFFs, builds a
     STAC Collection and one Item per file, and POSTs both to the STAC API.
  2. `Raster_Automation/S3Upload/S3.py` uploads the GeoTIFFs themselves to the
     bucket the items' asset hrefs point at.

Both are needed: the items describe assets that do not exist until the upload
lands, and the upload is meaningless without the items describing it.

The asset href is `<collection id>/<file name>` while S3.py keys objects as
`<directory name>/<file name>`, so the two only agree when the directory is
named after the collection. The harness stages the rasters into a directory
named with the item id — symlinks, so a folder of large GeoTIFFs is not copied
per run — and points both scripts at that.

Neither script reports failure through its exit code (the ingestion logs errors
and returns; the upload prints a summary), so success is read out of the output.
"""

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from . import script_runner
from .config import resolve_path
from .ogc_vector import collection_title

INGEST_MEANING = {
    script_runner.EXIT_OK: "ingestion script ran",
    script_runner.EXIT_FAILED: "ingestion script crashed",
}

UPLOAD_MEANING = {
    script_runner.EXIT_OK: "upload script ran",
    script_runner.EXIT_FAILED: "upload script failed to start",
}

DELETE_MEANING = {
    script_runner.EXIT_OK: "STAC items deleted",
    script_runner.EXIT_FAILED: "one or more items could not be deleted",
}

RASTER_SUFFIXES = (".tif", ".tiff", ".aux.xml")


def settings(config):
    """Raster settings, with the OGC server and vector fallbacks applied."""
    raster = config["ogc_raster"]
    vector = config["ogc_vector"]
    server = config["resource_servers"]["ogc"]

    base_url = raster.get("base_url") or vector.get("base_url")
    if not base_url and server.get("url"):
        url = server["url"]
        base_url = url if url.startswith("http") else f"{server.get('scheme', 'https')}://{url}"
    base_url = (base_url or "").rstrip("/")

    return {
        "base_url": base_url,
        # The STAC API sits under /stac on the same host.
        "api_url": f"{base_url}/stac/collections",
        "api_base": f"{base_url}/stac",
        "tif_dir": str(resolve_path(raster["tif_dir"])) if raster.get("tif_dir") else None,
        # The bucket the vector path already names; rasters land in the same one.
        "bucket": raster.get("bucket") or vector.get("bucket_name"),
        "region": raster.get("region") or vector.get("region"),
        "endpoint": raster.get("endpoint"),
        "access_key": raster.get("aws_access_key_id"),
        "secret_key": raster.get("aws_secret_access_key"),
    }


def _stage(item_id, source_dir):
    """Symlink the rasters into a directory named with the item id.

    Returns (root, staged_dir). The caller removes the root.
    """
    root = tempfile.mkdtemp(prefix="dx-e2e-raster-")
    staged = os.path.join(root, item_id)
    os.makedirs(staged)

    linked = 0
    for path in sorted(Path(source_dir).rglob("*")):
        if path.is_file() and path.name.lower().endswith(RASTER_SUFFIXES):
            os.symlink(path.resolve(), os.path.join(staged, path.name))
            linked += 1
    return root, staged, linked


def ogc_raster_create(ctx, item_id, token):
    """Ingest the STAC collection and upload its rasters.

    Raises AssertionError if either half did not report success — both scripts
    exit 0 regardless, so their output is the only account of what happened.
    """
    raster = ctx.config["ogc_raster"]
    values = settings(ctx.config)

    root, staged, count = _stage(item_id, values["tif_dir"])
    if not count:
        shutil.rmtree(root, ignore_errors=True)
        raise AssertionError(
            f"no GeoTIFFs found under {values['tif_dir']} — nothing to onboard"
        )
    print(f"    staged {count} raster(s) as {item_id}/", flush=True)

    try:
        _ingest(ctx, item_id, token, values, staged, raster)
        settle = raster["ingest_settle_seconds"]
        if settle:
            print(f"    waiting {settle}s before uploading the rasters", flush=True)
            time.sleep(settle)

        _upload(ctx, item_id, values, staged, raster)
        settle = raster["upload_settle_seconds"]
        if settle:
            print(f"    waiting {settle}s for the uploaded rasters to be served", flush=True)
            time.sleep(settle)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    return count


def _ingest(ctx, item_id, token, values, staged, raster):
    """STAC collection + items, posted to the API."""
    output_dir = os.path.join(os.path.dirname(staged), "stac-output")
    document = {
        "paths": {"input_dir": staged, "output_dir": output_dir},
        "stac": {
            "collection_id": item_id,
            # null means "use the collection id", which is what the S3 keys use.
            "href_prefix_override": None,
            "title": collection_title(ctx, raster["title"]),
            "description": raster["description"],
        },
        "api": {
            "push_to_api": True,
            "api_url": values["api_url"],
            "api_base": values["api_base"],
            "auth_token": token,
            "timeout_seconds": raster["timeout_seconds"],
        },
    }

    result = script_runner.run(
        ctx,
        resolve_path(raster["ingest_script"]),
        INGEST_MEANING,
        files={"config.json": json.dumps(document, indent=2)},
        args=("--config", "config.json"),
        cwd_in_temp=True,
        label=f"stac ingestion {item_id}",
        target=f"{values['api_url']} → {item_id}",
        verbose=bool(raster["verbose"]),
        request={"item": item_id, "input_dir": staged},
    )

    if not result.ok:
        raise AssertionError(f"STAC ingestion crashed (exit {result.code}) for {item_id}")
    if "posted successfully" not in result.output:
        raise AssertionError(
            f"STAC ingestion did not report the collection posted for {item_id} — "
            f"its log above is the only account (the script exits 0 either way)"
        )
    if "Batch item payload posted successfully" not in result.output:
        raise AssertionError(
            f"STAC collection {item_id} was created but its items were not posted"
        )


def _upload(ctx, item_id, values, staged, raster):
    """The GeoTIFFs themselves, to the bucket the items point at."""
    document = {
        "bucket": values["bucket"],
        "region": values["region"],
        "aws_access_key_id": values["access_key"],
        "aws_secret_access_key": values["secret_key"],
        "directory": staged,
    }
    if values["endpoint"]:
        document["endpoint"] = values["endpoint"]

    result = script_runner.run(
        ctx,
        resolve_path(raster["upload_script"]),
        UPLOAD_MEANING,
        # No --config: the script reads ./config.json from its working directory.
        files={"config.json": json.dumps(document, indent=2)},
        cwd_in_temp=True,
        label=f"raster upload {item_id}",
        target=f"s3://{values['bucket']}/{item_id}/",
        verbose=bool(raster["verbose"]),
        request={"item": item_id, "bucket": values["bucket"], "directory": staged},
    )

    if not result.ok:
        raise AssertionError(
            f"raster upload exited {result.code} for {item_id} — the bucket was "
            f"unreachable or refused the credentials"
        )
    if "Failed      : 0" not in result.output:
        raise AssertionError(
            f"raster upload reported failures for {item_id}; its summary above "
            f"says how many"
        )


def ogc_raster_delete(ctx, item_id, token):
    """Delete every STAC item in the collection.

    Returns a ScriptResult; nothing is raised, because this runs during teardown.
    """
    delete = ctx.config["ogc_raster_delete"]
    values = settings(ctx.config)

    document = {
        "api_base": values["api_url"],
        "token": token,
        "collection_id": item_id,
        "batch_size": delete["batch_size"],
        "delete_delay": delete["delete_delay"],
        "request_timeout": delete["request_timeout"],
    }

    return script_runner.run(
        ctx,
        resolve_path(delete["script"]),
        DELETE_MEANING,
        # The script reads config.json from *beside itself*, so it runs as a
        # copy in the run directory rather than having a token written into the
        # checkout.
        files={"config.json": json.dumps(document, indent=2)},
        copy_script=True,
        collect=("stac_deletion_*.log",),
        label=f"stac items teardown {item_id}",
        target=f"{values['api_url']}/{item_id}/items",
        verbose=bool(delete["verbose"]),
        request={"item": item_id},
    )
