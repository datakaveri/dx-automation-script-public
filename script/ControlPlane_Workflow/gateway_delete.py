#!/usr/bin/env python3
"""
Remove the broker objects a gateway item leaves behind.

A gateway item is served over RPC: the catalogue creates a queue named with the
item id, and a broker user for the provider. Deleting the catalogue item removes
neither, and once the item is gone nothing on the platform names them.

script/GATEWAY_Automation_Script/delete_rmq.py does the removal — the queue over
AMQP (a passive declare first, because queue_delete is idempotent and cannot
tell an existing queue from a missing one), then the user over the management
API. The harness runs that script rather than reimplementing it.

Unlike the NGSI-LD scripts it takes no --config: it reads `./config.ini` from
its working directory, so `script_runner` is asked to run it that way.
"""

from . import script_runner
from .config import resolve_path

# The script exits with a message on any failure, so anything non-zero is one
# bucket — it has no separate configuration-error code.
EXIT_MEANING = {
    script_runner.EXIT_OK: "queue and broker user removed",
    script_runner.EXIT_FAILED: "queue or broker user could not be removed",
}


def settings(config):
    """Settings for the teardown script, with the fallbacks applied.

    The adaptor already describes this broker and vhost — it consumes from the
    very queue being deleted — so those values are reused rather than restated.
    The management URL falls back to ngsild_delete's, which is the same API.
    """
    delete = config["gateway_delete"]
    adaptor = config["gateway_adaptor"]
    publish = config["ngsild_publish"]
    databroker = config["databroker"]

    def either(key):
        return (
            delete.get(key)
            or adaptor.get(key)
            or publish.get(key)
            or databroker.get(key)
        )

    return {
        "host": either("host"),
        "port": int(delete.get("port") or adaptor["port"]),
        "username": either("username"),
        "password": either("password"),
        "vhost": delete.get("vhost") or adaptor["vhost"],
        "cert_path": str(resolve_path(delete.get("cert_path") or adaptor["cert_path"])),
        "check_hostname": bool(
            adaptor["check_hostname"] if delete.get("check_hostname") is None
            else delete["check_hostname"]
        ),
        "mgmt_url": delete.get("mgmt_url") or config["ngsild_delete"]["mgmt_url"],
        "mgmt_verify": delete.get("mgmt_verify") or config["ngsild_delete"]["mgmt_verify"],
    }


def gateway_delete(ctx, queue, broker_user):
    """Delete `queue` and `broker_user`.

    Returns a ScriptResult; nothing is raised, because this runs during teardown
    where one failure must not strand the artefacts behind it.
    """
    delete = ctx.config["gateway_delete"]
    values = settings(ctx.config)
    target = f"amqps://{values['host']}:{values['port']}/{values['vhost']} → {queue}"

    return script_runner.run(
        ctx,
        resolve_path(delete["script"]),
        EXIT_MEANING,
        # No --config: the script reads ./config.ini from its working directory.
        files={"config.ini": script_runner.ini_text({
            "server_setup": {
                "username": values["username"],
                "password": values["password"],
                "host": values["host"],
                "port": values["port"],
                "vhost": values["vhost"],
                # Absolute: the script resolves this against its cwd, which is
                # the temporary directory holding the generated config.
                "cert_path": values["cert_path"],
                "check_hostname": values["check_hostname"],
                "mgmt_url": values["mgmt_url"],
                "mgmt_verify": values["mgmt_verify"],
            },
            "queue": {"name": queue},
            # The provider's broker user, named with their Keycloak id.
            "delete": {"user": broker_user},
        })},
        cwd_in_temp=True,
        label=f"gateway teardown {queue}",
        target=target,
        verbose=bool(delete["verbose"]),
        request={"queue": queue, "broker_user": broker_user},
    )
