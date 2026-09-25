#!/usr/bin/env python3
"""
Publish NGSI-LD records into a catalogue item's own RabbitMQ exchange.

The catalogue creates one exchange per NGSI-LD item, named with the item id, and
the resource server serves what was published there. Until something has been
published, a data-plane GET returns an empty result whether or not the policy
works — so this runs after the access request and before the resource-server
checks, and a run that skips it cannot tell an authorisation failure from an
empty dataset.

The publishing itself is not reimplemented here.
script/NGSILD_Automation_Script/ngsild_publish_v1.py already does it with
publisher confirms and mandatory routing, and is run by hand against real
deployments; the harness invokes that same script through `script_runner`, with
exchange_name set to the item id.
"""

import re

from . import script_runner
from .config import resolve_path

EXIT_MEANING = {
    script_runner.EXIT_OK: "published and confirmed",
    script_runner.EXIT_FAILED: "publish failed",
    script_runner.EXIT_CONFIG: "publisher rejected its configuration",
}


def settings(config):
    """Broker settings for the publisher, with the databroker fallbacks applied."""
    publish = config["ngsild_publish"]
    databroker = config["databroker"]

    def either(key):
        return publish.get(key) or databroker.get(key)

    return {
        "host": either("host"),
        "port": int(publish["port"]),
        "username": either("username"),
        "password": either("password"),
        "vhost": publish["vhost"],
        "cert_path": str(resolve_path(publish["cert_path"])),
        "check_hostname": bool(publish["check_hostname"]),
        "queue_name": publish["queue_name"],
    }


def _published_count(output):
    """The packet count the publisher confirmed, if it said."""
    match = re.search(r"Run SUCCEEDED: (\d+) packet", output)
    return int(match.group(1)) if match else None


def ngsild_publish(ctx, exchange):
    """Publish into `exchange`. Raises AssertionError if the publisher failed.

    Returns the number of packets the broker confirmed, or None when the
    publisher did not report a count.
    """
    publish = ctx.config["ngsild_publish"]
    broker = settings(ctx.config)
    target = f"amqps://{broker['host']}:{broker['port']}/{broker['vhost']} → {exchange}"

    args = []
    if publish.get("data_file"):
        args += ["--data", str(resolve_path(publish["data_file"]))]
    if publish.get("count") not in (None, ""):
        args += ["--count", str(int(publish["count"]))]
    if publish.get("verbose"):
        args.append("--verbose")

    result = script_runner.run(
        ctx,
        resolve_path(publish["script"]),
        EXIT_MEANING,
        files={"run.ini": script_runner.ini_text({
            "rabbitmq": {
                "host": broker["host"],
                "port": broker["port"],
                "username": broker["username"],
                "password": broker["password"],
                "vhost": broker["vhost"],
                "cert_path": broker["cert_path"],
                "check_hostname": broker["check_hostname"],
                # The item id. The publisher also uses it as the routing key: a
                # dotless UUID matches exactly on a topic exchange.
                "exchange_name": exchange,
                "queue_name": broker["queue_name"],
            }
        })},
        config_arg="run.ini",
        label=f"publish to {exchange}",
        target=target,
        args=args,
        verbose=bool(publish["verbose"]),
        retries=int(publish["retries"]),
        retry_delay=int(publish["retry_delay_seconds"]),
        request={
            "exchange": exchange,
            "queue": broker["queue_name"],
            "data_file": publish.get("data_file") or "built-in sample data",
            "count": publish.get("count"),
        },
    )

    if not result.ok:
        raise AssertionError(
            f"publisher exited {result.code} ({result.meaning}) for exchange "
            f"{exchange} on {target}"
        )
    return _published_count(result.output)
