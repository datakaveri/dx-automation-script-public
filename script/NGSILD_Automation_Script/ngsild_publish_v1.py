"""
NGSI-LD publisher - publishes records to a RabbitMQ exchange.

Usage:
    python3 ngsild_publish.py [--config config.ini] [--data records.json]
                              [--count N] [--verbose]

Exit codes (stable - automation can branch on these):
    0  every packet published AND confirmed by the broker
    1  publish failed
    2  configuration error (missing file, missing key, bad value)

Two things worth knowing about this script:

  * Publisher confirms are ON. Without them basic_publish is
    fire-and-forget: the call returns successfully even when the
    broker never accepted the message, so a test would report a
    false pass. With confirms, an unaccepted message raises.

  * Messages are published with mandatory=True. A message that
    routes to no queue is returned rather than silently discarded,
    which is exactly the failure an automated test needs to catch -
    a missing binding otherwise looks identical to a successful run.

  * The exchange is created upstream by the catalogue. This script
    declares it PASSIVELY: existence is asserted, type is not. A
    non-passive declare with the wrong type fails with a 406
    'inequivalent arg' error, which is how this bit them before.
"""

import sys
import ssl
import json
import logging
import argparse
import configparser
from pathlib import Path
from datetime import datetime

import pika


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

DEFAULT_TIMEZONE_SUFFIX = "+05:30"


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger("ngsild_publish")


def configure_logging(verbose):

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(funcName)-24s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # pika is very chatty at INFO level
    logging.getLogger("pika").setLevel(logging.WARNING)


# ============================================================
# CONFIGURATION
# ============================================================

REQUIRED = {
    "rabbitmq": [
        "host", "port", "username", "password",
        "vhost", "cert_path", "check_hostname",
        "exchange_name", "queue_name"
    ]
}


def load_config(path):
    """
    Read and validate the INI. Every required key is checked up
    front so a missing value fails before a connection is opened.
    """

    # interpolation=None so a '%' in a password is not treated as syntax
    parser = configparser.ConfigParser(interpolation=None)

    try:

        if not parser.read(path):
            logger.error(f"Config file not found: {path}")
            sys.exit(EXIT_CONFIG)

    except configparser.Error as error:

        logger.error(f"Invalid INI in {path}: {error}")
        sys.exit(EXIT_CONFIG)

    problems = []

    for section, keys in REQUIRED.items():

        if not parser.has_section(section):
            problems.append(f"missing section [{section}]")
            continue

        for key in keys:

            if not parser.has_option(section, key):
                problems.append(f"missing key {section}.{key}")
            elif not parser.get(section, key).strip():
                problems.append(f"empty value for {section}.{key}")

    # port must be an int - pika fails deep in the socket layer
    # with a confusing error if it is handed a string
    if parser.has_option("rabbitmq", "port"):
        try:
            parser.getint("rabbitmq", "port")
        except ValueError:
            problems.append(
                f"rabbitmq.port is not an integer: "
                f"{parser.get('rabbitmq', 'port')!r}"
            )

    if parser.has_option("rabbitmq", "cert_path"):

        cert = parser.get("rabbitmq", "cert_path").strip()

        if cert and not Path(cert).exists():
            problems.append(f"certificate not found: {cert}")

    # check_hostname must be a boolean - a typo would otherwise
    # raise mid-connection rather than here
    if parser.has_option("rabbitmq", "check_hostname"):
        try:
            parser.getboolean("rabbitmq", "check_hostname")
        except ValueError:
            problems.append(
                f"rabbitmq.check_hostname is not a boolean: "
                f"{parser.get('rabbitmq', 'check_hostname')!r} "
                f"(use true or false)"
            )

    if problems:

        logger.error(f"Configuration problems in {path}:")

        for problem in problems:
            logger.error(f"  - {problem}")

        sys.exit(EXIT_CONFIG)

    return parser


# ============================================================
# SAMPLE DATA
#
# Used when --data is not supplied. Real runs should pass a
# JSON file so the payload is not pinned to this source file.
# ============================================================

SAMPLE_DATA = [
    {
        "cbocode": "CBO001",
        "cboname": "Sample CBO",
        "piuid": 101,
        "piu": "Sample PIU",
        "riuid": 201,
        "riu": "Sample RIU",
        "diuid": 301,
        "diu": "Sample DIU",
        "village": "Sample Village",
        "proposedcost": 100000.00,
        "approvalcost": 90000.00,
        "totaltranche": 3,
        "proposedsubjectname": "Sample Subject",
        "cropname": "Rice",
        "subproject": "Sample Project"
    },
    {
        "cbocode": "CBO002",
        "cboname": "Test CBO",
        "piuid": 102,
        "piu": "Test PIU",
        "riuid": 202,
        "riu": "Test RIU",
        "diuid": 302,
        "diu": "Test DIU",
        "village": "Test Village",
        "proposedcost": 200000.00,
        "approvalcost": 180000.00,
        "totaltranche": 4,
        "proposedsubjectname": "Test Subject",
        "cropname": "Wheat",
        "subproject": "Test Project"
    }
]

FIELDS = [
    "cbocode", "cboname",
    "piuid", "piu",
    "riuid", "riu",
    "diuid", "diu",
    "village",
    "proposedcost", "approvalcost",
    "totaltranche",
    "proposedsubjectname",
    "cropname",
    "subproject"
]


def load_records(path):
    """Load input records from a JSON file (a list of objects)."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)

    except FileNotFoundError:
        logger.error(f"Data file not found: {path}")
        sys.exit(EXIT_CONFIG)

    except json.JSONDecodeError as error:
        logger.error(f"Invalid JSON in {path}: {error}")
        sys.exit(EXIT_CONFIG)

    if not isinstance(data, list):
        logger.error(f"{path} must contain a JSON array of objects")
        sys.exit(EXIT_CONFIG)

    logger.info(f"Loaded {len(data)} record(s) from {path}")

    return data


# ============================================================
# TRANSFORM
# ============================================================

def transform(records, resource_id):
    """Build NGSI-LD packets, one per input record."""

    logger.info(f"Transform started for {len(records)} record(s)")

    observed_at = datetime.now().strftime(
        f"%Y-%m-%dT%H:%M:%S{DEFAULT_TIMEZONE_SUFFIX}"
    )

    logger.info(f"observationDateTime: {observed_at}")

    packets = []
    skipped = 0

    for index, record in enumerate(records, start=1):

        if not isinstance(record, dict):

            logger.error(f"Record {index} is not an object, skipping")
            skipped += 1
            continue

        packet = {"id": resource_id}

        for field in FIELDS:
            packet[field] = record.get(field)

        packet["observationDateTime"] = observed_at

        # Flag missing fields so bad upstream data is visible
        missing = [key for key, value in packet.items() if value is None]

        if missing:
            logger.warning(
                f"Record {index} (cbocode={record.get('cbocode')}) "
                f"has null fields: {missing}"
            )

        packets.append(packet)

        logger.debug(
            f"Record {index}: cbocode={packet['cbocode']}, "
            f"village={packet['village']}"
        )

    logger.info(
        f"Transform complete: {len(packets)} packet(s), {skipped} skipped"
    )

    return packets


# ============================================================
# TLS
# ============================================================

def build_ssl_context(cert_path, check_hostname):

    logger.info(f"Building TLS context from CA file: {cert_path}")

    context = ssl.create_default_context(cafile=cert_path)

    # check_hostname=false accepts a certificate whose subject does not
    # match the broker hostname (this broker's is issued for localhost);
    # certificate chain verification stays on either way.
    context.check_hostname = check_hostname
    context.verify_mode = ssl.CERT_REQUIRED

    if not check_hostname:
        logger.warning(
            "check_hostname is disabled - the broker certificate's "
            "subject is not verified against its hostname"
        )

    logger.info(
        f"TLS ready (check_hostname={check_hostname}, "
        f"verify_mode=CERT_REQUIRED)"
    )

    return context


# ============================================================
# PUBLISH
# ============================================================

def publish(packets, settings):
    """
    Publish every packet. Returns True only if the broker confirmed
    all of them.
    """

    connection = None

    host = settings["host"]
    port = settings["port"]
    vhost = settings["vhost"]
    username = settings["username"]
    exchange = settings["exchange"]
    queue = settings["queue"]
    routing_key = settings["routing_key"]

    returned = []

    logger.info(f"Publishing {len(packets)} packet(s)")

    try:

        logger.info(f"Connecting to {host}:{port}/{vhost}")

        context = build_ssl_context(
            settings["cert_path"],
            settings["check_hostname"]
        )

        parameters = pika.ConnectionParameters(
            host=host,
            port=port,
            virtual_host=vhost,
            credentials=pika.PlainCredentials(username, settings["password"]),
            ssl_options=pika.SSLOptions(context, host),
            heartbeat=60,
            blocked_connection_timeout=300,
            connection_attempts=3,
            retry_delay=5
        )

        connection = pika.BlockingConnection(parameters)

        logger.info("Connection established")

        channel = connection.channel()

        logger.info(f"Channel opened (number={channel.channel_number})")

        # Publisher confirms: without this, basic_publish returns
        # successfully even when the broker never accepted the message.
        channel.confirm_delivery()

        logger.info("Publisher confirms enabled")

        # A message that routes nowhere comes back here instead of
        # being silently dropped - the signal that a binding is missing.
        channel.add_on_return_callback(
            lambda ch, method, props, body: returned.append(method)
        )

        # --- verify exchange, bind queue --------------------
        # PASSIVE declare: asserts existence without asserting type,
        # so this never conflicts with the catalogue's definition.

        logger.info(f"Verifying exchange '{exchange}' (passive declare)")

        channel.exchange_declare(exchange=exchange, passive=True)

        logger.info(f"Exchange '{exchange}' verified")

        logger.info(
            f"Binding queue '{queue}' to '{exchange}' "
            f"with routing key '{routing_key}'"
        )

        channel.queue_bind(
            exchange=exchange,
            queue=queue,
            routing_key=routing_key
        )

        logger.info("Queue binding confirmed")

        # --- publish ----------------------------------------

        published = 0
        total_bytes = 0

        properties = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2
        )

        for index, packet in enumerate(packets, start=1):

            message = json.dumps(packet, ensure_ascii=False)
            encoded = message.encode("utf-8")

            try:

                channel.basic_publish(
                    exchange=exchange,
                    routing_key=routing_key,
                    body=message,
                    properties=properties,
                    mandatory=True
                )

            except pika.exceptions.UnroutableError:

                logger.error(
                    f"Packet {index} was returned as unroutable - no queue "
                    f"is bound to '{exchange}' for routing key "
                    f"'{routing_key}'"
                )

                return False

            except pika.exceptions.NackError:

                logger.error(
                    f"Packet {index} was rejected (nacked) by the broker"
                )

                return False

            published += 1
            total_bytes += len(encoded)

            logger.info(
                f"Published and confirmed {index}/{len(packets)} "
                f"({len(encoded)} bytes)"
            )

        if returned:

            logger.error(
                f"{len(returned)} message(s) were returned as unroutable"
            )

            return False

        logger.info(
            f"All packets confirmed: {published} message(s), "
            f"{total_bytes} bytes"
        )

        return True

    except ssl.SSLCertVerificationError as error:

        logger.error(
            f"TLS certificate verification failed for {host} using CA "
            f"{settings['cert_path']}: {error}"
        )

        return False

    except ssl.SSLError as error:

        logger.error(f"TLS error: {error}")

        return False

    except pika.exceptions.ProbableAuthenticationError as error:

        logger.error(
            f"Authentication rejected for '{username}' on vhost "
            f"'{vhost}': {error}"
        )

        return False

    except pika.exceptions.ProbableAccessDeniedError as error:

        logger.error(
            f"Access denied to vhost '{vhost}' for '{username}': {error}"
        )

        return False

    except pika.exceptions.AMQPConnectionError as error:

        logger.error(
            f"Connection error to {host}:{port} - "
            f"{type(error).__name__}: {error}"
        )

        return False

    # ChannelClosedByBroker must precede AMQPChannelError - it is a
    # subclass, and Python takes the first matching handler.
    except pika.exceptions.ChannelClosedByBroker as error:

        if error.reply_code == 404:

            logger.error(
                f"Exchange '{exchange}' or queue '{queue}' does not exist "
                f"on vhost '{vhost}' - upstream has not created it yet: "
                f"{error.reply_text}"
            )

        elif error.reply_code == 403:

            logger.error(
                f"'{username}' lacks permission on vhost '{vhost}': "
                f"{error.reply_text}"
            )

        elif error.reply_code == 406:

            logger.error(
                f"Exchange '{exchange}' exists with different properties "
                f"than requested: {error.reply_text}"
            )

        else:

            logger.error(
                f"Broker closed channel: {error.reply_code} "
                f"{error.reply_text}"
            )

        return False

    except pika.exceptions.AMQPChannelError as error:

        logger.error(
            f"Channel error (exchange={exchange}, queue={queue}) - "
            f"{type(error).__name__}: {error}"
        )

        return False

    except Exception as error:

        logger.exception(
            f"Unexpected error during publish - "
            f"{type(error).__name__}: {error}"
        )

        return False

    finally:

        if connection is not None and connection.is_open:

            try:
                connection.close()
                logger.info("Connection closed cleanly")

            except Exception as error:
                logger.warning(
                    f"Error closing connection (ignored): "
                    f"{type(error).__name__}: {error}"
                )


# ============================================================
# MAIN
# ============================================================

def main():

    args = parse_args()

    configure_logging(args.verbose)

    config = load_config(args.config)

    rmq = config["rabbitmq"]

    exchange = rmq["exchange_name"]

    settings = {
        "host": rmq["host"],
        "port": rmq.getint("port"),
        "username": rmq["username"],
        "password": rmq["password"],
        "vhost": rmq["vhost"],
        "cert_path": rmq["cert_path"],
        "check_hostname": rmq.getboolean("check_hostname"),
        "exchange": exchange,
        "queue": rmq["queue_name"],
        # Routing key is the exchange name: a dotless UUID, which
        # matches exactly on a topic exchange.
        "routing_key": exchange
    }

    started = datetime.now()

    logger.info("=" * 60)
    logger.info("NGSI-LD publisher run started")
    logger.info("=" * 60)

    logger.info(f"config file  : {args.config}")
    logger.info(
        f"broker       : {settings['host']}:{settings['port']}"
        f"/{settings['vhost']} as {settings['username']}"
    )
    logger.info(
        f"exchange     : {exchange} | queue: {settings['queue']} | "
        f"routing key: {settings['routing_key']}"
    )

    records = load_records(args.data) if args.data else SAMPLE_DATA

    if not args.data:
        logger.info(f"Using built-in sample data ({len(records)} records)")

    if args.count is not None:

        if args.count < 1:
            logger.error("--count must be at least 1")
            return EXIT_CONFIG

        # Cycle the source records up to the requested count
        records = [records[i % len(records)] for i in range(args.count)]

        logger.info(f"Expanded to {len(records)} record(s) via --count")

    packets = transform(records, exchange)

    if not packets:
        logger.error("No packets to publish after transform")
        return EXIT_FAILED

    success = publish(packets, settings)

    duration = (datetime.now() - started).total_seconds()

    logger.info("=" * 60)

    if not success:
        logger.error(f"Run FAILED after {duration:.2f}s")
        return EXIT_FAILED

    logger.info(
        f"Run SUCCEEDED: {len(packets)} packet(s) published and "
        f"confirmed in {duration:.2f}s"
    )

    return EXIT_OK


def parse_args():

    parser = argparse.ArgumentParser(
        description="Publish NGSI-LD records to a RabbitMQ exchange."
    )

    parser.add_argument(
        "--config",
        default="config.ini",
        help="path to the INI config file (default: config.ini)"
    )

    parser.add_argument(
        "--data",
        help="path to a JSON file containing an array of records "
             "(default: built-in sample data)"
    )

    parser.add_argument(
        "--count",
        type=int,
        help="publish this many packets, cycling through the source "
             "records (useful for load checks)"
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable debug logging"
    )

    return parser.parse_args()


if __name__ == "__main__":

    try:
        sys.exit(main())

    except KeyboardInterrupt:
        logger.error("Interrupted")
        sys.exit(EXIT_FAILED)
