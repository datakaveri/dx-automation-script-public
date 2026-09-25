import ssl
import sys
import pika
import requests
from pathlib import Path
from configparser import ConfigParser

# -------------------------------------------------
# Config
# -------------------------------------------------
CONFIG_FILE = "./config.ini"

config = ConfigParser(interpolation=None)

if not config.read(CONFIG_FILE):
    # config.read() fails silently on a missing file - every later
    # lookup would raise a confusing KeyError instead.
    sys.exit(f"ERROR: config not found: {CONFIG_FILE}")

rmq = config["server_setup"]

QUEUE_NAME = config["queue"]["name"]
DELETE_USER = config["delete"]["user"]

if not Path(rmq["cert_path"]).exists():
    sys.exit(f"ERROR: certificate not found: {rmq['cert_path']}")


# -------------------------------------------------
# Connect (AMQP)
# -------------------------------------------------
context = ssl.create_default_context(cafile=rmq["cert_path"])
context.check_hostname = rmq.getboolean("check_hostname", fallback=True)

connection = pika.BlockingConnection(
    pika.ConnectionParameters(
        host=rmq["host"],
        port=rmq.getint("port"),
        virtual_host=rmq["vhost"],
        credentials=pika.PlainCredentials(rmq["username"], rmq["password"]),
        ssl_options=pika.SSLOptions(context, rmq["host"])
    )
)

channel = connection.channel()


# -------------------------------------------------
# 1. Delete queue
#
# queue_delete is idempotent - it reports success whether or
# not the queue existed, so it cannot tell us which happened.
# A passive declare is what actually answers that: it checks
# existence only, and 404s if the queue is not there.
# -------------------------------------------------
try:
    channel.queue_declare(queue=QUEUE_NAME, passive=True)
    queue_exists = True

except pika.exceptions.ChannelClosedByBroker as error:

    if error.reply_code == 404:
        queue_exists = False

    elif error.reply_code == 403:
        connection.close()
        sys.exit(
            f"ERROR: '{rmq['username']}' lacks configure permission "
            f"on {rmq['vhost']} - cannot delete queues."
        )

    else:
        connection.close()
        sys.exit(f"ERROR: {error.reply_code} {error.reply_text}")

    # Any channel error closes the channel - reopen it to continue
    channel = connection.channel()

if queue_exists:
    result = channel.queue_delete(queue=QUEUE_NAME)
    print(f"Queue deleted: {QUEUE_NAME} "
          f"({result.method.message_count} messages)")
else:
    print(f"Queue does not exist, nothing to delete: {QUEUE_NAME}")

if connection.is_open:
    connection.close()


# -------------------------------------------------
# 2. Delete user (management API)
#
# Users are broker-level and not exposed over AMQP, so this
# goes over HTTP. Requires the 'administrator' tag on the
# account used here.
# -------------------------------------------------
response = requests.delete(
    f"{rmq['mgmt_url'].rstrip('/')}/api/users/{DELETE_USER}",
    auth=(rmq["username"], rmq["password"]),
    verify=rmq.getboolean("mgmt_verify", fallback=True),
    timeout=30
)

if response.status_code in (200, 204):
    print(f"User deleted: {DELETE_USER}")

elif response.status_code == 404:
    print(f"User does not exist, nothing to delete: {DELETE_USER}")

elif response.status_code in (401, 403):
    sys.exit(
        f"ERROR: '{rmq['username']}' is not authorised to delete users "
        f"- the 'administrator' tag is required ({response.status_code})."
    )

else:
    sys.exit(
        f"ERROR: user delete failed: "
        f"{response.status_code} {response.text}"
    )

print("\nDone.")
