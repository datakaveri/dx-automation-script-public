#!/usr/bin/env python3
"""
Upload a file to the item's databank, and delete it again.

A catalogue item is a databank — `adex:DataBank` is the type the harness creates
— and the Files Connect API stores objects under it. Onboarding a file item
therefore means a multipart upload against `databanks/<item id>/uploads`,
optionally followed by a zip/report processing job.

`script/FILE_Automation_Script/creation/file_creation.py` does that, and
`deletion/file_deletion.py` removes the objects again. Unlike the other
automation scripts, both take their config path as a positional argument and
report success through their exit code, so the harness only has to read that.

Both accept a static bearer token, which is what the harness passes: the run's
provider already holds one, so no second credential is configured here.
"""

import json
import os

from . import script_runner
from .config import resolve_path

CREATE_MEANING = {
    script_runner.EXIT_OK: "file uploaded",
    script_runner.EXIT_FAILED: "upload or processing failed",
}

DELETE_MEANING = {
    script_runner.EXIT_OK: "file deleted",
    script_runner.EXIT_FAILED: "one or more files could not be deleted",
}


def settings(config):
    """File server settings, with the resource server's URL as the fallback."""
    upload = config["file_upload"]
    server = config["resource_servers"]["file"]

    base_url = upload.get("base_url")
    if not base_url and server.get("url"):
        url = server["url"]
        base_url = url if url.startswith("http") else f"{server.get('scheme', 'https')}://{url}"

    path = resolve_path(upload["file_path"]) if upload.get("file_path") else None
    return {
        "base_url": (base_url or "").rstrip("/"),
        "api_version": upload["api_version"],
        "verify_tls": bool(upload["verify_tls"]),
        "request_timeout_seconds": upload["request_timeout_seconds"],
        "file_path": str(path) if path else None,
        # The object key inside the databank; the file's own name by default.
        "key": upload.get("key") or (path.name if path else None),
    }


def _common(values, token, item_id):
    """The half of the config both scripts share."""
    return {
        "base_url": values["base_url"],
        "api_version": values["api_version"],
        "token": token,
        "verify_tls": values["verify_tls"],
        "request_timeout_seconds": values["request_timeout_seconds"],
        # The databank is the catalogue item.
        "databank_id": item_id,
    }


def file_upload(ctx, item_id, token):
    """Upload the configured file into `item_id`'s databank.

    Raises AssertionError if the script reported a failure. Returns the object
    key, which teardown needs to delete it again.
    """
    upload = ctx.config["file_upload"]
    values = settings(ctx.config)

    document = dict(
        _common(values, token, item_id),
        max_retries=upload["max_retries"],
        part_size_mb=upload["part_size_mb"],
        processing={
            "enabled": bool(upload["processing_enabled"]),
            "type": upload["processing_type"],
            "include_uploaded_files_only": bool(upload["processing_uploaded_only"]),
            "wait_for_completion": bool(upload["processing_wait"]),
            "poll_interval_seconds": upload["processing_poll_seconds"],
            "timeout_seconds": upload["processing_timeout_seconds"],
        },
        files=[
            {
                "file_path": values["file_path"],
                "key": values["key"],
                **({"content_type": upload["content_type"]} if upload.get("content_type") else {}),
            }
        ],
    )

    result = script_runner.run(
        ctx,
        resolve_path(upload["script"]),
        CREATE_MEANING,
        files={"file_creation_config.json": json.dumps(document, indent=2)},
        positional_config="file_creation_config.json",
        label=f"file upload {values['key']} → {item_id}",
        target=f"{values['base_url']}/{values['api_version']}/databanks/{item_id}",
        verbose=bool(upload["verbose"]),
        request={"item": item_id, "file": values["file_path"], "key": values["key"]},
    )

    if not result.ok:
        raise AssertionError(
            f"file upload exited {result.code} ({result.meaning}) for {item_id} — "
            f"its log above says which stage failed"
        )
    return values["key"]


def file_delete(ctx, item_id, token, keys):
    """Delete `keys` from the item's databank.

    Returns a ScriptResult; nothing is raised, because this runs during teardown.
    """
    delete = ctx.config["file_delete"]
    values = settings(ctx.config)

    document = dict(
        _common(values, token, item_id),
        files=[{"key": key} for key in keys],
    )

    return script_runner.run(
        ctx,
        resolve_path(delete["script"]),
        DELETE_MEANING,
        files={"file_deletion_config.json": json.dumps(document, indent=2)},
        positional_config="file_deletion_config.json",
        label=f"file teardown {item_id}",
        target=f"{values['base_url']}/{values['api_version']}/databanks/{item_id}/files/delete",
        verbose=bool(delete["verbose"]),
        request={"item": item_id, "keys": list(keys)},
    )
