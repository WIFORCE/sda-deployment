#!/usr/bin/env bash
# sda-side.sh - give Rucio least-privilege access to the SDA stack:
#   1. S3:       read-only subuser sda:rucio on SDA's RGW user, with key rucioread
#   2. Postgres: role rucio that can only SELECT from sda.files
#   3. RabbitMQ: queue rucio_registrar, which gets a copy of every `completed`
#                message, and user rucio, which can only consume from it
#
# Safe to rerun: each step creates what is missing and fixes what is wrong.
# Rerun it after any SDA `down -v`, or after s3, postgres or rabbitmq are
# recreated without their volumes. Then run the registrar once with --backfill
# to pick up anything archived in the meantime.
set -euo pipefail

# These must match the registrar's defaults (S3_ACCESS_KEY, S3_SECRET_KEY,
# SDA_DB_DSN, AMQP_URL) and the keys rucio-side.sh writes to rse-accounts.cfg.
S3_KEY=rucioread
S3_SECRET=rucioreadsecret
DB_PASS=rucio
MQ_PASS=rucio
MQ=http://localhost:15672/api
MQ_ADMIN=guest:guest

fail() { echo "ERROR: $*" >&2; exit 1; }
rgw() { docker exec s3 radosgw-admin -c /etc/ceph/ceph.conf -k /etc/ceph/keyring "$@"; }
# SDA's Postgres image demands a password even on the local socket, so take the
# superuser's password from the container's own environment.
sdapsql() { docker exec -i postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -q -U postgres -d sda -v ON_ERROR_STOP=1 "$@"' sh "$@"; }
mq() { curl -fsS -u "$MQ_ADMIN" -H 'content-type: application/json' "$@"; }

echo "== S3: read-only subuser sda:rucio"
INFO=$(rgw user info --uid=sda) || fail "RGW user 'sda' not found; is the s3 container up and initialised?"
if jq -e --arg k "$S3_KEY" --arg s "$S3_SECRET" '
     any(.subusers[]; .id == "sda:rucio" and .permissions == "read") and
     any(.keys[]; .user == "sda:rucio" and .access_key == $k and .secret_key == $s)' <<<"$INFO" >/dev/null; then
  echo "already in place"
else
  if jq -e 'any(.subusers[]; .id == "sda:rucio")' <<<"$INFO" >/dev/null; then
    echo "exists with the wrong key or permissions; recreating it"
    rgw subuser rm --uid=sda --subuser=sda:rucio --purge-keys >/dev/null
  fi
  rgw subuser create --uid=sda --subuser=sda:rucio --access=read --key-type=s3 \
    --access-key="$S3_KEY" --secret-key="$S3_SECRET" >/dev/null
  echo "created"
fi

echo "== Postgres: SELECT-only role rucio"
sdapsql -v pass="$DB_PASS" <<'SQL'
SELECT 'CREATE ROLE rucio' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rucio') \gexec
ALTER ROLE rucio LOGIN PASSWORD :'pass';
GRANT USAGE ON SCHEMA sda TO rucio;
GRANT SELECT ON sda.files TO rucio;
SQL
echo "role and grants in place"

echo "== RabbitMQ: queue rucio_registrar and consume-only user rucio"
mq -X PUT  "$MQ/queues/sda/rucio_registrar" -d '{"durable":true}'
mq -X POST "$MQ/bindings/sda/e/sda/q/rucio_registrar" -d '{"routing_key":"completed"}'
mq -X PUT  "$MQ/users/rucio" -d "{\"password\":\"$MQ_PASS\",\"tags\":\"\"}"
mq -X PUT  "$MQ/permissions/sda/rucio" -d '{"configure":"","write":"","read":"^rucio_registrar$"}'
echo "queue, binding, user and permissions in place"

echo "== Verify"
rgw user info --uid=sda | jq -e --arg k "$S3_KEY" 'any(.keys[]; .user == "sda:rucio" and .access_key == $k)' >/dev/null \
  || fail "S3 key $S3_KEY for sda:rucio is missing"
echo "s3:       sda:rucio has key $S3_KEY"
ROWS=$(docker exec -e PGPASSWORD="$DB_PASS" postgres psql -X -h 127.0.0.1 -U rucio -d sda -Atc 'SELECT count(*) FROM sda.files') \
  || fail "role rucio cannot log in to Postgres or read sda.files"
echo "postgres: rucio logs in with its password and can read sda.files ($ROWS rows)"
mq "$MQ/exchanges/sda/sda/bindings/source" \
  | jq -e 'any(.[]; .destination == "rucio_registrar" and .routing_key == "completed")' >/dev/null \
  || fail "rucio_registrar is not bound to routing key completed on exchange sda"
echo "rabbitmq: rucio_registrar $(mq "$MQ/queues/sda/rucio_registrar" | jq -c '{messages, consumers}')"
echo "Done."
