import ssl
import sys
import json
import logging
import pika
import requests
from pathlib import Path
from configparser import ConfigParser

# -------------------------------------------------
# Config & Logging
# -------------------------------------------------
CONFIG_FILE = "./config.ini"

config = ConfigParser(interpolation=None)

if not config.read(CONFIG_FILE):
    # config.read() fails silently on a missing file - every later
    # lookup would raise a confusing KeyError instead.
    sys.exit(f"ERROR: config not found: {CONFIG_FILE}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

# -------------------------------------------------
# RabbitMQ Config Holder
# -------------------------------------------------
class RabbitMqServerConfigure:
    def __init__(self, username, password, host, port, vhost,
                 queue, cert_path, check_hostname):
        self.username = username
        self.password = password
        self.host = host
        self.port = int(port)
        self.vhost = vhost
        self.queue = queue
        self.cert_path = cert_path
        self.check_hostname = check_hostname

# -------------------------------------------------
# RabbitMQ Server
#
# Topology (queue, exchange, binding) is owned entirely by
# upstream. This script only consumes and publishes.
# -------------------------------------------------
class rabbitmqServer:

    def __init__(self, server):
        self.server = server

        # Credentials are passed as objects, NOT embedded in a URL.
        # This keeps special characters (#, @, /, :, %) in the password
        # intact - a URL would treat '#' as the start of a fragment and
        # silently truncate everything after it.
        credentials = pika.PlainCredentials(server.username, server.password)

        ssl_options = pika.SSLOptions(
            self.create_ssl_context(),
            server.host
        )

        parameters = pika.ConnectionParameters(
            host=server.host,
            port=server.port,
            virtual_host=server.vhost,
            credentials=credentials,
            ssl_options=ssl_options,
            heartbeat=60,
            blocked_connection_timeout=300,
            connection_attempts=3,
            retry_delay=5
        )

        self.connection = pika.BlockingConnection(parameters)
        self.channel = self.connection.channel()
        logging.info("......RabbitMQ Server started......")

    # -------------------------------------------------
    # TLS context
    # -------------------------------------------------
    def create_ssl_context(self):
        # A missing cert file otherwise surfaces as an obscure SSL
        # error rather than "file not found". cert_path is relative
        # to the working directory, which is exactly the case that bites.
        if not Path(self.server.cert_path).exists():
            sys.exit(
                f"ERROR: certificate not found: {self.server.cert_path}"
            )

        ssl_context = ssl.create_default_context(
            cafile=self.server.cert_path
        )

        # Hostname checking is configurable: this broker's cert is
        # issued for localhost, but another environment's may not be.
        ssl_context.check_hostname = self.server.check_hostname
        ssl_context.verify_mode = ssl.CERT_REQUIRED

        return ssl_context

    def startserver(self, on_request):
        self.channel.basic_qos(prefetch_count=1)
        self.channel.basic_consume(
            queue=self.server.queue,
            on_message_callback=on_request
        )
        logging.info(f"......Consuming from '{self.server.queue}'......")

        try:
            self.channel.start_consuming()
        except KeyboardInterrupt:
            logging.info("......Shutting down......")
            self.channel.stop_consuming()
            self.connection.close()

    def publish(self, payload, routing_key, corr_id, method):
        self.channel.basic_publish(
            exchange="",
            routing_key=routing_key,
            properties=pika.BasicProperties(correlation_id=corr_id),
            body=json.dumps(payload)
        )
        logging.info(f"......Response published to '{routing_key}'......")

    def ack(self, method):
        self.channel.basic_ack(delivery_tag=method.delivery_tag)

# -------------------------------------------------
# Dummy API Worker (test harness)
# Incoming message is a TRIGGER ONLY - body is not parsed or used.
#
# RESPONSE CONTRACT: every reply - success or failure - is
# {"results": [ ... ]}. Upstream only reads the "results" key,
# so errors must live inside that array too or they are invisible.
# -------------------------------------------------
class DummyUsersLookup:

    def __init__(self, api_url, results_key):
        self.api_url = api_url
        self.results_key = results_key

    # -------------------------------------------------
    # Single place that builds the error shape, so the
    # contract cannot drift between call sites
    # -------------------------------------------------
    @staticmethod
    def error_payload(status_code, details):
        return {
            "results": [
                {
                    "statusCode": status_code,
                    "details": str(details)
                }
            ]
        }

    def process_request(self, ch, method, properties, body):
        logging.info("......Request received......")

        reply_to = properties.reply_to
        corr_id = properties.correlation_id

        # Without reply_to there is nowhere to send the response.
        # basic_publish(routing_key=None) would raise, the message
        # would never be acked, and it would redeliver forever.
        # Ack and drop instead - this happens when a message is
        # published by hand from the management UI.
        if not reply_to:
            logging.error(
                "......No reply_to on message - dropping......"
            )
            server.ack(method)
            return

        try:
            payload = self.getData()
        except Exception as e:
            logging.error(f"......Unhandled error: {e}......")
            payload = self.error_payload(400, e)

        # Ack happens whatever the payload turned out to be, so a
        # failing API can never wedge the queue with redeliveries.
        try:
            server.publish(payload, reply_to, corr_id, method)
        except Exception as e:
            logging.error(f"......Publish failed: {e}......")
        finally:
            server.ack(method)

    # -------------------------------------------------
    # Call API and build the results array
    # -------------------------------------------------
    def getData(self):

        try:
            api_response = requests.get(self.api_url, timeout=30)
        except requests.exceptions.RequestException as e:
            logging.error(f"......API request failed: {e}......")
            return self.error_payload(400, e)

        if api_response.status_code != 200:
            logging.error(
                f"......API returned {api_response.status_code}......"
            )
            return self.error_payload(
                api_response.status_code,
                "API call failed"
            )

        try:
            api_json = api_response.json()
        except ValueError as e:
            logging.error(f"......API returned non-JSON: {e}......")
            return self.error_payload(400, "API returned non-JSON body")

        # Normalise to a list, whatever shape came back:
        #   {"users": [...]}  -> the inner list
        #   [ ... ]           -> as-is
        #   { ... }           -> wrapped in a list
        if isinstance(api_json, dict):
            records = api_json.get(self.results_key)
            if records is None:
                records = [api_json]
        elif isinstance(api_json, list):
            records = api_json
        else:
            records = [api_json]

        if not isinstance(records, list):
            records = [records]

        logging.info(f"......Fetched {len(records)} records......")

        return {"results": records}

# -------------------------------------------------
# Main
# -------------------------------------------------
if __name__ == "__main__":

    srv = config["server_setup"]

    server_config = RabbitMqServerConfigure(
        username=srv["username"],
        password=srv["password"],
        host=srv["host"],
        port=srv["port"],
        vhost=srv["vhost"],
        queue=config["queue"]["name"],
        cert_path=srv["cert_path"],
        check_hostname=srv.getboolean("check_hostname", fallback=True)
    )

    worker = DummyUsersLookup(
        api_url=config["api"]["url"],
        results_key=config["api"]["results_key"]
    )

    server = rabbitmqServer(server_config)
    server.startserver(worker.process_request)
