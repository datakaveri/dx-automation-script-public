"""
NGSI-LD teardown - removes one exchange's resources.

Order:
  1. Elasticsearch index  (documents go with it)
  2. RabbitMQ exchange
  3. Catalogue-created RabbitMQ user

Usage:
    python3 ngsild_delete.py [--config config.ini] [--dry-run] [--verbose]

Exit codes (stable - automation can branch on these):
    0  every step succeeded, or the resources were already gone
    1  at least one step failed
    2  configuration error (missing file, missing key, bad value)

Environment notes, established by es_kibana_diagnostic.py:

  * The Kibana console proxy returns HTTP 200 for EVERY request.
    Elasticsearch's real status is in the response body - a top-level
    "status" on JSON errors, or plain text like "404 - Not Found" for
    HEAD. Never read response.status_code for an ES call; an earlier
    version did and logged 403s as successful deletions.

  * HEAD <index> is unusable through the proxy: 200 for both existing
    and missing indices. Existence is checked with _count instead.

  * Index names use a DOUBLE underscore: iudx-v2__<exchange_name>.
"""

import sys
import logging
import argparse
import configparser
from urllib.parse import quote

import requests


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2

DEFAULT_INDEX_PREFIX = "iudx-v2__"

HTTP_TIMEOUT = 30
ES_TIMEOUT = 300


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger("ngsild_delete")


def configure_logging(verbose):

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(funcName)-30s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # urllib3 logs every connection at DEBUG
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ============================================================
# CONFIGURATION
# ============================================================

REQUIRED = {
    "kibana": ["host", "username", "password"],
    "rabbitmq": [
        "username", "password", "vhost",
        "exchange_name", "mgmt_url", "delete_user"
    ]
}


def load_config(path):
    """
    Read and validate the INI. Every required key is checked up
    front so a missing value fails before anything is deleted,
    not halfway through a teardown.
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

    if problems:

        logger.error(f"Configuration problems in {path}:")

        for problem in problems:
            logger.error(f"  - {problem}")

        sys.exit(EXIT_CONFIG)

    return parser


# ============================================================
# KIBANA -> ELASTICSEARCH
# ============================================================

class Elasticsearch:
    """Elasticsearch access through the Kibana console proxy."""

    def __init__(self, host, username, password, index):

        self.host = host.rstrip("/")
        self.auth = (username, password)
        self.username = username
        self.index = index

    def request(self, method, path, body=None):
        """
        Returns (es_status, parsed_body, raw_text).

        es_status is Elasticsearch's REAL status, recovered from the
        body - not the proxy's HTTP status, which is always 200.
        Returns (None, None, None) if Kibana is unreachable.
        """

        url = (
            f"{self.host}/api/console/proxy"
            f"?path={quote(path, safe='')}"
            f"&method={method}"
        )

        headers = {
            "kbn-xsrf": "true",
            "Content-Type": "application/json"
        }

        logger.debug(f"proxy request: {method} {path}")

        try:

            response = requests.post(
                url,
                headers=headers,
                auth=self.auth,
                json=body,
                timeout=ES_TIMEOUT
            )

        except requests.RequestException as error:

            logger.error(
                f"Kibana unreachable: {type(error).__name__}: {error}"
            )

            return None, None, None

        raw = response.text or ""

        if response.status_code == 401:

            logger.error(f"Kibana rejected credentials for '{self.username}'")

            return 401, None, raw

        try:
            parsed = response.json() if raw else None
        except ValueError:
            parsed = None

        status = self._real_status(response.status_code, parsed, raw)

        logger.debug(f"proxy 200, elasticsearch {status}")

        return status, parsed, raw

    @staticmethod
    def _real_status(http_status, parsed, raw):
        """
        Recover Elasticsearch's status from the response body.

        JSON errors carry a top-level "status"; HEAD replies arrive
        as plain text beginning with the code.
        """

        text = (raw or "").strip()

        if text[:3].isdigit() and not text.startswith("{"):
            return int(text[:3])

        if (
            isinstance(parsed, dict)
            and "status" in parsed
            and "error" in parsed
        ):
            try:
                return int(parsed["status"])
            except (TypeError, ValueError):
                pass

        return http_status

    @staticmethod
    def reason(parsed):
        """Human-readable reason from an ES error body, or None."""

        if not isinstance(parsed, dict):
            return None

        error = parsed.get("error")

        if isinstance(error, str):
            return error

        if isinstance(error, dict):

            causes = error.get("root_cause")

            if isinstance(causes, list) and causes:
                return causes[0].get("reason", error.get("reason"))

            return error.get("reason")

        return None

    def exists(self):
        """
        True (exists), False (missing), or None (unknown).

        Uses _count: HEAD is unusable through this proxy, and _cat
        needs indices:monitor/stats. _count needs only read.
        """

        status, parsed, raw = self.request("GET", f"{self.index}/_count")

        if status is None:
            return None

        if isinstance(parsed, dict) and "count" in parsed:

            logger.info(
                f"Index exists with {parsed['count']} documents: {self.index}"
            )

            return True

        if status == 404:

            logger.info(f"Index does not exist: {self.index}")

            return False

        logger.error(
            f"Cannot determine index status - ES returned {status}: "
            f"{self.reason(parsed) or (raw or '')[:300]}"
        )

        if status == 403:
            logger.error(
                f"'{self.username}' needs the 'read' privilege on "
                f"{self.index} for this check"
            )

        return None

    def delete(self):
        """Delete the index. Documents go with it."""

        status, parsed, raw = self.request("DELETE", self.index)

        if status is None:
            return False

        if status == 404:

            logger.info(f"Index does not exist: {self.index}")

            return True

        if status in (401, 403):

            logger.error(
                f"Not authorised to delete index as '{self.username}': "
                f"{self.reason(parsed)}"
            )

            return False

        if status == 200 and isinstance(parsed, dict):

            if parsed.get("acknowledged") is True:

                logger.info(f"Index deleted: {self.index}")

                return True

            logger.error(
                f"Index deletion not acknowledged: {(raw or '')[:300]}"
            )

            return False

        logger.error(
            f"Index deletion failed - ES returned {status}: "
            f"{self.reason(parsed) or (raw or '')[:300]}"
        )

        return False


# ============================================================
# RABBITMQ MANAGEMENT API
# ============================================================

class RabbitMQ:
    """RabbitMQ broker objects over the HTTP Management API."""

    def __init__(self, mgmt_url, username, password, vhost, verify=True):

        self.mgmt_url = mgmt_url.rstrip("/")
        self.auth = (username, password)
        self.username = username
        self.vhost = vhost
        self.verify = verify

    def _call(self, method, path):

        url = f"{self.mgmt_url}{path}"

        logger.debug(f"management api: {method} {url}")

        try:

            return requests.request(
                method,
                url,
                auth=self.auth,
                verify=self.verify,
                timeout=HTTP_TIMEOUT
            )

        except requests.RequestException as error:

            logger.error(
                f"Management API error: {type(error).__name__}: {error}"
            )

            return None

    def whoami(self):
        """
        Report the tags on this credential. Deleting exchanges and
        users both need 'administrator', so a missing tag is worth
        surfacing before the 403 arrives.
        """

        response = self._call("GET", "/api/whoami")

        if response is None:
            return False

        if response.status_code == 401:

            logger.error(
                f"Management API rejected credentials for '{self.username}'"
            )

            return False

        if response.status_code != 200:

            logger.warning(
                f"Could not read /api/whoami: {response.status_code} "
                f"- continuing"
            )

            return True

        try:
            payload = response.json()
        except ValueError:
            logger.warning("/api/whoami did not return JSON - continuing")
            return True

        tags = payload.get("tags", [])

        if isinstance(tags, str):
            tags = [tag.strip() for tag in tags.split(",") if tag.strip()]

        logger.info(f"Authenticated as '{payload.get('name')}' tags={tags}")

        if "administrator" not in tags:

            logger.warning(
                f"'{self.username}' lacks the 'administrator' tag - "
                f"exchange and user deletion will likely be refused"
            )

        return True

    def _exchange_path(self, name):

        return (
            f"/api/exchanges/{quote(self.vhost, safe='')}/"
            f"{quote(name, safe='')}"
        )

    def _user_path(self, name):

        return f"/api/users/{quote(name, safe='')}"

    def exchange_exists(self, name):

        response = self._call("GET", self._exchange_path(name))

        if response is None:
            return None

        if response.status_code == 200:

            try:
                payload = response.json()
                detail = (
                    f" (type={payload.get('type')}, "
                    f"durable={payload.get('durable')})"
                )
            except ValueError:
                detail = ""

            logger.info(f"Exchange exists: {name}{detail}")

            return True

        if response.status_code == 404:

            logger.info(f"Exchange does not exist: {name}")

            return False

        logger.error(
            f"Cannot check exchange {name}: {response.status_code}"
        )

        logger.error(response.text[:500])

        return None

    def delete_exchange(self, name):

        response = self._call("DELETE", self._exchange_path(name))

        if response is None:
            return False

        if response.status_code in (200, 204):

            logger.info(f"Exchange deleted: {name}")

            return True

        if response.status_code == 404:

            logger.info(f"Exchange does not exist: {name}")

            return True

        if response.status_code in (401, 403):

            logger.error(
                f"Exchange deletion unauthorized for '{self.username}': "
                f"{response.status_code} - the 'administrator' tag is "
                f"required"
            )

            logger.error(response.text[:500])

            return False

        logger.error(
            f"Exchange deletion failed for {name}: {response.status_code}"
        )

        logger.error(response.text[:500])

        return False

    def user_exists(self, name):

        response = self._call("GET", self._user_path(name))

        if response is None:
            return None

        if response.status_code == 200:

            logger.info(f"User exists: {name}")

            return True

        if response.status_code == 404:

            logger.info(f"User does not exist: {name}")

            return False

        logger.error(f"Cannot check user {name}: {response.status_code}")

        logger.error(response.text[:500])

        return None

    def delete_user(self, name):

        response = self._call("DELETE", self._user_path(name))

        if response is None:
            return False

        if response.status_code in (200, 204):

            logger.info(f"User deleted: {name}")

            return True

        if response.status_code == 404:

            logger.info(f"User does not exist: {name}")

            return True

        if response.status_code in (401, 403):

            logger.error(
                f"User deletion unauthorized for '{self.username}': "
                f"{response.status_code} - the 'administrator' tag is "
                f"required"
            )

            logger.error(response.text[:500])

            return False

        logger.error(
            f"User deletion failed for {name}: {response.status_code}"
        )

        logger.error(response.text[:500])

        return False


# ============================================================
# MAIN
# ============================================================

def main():

    args = parse_args()

    configure_logging(args.verbose)

    config = load_config(args.config)

    kibana = config["kibana"]
    rmq = config["rabbitmq"]

    exchange_name = rmq["exchange_name"]
    delete_user_name = rmq["delete_user"]

    index_prefix = rmq.get("index_prefix", fallback=DEFAULT_INDEX_PREFIX)
    es_index = index_prefix + exchange_name

    # verify accepts a bool or a CA path; getboolean would reject a path
    mgmt_verify = rmq.get("mgmt_verify", fallback="true").strip()

    if mgmt_verify.lower() in ("true", "false"):
        mgmt_verify = mgmt_verify.lower() == "true"

    logger.info("=" * 60)
    logger.info(f"NGSI-LD teardown: {exchange_name}")
    logger.info("=" * 60)

    logger.info(f"config file         : {args.config}")
    logger.info(f"elasticsearch index : {es_index}")
    logger.info(f"elasticsearch user  : {kibana['username']}")
    logger.info(f"rabbitmq vhost      : {rmq['vhost']}")
    logger.info(f"management api user : {rmq['username']}")
    logger.info(f"user to delete      : {delete_user_name}")

    if args.dry_run:
        logger.warning("DRY RUN - nothing will be deleted")

    # --- guard ----------------------------------------------
    # Never delete the credential this script authenticates with

    if rmq["username"] == delete_user_name:

        logger.error(
            "delete_user is the same as the RabbitMQ user this script "
            "authenticates with. Refusing to run."
        )

        return EXIT_CONFIG

    es = Elasticsearch(
        kibana["host"],
        kibana["username"],
        kibana["password"],
        es_index
    )

    broker = RabbitMQ(
        rmq["mgmt_url"],
        rmq["username"],
        rmq["password"],
        rmq["vhost"],
        verify=mgmt_verify
    )

    # Failures are collected rather than exiting on the first one, so
    # a single run shows the state of all three objects.
    failures = []

    if not broker.whoami():
        return EXIT_CONFIG

    # === STEP 1 - Elasticsearch index ========================
    # Deleting the index removes its documents; no separate
    # _delete_by_query pass is needed.

    logger.info("-" * 60)
    logger.info("STEP 1 - Elasticsearch index")

    index_exists = es.exists()

    if index_exists is None:
        failures.append("elasticsearch index status check")

    elif index_exists:

        if args.dry_run:
            logger.info(f"DRY RUN - would delete index {es_index}")
        elif es.delete():
            if es.exists() is not False:
                failures.append("elasticsearch index deletion not verified")
        else:
            failures.append("elasticsearch index deletion")

    # === STEP 2 - RabbitMQ exchange ==========================

    logger.info("-" * 60)
    logger.info("STEP 2 - RabbitMQ exchange")

    exchange_exists = broker.exchange_exists(exchange_name)

    if exchange_exists is None:
        failures.append("rabbitmq exchange status check")

    elif exchange_exists:

        if args.dry_run:
            logger.info(f"DRY RUN - would delete exchange {exchange_name}")
        elif broker.delete_exchange(exchange_name):
            if broker.exchange_exists(exchange_name) is not False:
                failures.append("rabbitmq exchange deletion not verified")
        else:
            failures.append("rabbitmq exchange deletion")

    # === STEP 3 - RabbitMQ user ==============================

    logger.info("-" * 60)
    logger.info("STEP 3 - RabbitMQ user")

    user_exists = broker.user_exists(delete_user_name)

    if user_exists is None:
        failures.append("rabbitmq user status check")

    elif user_exists:

        if args.dry_run:
            logger.info(f"DRY RUN - would delete user {delete_user_name}")
        elif broker.delete_user(delete_user_name):
            if broker.user_exists(delete_user_name) is not False:
                failures.append("rabbitmq user deletion not verified")
        else:
            failures.append("rabbitmq user deletion")

    # === RESULT ==============================================

    logger.info("=" * 60)

    if failures:

        logger.error(f"Teardown INCOMPLETE - {len(failures)} step(s) failed:")

        for failure in failures:
            logger.error(f"  - {failure}")

        return EXIT_FAILED

    if args.dry_run:
        logger.info("Dry run complete - nothing was deleted.")
    else:
        logger.info("Teardown completed successfully.")

    return EXIT_OK


def parse_args():

    parser = argparse.ArgumentParser(
        description="Delete the ES index, RabbitMQ exchange, and "
                    "catalogue-created user for one NGSI-LD resource."
    )

    parser.add_argument(
        "--config",
        default="config.ini",
        help="path to the INI config file (default: config.ini)"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be deleted without deleting anything"
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
