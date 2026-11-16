#!/usr/bin/env python3
"""
rucio_registrar.py - register files archived by SDA as replicas in Rucio.

Written for the SDA v4.0.2-hotfix-2 S3 integration stack and the Rucio 41.5.1
dev environment, running inside the dev-rucio-1 container.

For every message on REGISTRAR_QUEUE (a copy of SDA's `completed` messages):
  1. take accession_id from the message
  2. look up archive_location, archive_file_path and archive_file_size in sda.files
  3. stream the archived object from S3 and compute its adler32
  4. register <scope>:<accession_id> on the RSE with an explicit PFN,
     https://sda-s3:443/<bucket>/<object key>

Modes:
  (no arguments)     consume the queue until stopped with Ctrl-C
  --accession ID     register one file, then exit
  --backfill         register every file that already has an accession ID, then exit

Only pika needs installing in dev-rucio-1 (python3 -m pip install pika).
boto3, psycopg (v3) and the Rucio client are already in the image.
The defaults below match the test stack; override them with environment variables.
"""
import argparse
import json
import logging
import os
import signal
import sys
import time
import zlib
from urllib.parse import urlparse

import boto3
import pika
import psycopg
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from psycopg import sql
from rucio.client import Client
from rucio.common.exception import DataIdentifierNotFound, Duplicate

AMQP_URL = os.environ.get("AMQP_URL", "amqp://rucio:rucio@rabbitmq:5672/sda")
AMQP_HEARTBEAT = int(os.environ.get("AMQP_HEARTBEAT", "600"))
QUEUE = os.environ.get("REGISTRAR_QUEUE", "rucio_registrar")
SDA_DB_DSN = os.environ.get("SDA_DB_DSN", "host=postgres dbname=sda user=rucio password=rucio sslmode=disable")
DB_SCHEMA = os.environ.get("SDA_DB_SCHEMA", "sda")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://s3:9000")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "rucioread")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "rucioreadsecret")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
PFN_BASE = os.environ.get("PFN_BASE", "https://sda-s3:443").rstrip("/")
RSE = os.environ.get("RUCIO_RSE", "SDA_ARCHIVE")
SCOPE = os.environ.get("RUCIO_SCOPE", "sda")
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", "5"))

log = logging.getLogger("rucio-registrar")


class PermanentError(Exception):
    """This message can never succeed, so it should not be retried."""


def parse_message(body):
    """Return the accession ID from a `completed` message body."""
    try:
        msg = json.loads(body)
    except (ValueError, UnicodeDecodeError) as err:
        raise PermanentError(f"message is not valid JSON: {err}")
    accession_id = msg.get("accession_id") if isinstance(msg, dict) else None
    if not accession_id or not isinstance(accession_id, str):
        raise PermanentError(f"message has no accession_id: {body[:200]!r}")
    return accession_id


def bucket_from_location(location):
    """'http://s3:9000/archive3' -> 'archive3'."""
    parsed = urlparse(location or "")
    bucket = parsed.path.strip("/")
    if parsed.scheme not in ("http", "https") or not bucket or "/" in bucket:
        raise PermanentError(f"unexpected archive_location {location!r}; expected <S3 endpoint>/<bucket>")
    return bucket


def make_pfn(bucket, key):
    return f"{PFN_BASE}/{bucket}/{key}"


def adler32_of(s3, bucket, key, chunk_size=8 * 1024 * 1024):
    """Stream an S3 object; return (adler32 as 8 hex digits, size in bytes)."""
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "NoSuchBucket", "404"):
            raise PermanentError(f"s3://{bucket}/{key} does not exist ({code})")
        raise
    value, size = 1, 0
    try:
        for block in iter(lambda: body.read(chunk_size), b""):
            value = zlib.adler32(block, value)
            size += len(block)
    finally:
        body.close()
    return f"{value & 0xFFFFFFFF:08x}", size


class Registrar:
    LOOKUP = sql.SQL(
        "SELECT archive_location, archive_file_path, archive_file_size FROM {}.files WHERE stable_id = %s"
    ).format(sql.Identifier(DB_SCHEMA))
    BACKFILL = sql.SQL(
        "SELECT stable_id FROM {}.files WHERE stable_id IS NOT NULL AND archive_file_path <> '' ORDER BY created_at"
    ).format(sql.Identifier(DB_SCHEMA))

    def __init__(self, rucio=None, s3=None):
        self.s3 = s3 or boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
            region_name=S3_REGION,
            config=BotoConfig(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
        )
        self.rucio = rucio or Client()  # reads /opt/rucio/etc/rucio.cfg
        self._db = None

    def query(self, statement, params=()):
        """Run a query, reconnecting on the next call if the connection dropped."""
        if self._db is None or self._db.closed:
            self._db = psycopg.connect(SDA_DB_DSN, autocommit=True)
        try:
            return self._db.execute(statement, params).fetchall()
        except psycopg.OperationalError:
            self._db = None
            raise

    def check_catalogue(self, name, size, adler32):
        """Refuse to register over a Rucio entry that describes a different file."""
        try:
            meta = self.rucio.get_metadata(SCOPE, name)
        except DataIdentifierNotFound:
            return
        if meta.get("bytes") != size or meta.get("adler32") != adler32:
            raise PermanentError(
                f"{SCOPE}:{name} already exists in Rucio with bytes={meta.get('bytes')} "
                f"adler32={meta.get('adler32')}, but the archived object has bytes={size} "
                f"adler32={adler32}. Rucio still holds an entry from an earlier SDA stack; reset Rucio."
            )

    def register(self, accession_id):
        """Register one file. Returns 'registered' or 'exists'."""
        rows = self.query(self.LOOKUP, (accession_id,))
        if not rows:
            raise PermanentError(f"no file with accession ID {accession_id} in {DB_SCHEMA}.files")
        location, key, size = rows[0]
        if not key:
            raise PermanentError(f"{accession_id} has no archive path")
        bucket = bucket_from_location(location)

        adler32, streamed = adler32_of(self.s3, bucket, key)
        if size is not None and streamed != size:
            raise PermanentError(
                f"{accession_id}: s3://{bucket}/{key} is {streamed} bytes, but {DB_SCHEMA}.files says {size}"
            )
        self.check_catalogue(accession_id, streamed, adler32)

        replica = {"scope": SCOPE, "name": accession_id, "bytes": streamed,
                   "adler32": adler32, "pfn": make_pfn(bucket, key)}
        try:
            self.rucio.add_replicas(rse=RSE, files=[replica])
        except Duplicate:
            log.info("%s:%s is already registered on %s", SCOPE, accession_id, RSE)
            return "exists"
        log.info("registered %s:%s on %s -> %s (%d bytes, adler32 %s)",
                 SCOPE, accession_id, RSE, replica["pfn"], streamed, adler32)
        return "registered"

    def on_message(self, channel, method, _properties, body):
        try:
            self.register(parse_message(body))
            channel.basic_ack(method.delivery_tag)
        except PermanentError as err:
            log.error("dropping message: %s", err)
            channel.basic_reject(method.delivery_tag, requeue=False)
        except Exception:
            # Rucio, S3 or DB unreachable, or a setup problem such as a missing
            # scope or RSE: keep the message and retry after a pause.
            log.exception("registration failed; requeueing in %ss", RETRY_DELAY)
            time.sleep(RETRY_DELAY)
            channel.basic_nack(method.delivery_tag, requeue=True)


def consume(registrar):
    params = pika.URLParameters(AMQP_URL)
    params.heartbeat = AMQP_HEARTBEAT  # long enough to checksum large files inside the callback
    while True:
        connection = None
        try:
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue=QUEUE, on_message_callback=registrar.on_message)
            log.info("waiting for messages on %s (vhost %s)", QUEUE, params.virtual_host)
            channel.start_consuming()
        except KeyboardInterrupt:
            log.info("stopping")
            if connection and connection.is_open:
                connection.close()
            return 0
        except (pika.exceptions.ProbableAuthenticationError, pika.exceptions.ProbableAccessDeniedError) as err:
            log.error("RabbitMQ refused the login or vhost: %r; check AMQP_URL and sda-side.sh", err)
            return 1
        except pika.exceptions.ChannelClosedByBroker as err:
            if err.reply_code in (403, 404):
                log.error("cannot consume from %s: %s; check that sda-side.sh ran", QUEUE, err.reply_text)
                return 1
            log.warning("channel closed by broker (%s); reconnecting in %ss", err, RETRY_DELAY)
        except pika.exceptions.AMQPConnectionError as err:
            log.warning("RabbitMQ connection lost (%r); reconnecting in %ss", err, RETRY_DELAY)
        time.sleep(RETRY_DELAY)


def backfill(registrar):
    counts = {"registered": 0, "exists": 0, "skipped": 0, "failed": 0}
    for (accession_id,) in registrar.query(Registrar.BACKFILL):
        try:
            counts[registrar.register(accession_id)] += 1
        except PermanentError as err:
            counts["skipped"] += 1
            log.error("skipped %s: %s", accession_id, err)
        except Exception as err:
            counts["failed"] += 1
            log.error("failed %s: %s", accession_id, err)
    log.info("backfill finished: %s", counts)
    return 0 if counts["failed"] == 0 else 1


def _stop(_signum, _frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Register SDA-archived files as Rucio replicas.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--accession", metavar="ID", help="register one accession ID and exit")
    mode.add_argument("--backfill", action="store_true", help="register every finalized file and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("pika", "botocore", "urllib3", "boto3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    registrar = Registrar()
    if args.accession:
        try:
            print(registrar.register(args.accession))
            return 0
        except PermanentError as err:
            log.error("%s", err)
            return 1
    if args.backfill:
        return backfill(registrar)
    # Stop cleanly on `docker stop`/`kill` as well as Ctrl-C.
    signal.signal(signal.SIGTERM, _stop)
    return consume(registrar)


if __name__ == "__main__":
    sys.exit(main())#!/usr/bin/env python3
"""
rucio_registrar.py - register files archived by SDA as replicas in Rucio.

Written for the SDA v4.0.2-hotfix-2 S3 integration stack and the Rucio 41.5.1
dev environment, running inside the dev-rucio-1 container.

For every message on REGISTRAR_QUEUE (a copy of SDA's `completed` messages):
  1. take accession_id from the message
  2. look up archive_location, archive_file_path and archive_file_size in sda.files
  3. stream the archived object from S3 and compute its adler32
  4. register <scope>:<accession_id> on the RSE with an explicit PFN,
     https://sda-s3:443/<bucket>/<object key>

Modes:
  (no arguments)     consume the queue until stopped with Ctrl-C
  --accession ID     register one file, then exit
  --backfill         register every file that already has an accession ID, then exit

Only pika needs installing in dev-rucio-1 (python3 -m pip install pika).
boto3, psycopg (v3) and the Rucio client are already in the image.
The defaults below match the test stack; override them with environment variables.
"""
import argparse
import json
import logging
import os
import signal
import sys
import time
import zlib
from urllib.parse import urlparse

import boto3
import pika
import psycopg
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from psycopg import sql
from rucio.client import Client
from rucio.common.exception import DataIdentifierNotFound, Duplicate

AMQP_URL = os.environ.get("AMQP_URL", "amqp://rucio:rucio@rabbitmq:5672/sda")
AMQP_HEARTBEAT = int(os.environ.get("AMQP_HEARTBEAT", "600"))
QUEUE = os.environ.get("REGISTRAR_QUEUE", "rucio_registrar")
SDA_DB_DSN = os.environ.get("SDA_DB_DSN", "host=postgres dbname=sda user=rucio password=rucio sslmode=disable")
DB_SCHEMA = os.environ.get("SDA_DB_SCHEMA", "sda")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "http://s3:9000")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "rucioread")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "rucioreadsecret")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
PFN_BASE = os.environ.get("PFN_BASE", "https://sda-s3:443").rstrip("/")
RSE = os.environ.get("RUCIO_RSE", "SDA_ARCHIVE")
SCOPE = os.environ.get("RUCIO_SCOPE", "sda")
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", "5"))

log = logging.getLogger("rucio-registrar")


class PermanentError(Exception):
    """This message can never succeed, so it should not be retried."""


def parse_message(body):
    """Return the accession ID from a `completed` message body."""
    try:
        msg = json.loads(body)
    except (ValueError, UnicodeDecodeError) as err:
        raise PermanentError(f"message is not valid JSON: {err}")
    accession_id = msg.get("accession_id") if isinstance(msg, dict) else None
    if not accession_id or not isinstance(accession_id, str):
        raise PermanentError(f"message has no accession_id: {body[:200]!r}")
    return accession_id


def bucket_from_location(location):
    """'http://s3:9000/archive3' -> 'archive3'."""
    parsed = urlparse(location or "")
    bucket = parsed.path.strip("/")
    if parsed.scheme not in ("http", "https") or not bucket or "/" in bucket:
        raise PermanentError(f"unexpected archive_location {location!r}; expected <S3 endpoint>/<bucket>")
    return bucket


def make_pfn(bucket, key):
    return f"{PFN_BASE}/{bucket}/{key}"


def adler32_of(s3, bucket, key, chunk_size=8 * 1024 * 1024):
    """Stream an S3 object; return (adler32 as 8 hex digits, size in bytes)."""
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "NoSuchBucket", "404"):
            raise PermanentError(f"s3://{bucket}/{key} does not exist ({code})")
        raise
    value, size = 1, 0
    try:
        for block in iter(lambda: body.read(chunk_size), b""):
            value = zlib.adler32(block, value)
            size += len(block)
    finally:
        body.close()
    return f"{value & 0xFFFFFFFF:08x}", size


class Registrar:
    LOOKUP = sql.SQL(
        "SELECT archive_location, archive_file_path, archive_file_size FROM {}.files WHERE stable_id = %s"
    ).format(sql.Identifier(DB_SCHEMA))
    BACKFILL = sql.SQL(
        "SELECT stable_id FROM {}.files WHERE stable_id IS NOT NULL AND archive_file_path <> '' ORDER BY created_at"
    ).format(sql.Identifier(DB_SCHEMA))

    def __init__(self, rucio=None, s3=None):
        self.s3 = s3 or boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
            region_name=S3_REGION,
            config=BotoConfig(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
        )
        self.rucio = rucio or Client()  # reads /opt/rucio/etc/rucio.cfg
        self._db = None

    def query(self, statement, params=()):
        """Run a query, reconnecting on the next call if the connection dropped."""
        if self._db is None or self._db.closed:
            self._db = psycopg.connect(SDA_DB_DSN, autocommit=True)
        try:
            return self._db.execute(statement, params).fetchall()
        except psycopg.OperationalError:
            self._db = None
            raise

    def check_catalogue(self, name, size, adler32):
        """Refuse to register over a Rucio entry that describes a different file."""
        try:
            meta = self.rucio.get_metadata(SCOPE, name)
        except DataIdentifierNotFound:
            return
        if meta.get("bytes") != size or meta.get("adler32") != adler32:
            raise PermanentError(
                f"{SCOPE}:{name} already exists in Rucio with bytes={meta.get('bytes')} "
                f"adler32={meta.get('adler32')}, but the archived object has bytes={size} "
                f"adler32={adler32}. Rucio still holds an entry from an earlier SDA stack; reset Rucio."
            )

    def register(self, accession_id):
        """Register one file. Returns 'registered' or 'exists'."""
        rows = self.query(self.LOOKUP, (accession_id,))
        if not rows:
            raise PermanentError(f"no file with accession ID {accession_id} in {DB_SCHEMA}.files")
        location, key, size = rows[0]
        if not key:
            raise PermanentError(f"{accession_id} has no archive path")
        bucket = bucket_from_location(location)

        adler32, streamed = adler32_of(self.s3, bucket, key)
        if size is not None and streamed != size:
            raise PermanentError(
                f"{accession_id}: s3://{bucket}/{key} is {streamed} bytes, but {DB_SCHEMA}.files says {size}"
            )
        self.check_catalogue(accession_id, streamed, adler32)

        replica = {"scope": SCOPE, "name": accession_id, "bytes": streamed,
                   "adler32": adler32, "pfn": make_pfn(bucket, key)}
        try:
            self.rucio.add_replicas(rse=RSE, files=[replica])
        except Duplicate:
            log.info("%s:%s is already registered on %s", SCOPE, accession_id, RSE)
            return "exists"
        log.info("registered %s:%s on %s -> %s (%d bytes, adler32 %s)",
                 SCOPE, accession_id, RSE, replica["pfn"], streamed, adler32)
        return "registered"

    def on_message(self, channel, method, _properties, body):
        try:
            self.register(parse_message(body))
            channel.basic_ack(method.delivery_tag)
        except PermanentError as err:
            log.error("dropping message: %s", err)
            channel.basic_reject(method.delivery_tag, requeue=False)
        except Exception:
            # Rucio, S3 or DB unreachable, or a setup problem such as a missing
            # scope or RSE: keep the message and retry after a pause.
            log.exception("registration failed; requeueing in %ss", RETRY_DELAY)
            time.sleep(RETRY_DELAY)
            channel.basic_nack(method.delivery_tag, requeue=True)


def consume(registrar):
    params = pika.URLParameters(AMQP_URL)
    params.heartbeat = AMQP_HEARTBEAT  # long enough to checksum large files inside the callback
    while True:
        connection = None
        try:
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue=QUEUE, on_message_callback=registrar.on_message)
            log.info("waiting for messages on %s (vhost %s)", QUEUE, params.virtual_host)
            channel.start_consuming()
        except KeyboardInterrupt:
            log.info("stopping")
            if connection and connection.is_open:
                connection.close()
            return 0
        except (pika.exceptions.ProbableAuthenticationError, pika.exceptions.ProbableAccessDeniedError) as err:
            log.error("RabbitMQ refused the login or vhost: %r; check AMQP_URL and sda-side.sh", err)
            return 1
        except pika.exceptions.ChannelClosedByBroker as err:
            if err.reply_code in (403, 404):
                log.error("cannot consume from %s: %s; check that sda-side.sh ran", QUEUE, err.reply_text)
                return 1
            log.warning("channel closed by broker (%s); reconnecting in %ss", err, RETRY_DELAY)
        except pika.exceptions.AMQPConnectionError as err:
            log.warning("RabbitMQ connection lost (%r); reconnecting in %ss", err, RETRY_DELAY)
        time.sleep(RETRY_DELAY)


def backfill(registrar):
    counts = {"registered": 0, "exists": 0, "skipped": 0, "failed": 0}
    for (accession_id,) in registrar.query(Registrar.BACKFILL):
        try:
            counts[registrar.register(accession_id)] += 1
        except PermanentError as err:
            counts["skipped"] += 1
            log.error("skipped %s: %s", accession_id, err)
        except Exception as err:
            counts["failed"] += 1
            log.error("failed %s: %s", accession_id, err)
    log.info("backfill finished: %s", counts)
    return 0 if counts["failed"] == 0 else 1


def _stop(_signum, _frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Register SDA-archived files as Rucio replicas.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--accession", metavar="ID", help="register one accession ID and exit")
    mode.add_argument("--backfill", action="store_true", help="register every finalized file and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("pika", "botocore", "urllib3", "boto3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    registrar = Registrar()
    if args.accession:
        try:
            print(registrar.register(args.accession))
            return 0
        except PermanentError as err:
            log.error("%s", err)
            return 1
    if args.backfill:
        return backfill(registrar)
    # Stop cleanly on `docker stop`/`kill` as well as Ctrl-C.
    signal.signal(signal.SIGTERM, _stop)
    return consume(registrar)


if __name__ == "__main__":
    sys.exit(main())
