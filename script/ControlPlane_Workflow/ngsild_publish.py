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
from datetime import datetime, timedelta

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
    spread = float(publish.get("spread_hours") or 0)
    if spread:
        args += ["--spread-hours", str(spread)]
    anchor = _observation_end(publish.get("observation_end"))
    if publish.get("observation_end"):
        args += ["--observation-end", str(publish["observation_end"])]
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
    ctx.ngsild_window = observation_window(spread, anchor)
    return _published_count(result.output)


# Padding on each end of the published window. A temporal query asking for
# exactly the range the data occupies is a boundary test, not a data test: the
# oldest and newest rows sit on the edge, and whether they come back depends on
# whether the server reads the range as inclusive. The window handed to the
# collections is deliberately wider than the data, so a query over it is asking
# "is the data there", which is the question.
_WINDOW_MARGIN = timedelta(hours=1)

# The publisher stamps a naive local clock reading and appends this offset
# literally (its DEFAULT_TIMEZONE_SUFFIX). Both halves of that are reproduced
# here rather than corrected, because the point is to describe the timestamps
# that were actually written: a window built from a different notion of "now"
# than the data would miss it, whatever the clock on this machine says.
_PUBLISHED_OFFSET = timedelta(hours=5, minutes=30)


def _observation_end(text):
    """`ngsild_publish.observation_end` as a datetime, or None for "now".

    Parsed here as well as in the publisher, and deliberately by the same rule:
    the window handed to the collections has to describe the timestamps that
    were actually written, so both sides must read the setting identically. An
    offset is read and discarded, because the publisher stamps a naive local
    clock reading and appends its own suffix.
    """
    if not text:
        return None
    cleaned = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", str(text).strip())
    for shape in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, shape)
        except ValueError:
            continue
    raise AssertionError(
        f"ngsild_publish.observation_end is not a timestamp: {text!r} "
        f"(want 2025-11-01T00:00:00, an offset optional)"
    )


def observation_window(spread_hours, end=None):
    """The range the records just published fall inside, in every spelling the
    collections ask for.

    The publisher spreads observationDateTime backwards from `end` - the moment
    it runs, unless `ngsild_publish.observation_end` moved it - so this is
    derived from the same two numbers rather than parsed back out of its log.
    Both halves must use the same anchor: a window built around "now" would miss
    data deliberately published into an earlier era.

    Three formats, because the collections use three. A `timerel=between` query
    takes `2026-08-24T15:46:14+05:30`; a `temporalQ` body takes the same instant
    as `…Z`; a `beforeTemporal` search criterion writes the offset without its
    colon. A window in the wrong spelling is not a near miss — the server either
    rejects it or reads a different instant.

    **`mid` is the reason there are three moments and not two.** The collection's
    `Latest Data` positive case sends `timeRel=before` *together with* `time` and
    `endTime`, and the two readings of that disagree about what the data must
    look like: if `before` filters on `time` alone the data has to sit *earlier*
    than it, and if the pair is a window the data has to sit *inside* it. No
    single instant satisfies both — but a `time` in the **middle** of the data
    does, because either reading then selects roughly half the rows, and the
    assertion is `result.length === size` rather than a total. That is why
    `count` has to comfortably exceed twice the largest page any folder asks
    for.
    """
    newest = end or datetime.now()
    spread = timedelta(hours=float(spread_hours or 0))
    start = newest - spread - _WINDOW_MARGIN
    end = newest + _WINDOW_MARGIN
    # Halfway through the *data*, not through the padded window: the padding
    # exists so a query over the window is not a boundary test, and splitting on
    # it would put the midpoint off-centre by an hour at each end.
    mid = newest - spread / 2

    def spellings(moment):
        return {
            "": moment.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
            "_compact": moment.strftime("%Y-%m-%dT%H:%M:%S+0530"),
            "_utc": (moment - _PUBLISHED_OFFSET).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    window = {}
    for name, moment in (("start", start), ("mid", mid), ("end", end)):
        for suffix, text in spellings(moment).items():
            window[name + suffix] = text
    return window
