#!/usr/bin/env python3
"""
Remove the broker and Elasticsearch objects the catalogue created for an item.

Onboarding an NGSI-LD item does not only write a catalogue document. The
databroker creates an exchange named with the item id, the resource server's
consumer creates an Elasticsearch index (`iudx-v2__<item id>`), and the
catalogue creates a RabbitMQ user for the provider. Deleting the catalogue item
removes none of them, so a harness that only calls the APIs leaves broker state
behind on every run — and unlike a stray database row, nothing on the platform
still names it afterwards.

As with publishing, the work is not reimplemented:
script/NGSILD_Automation_Script/ngsild_delete_v1.py already does it in the order
that works (index, then exchange, then user), and knows the awkward parts — the
Kibana console proxy answers 200 for everything, so the real Elasticsearch
status has to be read out of the body. The harness runs that script through
`script_runner`.
"""

from urllib.parse import quote

import requests

from . import script_runner
from .config import resolve_path

EXIT_MEANING = {
    script_runner.EXIT_OK: "index, exchange and broker user removed",
    script_runner.EXIT_FAILED: "at least one teardown step failed",
    script_runner.EXIT_CONFIG: "teardown script rejected its configuration",
}


def settings(config):
    """Settings for the teardown script, with the fallbacks applied.

    Broker credentials fall back to ngsild_publish and then databroker: it is
    one broker, reached with one admin credential. Only the management URL and
    the Kibana details are specific to this script.
    """
    delete = config["ngsild_delete"]
    publish = config["ngsild_publish"]
    databroker = config["databroker"]

    def either(key):
        return delete.get(key) or publish.get(key) or databroker.get(key)

    return {
        "kibana_host": delete["kibana_host"],
        "kibana_username": delete["kibana_username"],
        "kibana_password": delete["kibana_password"],
        "mgmt_url": delete["mgmt_url"],
        "username": either("username"),
        "password": either("password"),
        "vhost": delete.get("vhost") or publish["vhost"],
        "index_prefix": delete["index_prefix"],
        "mgmt_verify": delete["mgmt_verify"],
    }


# How long a console-proxy call may take. Listing every index is the slow one.
KIBANA_TIMEOUT = 60


def _kibana(ctx, path, method):
    """One Elasticsearch call through the Kibana console proxy.

    Elasticsearch is not exposed directly, so every call is a POST to
    /api/console/proxy carrying the real method in the query string. The proxy
    answers HTTP 200 whatever Elasticsearch said, so the body is the only
    account of what happened — the same rule ngsild_delete_v1.py works to.

    Returns the parsed body. Raises on a transport failure or a refused login,
    which are the two cases a caller cannot do anything sensible with.
    """
    values = settings(ctx.config)
    host = values["kibana_host"].rstrip("/")
    url = f"{host}/api/console/proxy?path={quote(path, safe='')}&method={method}"

    response = requests.post(
        url,
        headers={"kbn-xsrf": "true", "Content-Type": "application/json"},
        auth=(values["kibana_username"], values["kibana_password"]),
        timeout=KIBANA_TIMEOUT,
    )
    if response.status_code == 401:
        raise RuntimeError(
            f"Kibana rejected credentials for {values['kibana_username']!r}"
        )
    try:
        return response.json() if response.text else None
    except ValueError:
        raise RuntimeError(
            f"Kibana returned a non-JSON body for {method} {path}: "
            f"{response.text[:200]}"
        )


def list_indices(ctx):
    """{index name: document count} for every index under the configured prefix.

    Document counts come along because they are what makes a sweep line worth
    reading: "deleted an empty index" and "deleted 2.7 million documents" should
    not look the same in the log.
    """
    prefix = settings(ctx.config)["index_prefix"]
    rows = _kibana(
        ctx, f"_cat/indices/{prefix}*?format=json&h=index,docs.count", "GET"
    )
    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected _cat/indices response: {str(rows)[:200]}")
    return {
        row["index"]: row.get("docs.count")
        for row in rows
        if isinstance(row, dict) and row.get("index")
    }


def delete_index(ctx, index):
    """Delete one index, documents and all. True when Elasticsearch acknowledged."""
    body = _kibana(ctx, index, "DELETE")
    if isinstance(body, dict) and body.get("acknowledged"):
        return True
    # A 404 body is success as far as teardown is concerned: it is already gone.
    if isinstance(body, dict) and body.get("status") == 404:
        return True
    raise RuntimeError(f"elasticsearch did not acknowledge: {str(body)[:200]}")


def ngsild_delete(ctx, exchange, broker_user):
    """Tear down `exchange`, its index and `broker_user`.

    Returns a ScriptResult; nothing is raised, because this runs during teardown
    where one failure must not strand the artefacts behind it.
    """
    delete = ctx.config["ngsild_delete"]
    settings_ = settings(ctx.config)
    target = f"{settings_['mgmt_url']} → {exchange}"

    args = []
    if delete.get("dry_run"):
        args.append("--dry-run")
    if delete.get("verbose"):
        args.append("--verbose")

    return script_runner.run(
        ctx,
        resolve_path(delete["script"]),
        EXIT_MEANING,
        files={"run.ini": script_runner.ini_text({
            "kibana": {
                "host": settings_["kibana_host"],
                "username": settings_["kibana_username"],
                "password": settings_["kibana_password"],
            },
            "rabbitmq": {
                "username": settings_["username"],
                "password": settings_["password"],
                "vhost": settings_["vhost"],
                "exchange_name": exchange,
                "mgmt_url": settings_["mgmt_url"],
                # The provider's broker user, created by the catalogue and named
                # with their Keycloak id. The script refuses to run if this is
                # the credential it authenticates with.
                "delete_user": broker_user,
                "index_prefix": settings_["index_prefix"],
                "mgmt_verify": settings_["mgmt_verify"],
            },
        })},
        config_arg="run.ini",
        label=f"ngsi-ld teardown {exchange}",
        target=target,
        args=args,
        verbose=bool(delete["verbose"]),
        request={
            "exchange": exchange,
            "index": f"{settings_['index_prefix']}{exchange}",
            "broker_user": broker_user,
            "dry_run": bool(delete.get("dry_run")),
        },
    )
