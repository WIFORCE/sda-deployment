# Rucio on a running SDA stack: deployment manual

This manual brings up a minimal Rucio next to an SDA stack that is already running. It connects the two and registers every file SDA archives as a Rucio replica. Each step gives the commands, what to expect, and why it is done that way.

**Ground rule: the SDA stack is live and stays live.** Every SDA command in this manual reads from the stack or adds a small, separate identity for Rucio. Nothing here stops, recreates or wipes an SDA container, volume or network. In particular, `docker compose down -v` on the `sda-deployment` project will also break the Rucio deployment.

## So far

Rucio 41.5.1 is up and connected to SDA's network. Started the registrar in `dev-rucio-1`, then ran SDA's integration test in a second terminal, and the test passed. The registrar received the resulting `completed` messages. It could not log in to SDA's Postgres as `rucio`, though, because the database step of the original `sda-side.sh` had never taken effect. The messages are not lost. The registrar puts each one back on the queue and retries every 5 seconds.



## How the pieces fit

```
SDA  (compose project sda-deployment, network sda-deployment_default, 10.100.0.0/24)
  finalize ──"completed"──▶ exchange sda ─┬─▶ completed_stream   SDA's own consumers (unchanged)
                                          └─▶ rucio_registrar    Rucio's copy 
  postgres   sda.files: accession ID, bucket (archive_location), object key, size
  s3         Ceph RGW over plain HTTP at http://s3:9000; buckets archive1, archive2, …
               ▲ plain HTTP
  sda-s3     nginx, TLS on 443, certificate signed by Rucio's dev CA 
               ▲ HTTPS with presigned URLs
Rucio (compose project dev, network ruciodevnetwork, also joined to sda-deployment_default)
  dev-rucio-1     Rucio server, CLI and rucio_registrar.py
  dev-ruciodb-1   Rucio's own Postgres (catalogue), separate from SDA's
  dev-graphite-1  statsd sink; nothing reads it, but it must exist 
```

SDA stays the owner of the data. Rucio is a catalogue on top of it: one storage element (RSE), `SDA_ARCHIVE`, that points at SDA's archive buckets through `sda-s3`. The registrar is the bridge. For each `completed` message, it looks the file up in `sda.files`, streams the object from S3 to compute an adler32 checksum, and registers `sda:<accession ID>` on `SDA_ARCHIVE` with its exact location.

Rucio only ever gets read access to SDA: a read-only S3 key, a `SELECT`-only database role, and a user that can only consume its own queue. So even a Rucio misconfiguration cannot modify the archive.

## Files used

The helper files live in `~/sda-rucio/`:

| File | What it is | Where it comes from |
|---|---|---|
| `sda-s3.conf` | nginx config for the TLS proxy | 
| `sda-side.sh` | Rucio's identities on the SDA side; safe to rerun | 
| `rucio-side.sh` | Scope, RSE, quota and S3 credentials on the Rucio side | 
| `rucio_registrar.py` | The bridge, copied into `dev-rucio-1` |  


---

## Check the running SDA stack (read-only)

Confirm that every SDA component Rucio relies on is healthy.

```bash
# Check all conatiners are running and healthy. The credentials container will be exited but that is normal
docker compose ls  
# For more detailed/formatted info use
docker compose -f /home/admin/Git/sda-deployment/docker-compose.yml ps --format 'table {{.Name}}\t{{.State}}\t{{.Status}}'

# Check the network config, since we will add Rucio to this network
docker network inspect sda-deployment_default -f '{{(index .IPAM.Config 0).Subnet}}'          # 10.100.0.0/24, note that this is allowed in the firewall. 
docker network inspect sda-deployment_default -f '{{range .Containers}}{{.Name}} {{end}}'

# Check SDA-postgres status. The RUCIO registrar reads each file's bucket, key and size from `sda.files`, so the table must be readable. 
docker exec postgres pg_isready -U postgres # accepting connections
sdapsql() { docker exec -i postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -q -U postgres -d sda -v ON_ERROR_STOP=1 "$@"' sh "$@"; }
sdapsql -Atc "select count(*) from sda.files"
sdapsql -Atc "select rolname from pg_roles where rolname = 'rucio'" # 'rucio' once sda-side.sh has run


# Check SDA-raabbitmq. The Rucio registrar learns about new archive files from SDA's `completed` messages, so it needs the vhost and exchange names. The vhost is `sda`. SDA's `config.yaml` writes it as `/sda`, but `definitions.json` defines it as `sda`, and the client adds the slash, so the management-API paths use `sda`. Before running sda-side.sh, `completed` routes only to `completed_stream`.
docker exec rabbitmq rabbitmq-diagnostics -q check_running
curl -fsS -u guest:guest http://localhost:15672/api/vhosts | jq -r '.[].name'   # sda
curl -fsS -u guest:guest http://localhost:15672/api/exchanges/sda/sda/bindings/source \
  | jq -r '.[] | select(.routing_key=="completed") | .destination'             # completed_stream (+ rucio_registrar after running sda-side.sh detailed below)

# Check SDA-s3. SDA spreads archived files across buckets `archive1`, `archive2` and so on. Rucio's read-only key belongs to a subuser of the RGW user `sda`, so it can only read buckets that `sda` owns.
RGW="docker exec s3 radosgw-admin -c /etc/ceph/ceph.conf -k /etc/ceph/keyring"
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:19000/  # 200 or 403 = up; 000 = down
$RGW bucket list 2>/dev/null  # inbox, inbox1, archive1, backup1
$RGW user info --uid=sda 2>/dev/null | jq -r '.subusers[].id' # sda:rucio once sda-side.sh has run
$RGW bucket stats --bucket=archive1 2>/dev/null | jq -r .owner  # sda

# Make sure that SDA stores information in a way that Rucio registrar can read
docker inspect -f '{{.Name}}  {{.Config.Image}}' ingest finalize postgres rabbitmq | sed 's|^/||' # as we fixed SDA to one version, if the Rcuio registrar ever fails, one reason could be that the version of SDA changed the format of info stored.  
sdapsql() { docker exec -i postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -q -U postgres -d sda -v ON_ERROR_STOP=1 "$@"' sh "$@"; }
sdapsql -c '\d sda.files' | grep -E 'stable_id|archive_(location|file_path|file_size)' # `archive_location` holds the S3 endpoint plus the bucket (`http://s3:9000/archive1`), and `archive_file_path` holds the object key, a UUID. The registrar derives the bucket from `archive_location` and treats any other format as a permanent error, which drops the message. Also, if one is missing, the lookup fails with a database error. The registrar treats that as temporary, so it would retry the same message every 5 seconds forever.
sdapsql -c "select count(*) filter (where stable_id is not null and archive_file_path <> '') as backfillable,
                   count(*) as total from sda.files" 
# The backfillable count is the number of files backfill will register: those with an accession ID and an archive path. Files still in ingest have neither and are skipped.
sdapsql -c "select stable_id, archive_location, archive_file_path from sda.files where stable_id is not null limit 3"
```

## Bring up the minimal Rucio stack (pinned to 41.5.1)

The Rucio dev compose file mounts checkout's `lib`, `bin`, `tools` and `tests` directories into the container, on top of the code in the image. The image supplies only the Python dependencies and the server setup. If the checkout and the image come from different releases, the code can expect dependencies or a database schema the image doesn't have. Without a pin, `RUCIO_TAG` defaults to `latest`, which follows master. So to avoid breaking the stack, pinned the version to the lastest (similar to SDA).

Every script in this manual was checked against the 41.5.1 source: the CLI syntax, the protocol-priority rules, URL signing, the credentials file location, and which services are required. The registrar was tested against `rucio-clients==41.5.1`.


### Clone a pinned version and start up the minimal services

```bash
cd /home/admin/Git/sda-deployment
git clone --depth 1 --branch 41.5.1 https://github.com/rucio/rucio.git rucio-41.5.1
git -C rucio-41.5.1 describe --tags        # 41.5.1
git -C /home/admin/Git/sda-deployment/rucio-41.5.1 status --short etc/docker/dev  # only "?? etc/docker/dev/.env"
grep -n subnet /home/admin/Git/sda-deployment/rucio-41.5.1/etc/docker/dev/docker-compose.yml   # no output

# Make sure that the ports do not clash
ports() { docker compose "$@" config --format json | jq -r '.services[] | .ports[]? | .published' | sort -u; }
ports -f /home/admin/Git/sda-deployment/rucio-41.5.1                                                     # nothing
ss -Htlnu | awk '{print $5}' | sed 's/.*://' | sort -u > /tmp/inuse; ports -f /home/admin/Git/sda-deployment/rucio-41.5.1/etc/docker/dev/docker-compose.yml | comm -12 - /tmp/inuse   # nothing

# Start only 3 Rucio services. These are the minimum required services for Rucio. 
docker compose -f /home/admin/Git/sda-deployment/rucio-41.5.1/etc/docker/dev/docker-compose.yml up -d --wait ruciodb graphite
docker compose -f /home/admin/Git/sda-deployment/rucio-41.5.1/etc/docker/dev/docker-compose.yml up -d --no-deps rucio
docker compose -f /home/admin/Git/sda-deployment/rucio-41.5.1/etc/docker/dev/docker-compose.yml ps                                           # dev-ruciodb-1, dev-graphite-1, dev-rucio-1
docker network inspect ruciodevnetwork -f '{{(index .IPAM.Config 0).Subnet}}'   # 10.100.N.0/24, not 172.19
docker ps --format '{{.Names}}\t{{.Ports}}' | grep ^dev-             # no "->" anywhere
```

More context on the 3 services:

- **`ruciodb`** is Rucio's catalogue database.
- **`graphite` must exist even though nothing reads the metrics.** Not sure I understand why though. 

`--no-deps` is needed because the `rucio` service's `depends_on` lists `elasticsearch` and `influxdb`, which a plain `up rucio` would start too. Don't enable the `externalmetadata` profile. It has its own service called `postgres`, which could make the registrar's `host=postgres` reach the wrong database.

### Initialize and check

```bash
docker exec -it dev-rucio-1 tools/run_tests.sh -i
docker exec dev-rucio-1 rucio ping
docker exec dev-rucio-1 rucio whoami                                  # account root
docker exec dev-rucio-1 rucio rse list                                # test RSEs from the sync (+ SDA_ARCHIVE after connecting SDA and Rucio)
```

## Connect Rucio to SDA

### Join SDA's network

```bash
docker network connect sda-deployment_default dev-rucio-1
docker exec dev-rucio-1 getent hosts rabbitmq postgres s3 graphite   # all four resolve
```
Docker's embedded DNS resolves service names only for containers on the same network. Once joined, the registrar can reach `rabbitmq`, `postgres` and `s3` by name, which is what its defaults assume, while `graphite` still resolves on Rucio's own network. The extra network survives `docker restart` but not a container recreate.

### TLS proxy in front of SDA's S3 (`sda-s3`)

Rucio hands out downloads as presigned S3 URLs: temporary links with a signature, so clients never hold the S3 keys. Rucio 41.5.1's `core/credential.py` only builds those URLs as `https`, but SDA's RGW speaks plain HTTP. So we need an nginx container that adds TLS in front of it.

Its certificate is signed by the Rucio dev environment's own CA, which `dev-rucio-1` already trusts. Rucio's dev setup fronts its own S3 store (Garage) the same way. The subject alternative name `sda-s3` must match the hostname used in the RSE protocol (detailed when running rucio-side.sh below) and in the registrar's PFNs.

```bash
cd /home/admin/Git/sda-deployment/rucio-41.5.1/etc/certs
openssl req -new -newkey rsa:2048 -noenc -keyout hostcert_sda-s3.key.pem -subj "/CN=sda-s3" -out sda-s3.csr
openssl x509 -req -days 825 -CAcreateserial -in sda-s3.csr -CA rucio_ca.pem -CAkey rucio_ca.key.pem \
  -extfile <(printf "subjectAltName=DNS:sda-s3\nextendedKeyUsage=serverAuth") -out hostcert_sda-s3.pem

cat > ~/sda-rucio/sda-s3.conf <<'EOF'
server {
    listen 443 ssl;
    server_name sda-s3;
    ssl_certificate     /etc/nginx/cert.pem;
    ssl_certificate_key /etc/nginx/key.pem;
    resolver 127.0.0.11 valid=10s ipv6=off;
    location / {
        set $sda_s3 http://s3:9000;
        client_max_body_size 0; # Large archive files stream through without size limits or temporary files.
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_pass $sda_s3;
        proxy_set_header Host $host;
    }
}
EOF
docker rm -f sda-s3 2>/dev/null
docker run -d --name sda-s3 --restart unless-stopped --network sda-deployment_default \
  -v ~/sda-rucio/sda-s3.conf:/etc/nginx/conf.d/default.conf:ro \
  -v /home/admin/Git/sda-deployment/rucio-41.5.1/etc/certs/hostcert_sda-s3.pem:/etc/nginx/cert.pem:ro \
  -v /home/admin/Git/sda-deployment/rucio-41.5.1/etc/certs/hostcert_sda-s3.key.pem:/etc/nginx/key.pem:ro nginx:1.27-alpine
docker exec dev-rucio-1 curl -s -o /dev/null -w '%{http_code}\n' https://sda-s3/   # 200/403 = OK; 000 = TLS/CA problem
docker exec sda-s3 grep -c resolver /etc/nginx/conf.d/default.conf                 # 1 = hardened config in use
```

### SDA-side identities: `sda-side.sh` 

Rucio and the registrar need their own access to SDA’s storage, database and message queue, and this script sets that up without giving them any power to change SDA. It creates a read-only S3 key, a Postgres role that can only read sda.files, and a dedicated rucio_registrar queue that receives a copy of every completed message, so SDA’s own services are unaffected. Without it, the registrar can’t log in anywhere


```bash
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
ROWS=$(docker exec -e PGPASSWORD="$DB_PASS" postgres psql -X -h 127.0.0.1 -U rucio -d sda -Atc 'SELECT count(*) FROM sda.files
') \
  || fail "role rucio cannot log in to Postgres or read sda.files"
echo "postgres: rucio logs in with its password and can read sda.files ($ROWS rows)"
mq "$MQ/exchanges/sda/sda/bindings/source" \
  | jq -e 'any(.[]; .destination == "rucio_registrar" and .routing_key == "completed")' >/dev/null \
  || fail "rucio_registrar is not bound to routing key completed on exchange sda"
echo "rabbitmq: rucio_registrar $(mq "$MQ/queues/sda/rucio_registrar" | jq -c '{messages, consumers}')"
echo "Done."
```


```bash
bash ~/sda-rucio/sda-side.sh

# Expected ending:
#== Verify
#s3:       sda:rucio has key rucioread
#postgres: rucio logs in with its password and can read sda.files (<N> rows)
#rabbitmq: rucio_registrar {"messages":<n>,"consumers":1}
#Done.
```

### Rucio-side registration: `rucio-side.sh`

A fresh Rucio knows nothing about SDA, so this script tells it where SDA’s files live and how to reach them. It creates the sda scope and the SDA_ARCHIVE storage element, pointed at the sda-s3 proxy, and sets the quota and URL-signing permission Rucio requires. It also writes the read-only S3 key into Rucio’s credentials file so Rucio can generate download links. Without it, the registrar has nowhere to register files, and rucio download can’t fetch them.

```bash
cat > ~/sda-rucio/rucio-side.sh <<'OUTER'
docker exec -i dev-rucio-1 bash <<'EOF'
set -e
rucio scope add sda --account root
rucio rse add SDA_ARCHIVE --non-deterministic
rucio rse protocol add SDA_ARCHIVE --hostname sda-s3 --scheme https --port 443 --prefix / \
  --impl rucio.rse.protocols.gfal.NoRename \
  --domain-json '{"wan":{"read":1,"write":1,"delete":null,"third_party_copy_read":1,"third_party_copy_write":null},"lan":{"read":1,"write":1,"delete":null}}'
for kv in sign_url=s3 skip_upload_stat=True verify_checksum=False strict_copy=True s3_url_style=path; do
  rucio rse attribute set SDA_ARCHIVE --key "${kv%%=*}" --value "${kv#*=}"
done
rucio rse update SDA_ARCHIVE --key availability_delete --value False
rucio account limit set root --rse SDA_ARCHIVE --bytes infinity
rucio account attribute set root --key sign-url --value true
RSE_ID=$(python3 -c "from rucio.client import Client; print(Client().get_rse('SDA_ARCHIVE')['id'])")
echo "{\"$RSE_ID\": {\"access_key\": \"rucioread\", \"secret_key\": \"rucioreadsecret\", \"signature_version\": \"s3v4\", \"region\": \"us-east-1\"}}" \
  > /opt/rucio/etc/rse-accounts.cfg
EOF
OUTER
bash ~/sda-rucio/rucio-side.sh
docker restart dev-rucio-1
docker exec dev-rucio-1 rucio rse show SDA_ARCHIVE | head -5
```

---

## Run the registrar and verify end to end

### What the registrar does

`rucio_registrar.py` runs inside `dev-rucio-1`. For each message on `rucio_registrar`, it does four things:

1. It reads `accession_id`.
2. It looks up `archive_location`, `archive_file_path` and `archive_file_size` in `sda.files`. SDA writes this row before publishing `completed`, so the row is always there.
3. It streams the object from S3 and computes adler32, checking the size against the database. Rucio needs this checksum type, and SDA doesn't record it.
4. It registers `sda:<accession ID>` on `SDA_ARCHIVE` with PFN `https://sda-s3:443/<bucket>/<key>`.

Each message then ends one of three ways:

- **Acknowledged.** Successes and "already registered" are both acknowledged, so repeats are harmless.
- **Dropped.** Messages that can never succeed are rejected without requeue: invalid JSON, a missing ID, an unknown accession ID, a missing object or a size mismatch.
- **Retried.** Temporary failures go back on the queue after 5 seconds, so nothing is lost: a service that is down, a database login failure, or a missing setup step.

Before registering, it refuses to overwrite an existing Rucio entry that has a different size or checksum. Rucio would otherwise silently keep the stale entry, for example after SDA was rebuilt but Rucio wasn't.

The AMQP heartbeat is raised to 600 seconds, so large files can be checksummed inside the callback without the broker dropping the connection. `Ctrl-C`, `docker stop` and SIGTERM all stop it cleanly, and an unacknowledged message is simply redelivered later. Two one-off modes exist: `--backfill` registers every file that already has an accession ID, and `--accession ID` registers one file.

Its defaults (hosts `rabbitmq`, `postgres` and `s3`; the `rucio` credentials; `SDA_ARCHIVE`; scope `sda`) match this setup, so it needs no environment variables. It needs `pika` installed; `psycopg` 3, `boto3` and the Rucio client already ship in the image. It was tested locally against real RabbitMQ and Postgres, a mock S3, and a stand-in for Rucio that checks PFNs with Rucio's own parser. The run in this step is its first against the real stack.

### Install and start it (terminal 1)

```bash
cd /home/admin/Git/sda-deployment/sda-rucio
python3 -m py_compile rucio_registrar.py && echo syntax-ok
docker cp rucio_registrar.py dev-rucio-1:/opt/rucio_registrar.py
docker exec dev-rucio-1 python3 -m pip install pika
docker exec dev-rucio-1 python3 -c "import pika, psycopg, boto3, rucio.client; print('deps ok')"
docker exec -it -e PYTHONWARNINGS=ignore::FutureWarning dev-rucio-1 python3 /opt/rucio_registrar.py
```

It should print `waiting for messages on rucio_registrar (vhost sda)`. Leave it running.

On the host, `rabbitmq`, `postgres` and `s3` don't resolve, and the Rucio client has no `/opt/rucio/etc/rucio.cfg` to read. Always replace the whole file rather than merging edits into an old copy. 

### Backfill what was archived before the queue existed (terminal 2)

```bash
docker exec -it -e PYTHONWARNINGS=ignore::FutureWarning dev-rucio-1 python3 /opt/rucio_registrar.py --backfill
```

It ends with `backfill finished: {'registered': …, 'exists': …, 'skipped': …, 'failed': …}`.

If `archive1` already held files whose `completed` messages went out before `rucio_registrar` existed the queue will never see those messages. Backfill rebuilds the catalogue directly from `sda.files`. It reports already-registered files as `exists` and leaves them alone, so it's safe to rerun at any time, and it may run while the listener in terminal 1 is active.

`skipped` counts permanent errors, such as a missing object. `failed` counts temporary ones; rerun after fixing the cause.

### The live path: new submissions

The listener registers each file as soon as SDA finalises it. Any new submission exercises this path: either the normal upload, ingest, accession and finalize flow, or SDA's integration test.


### Verify

```bash
docker exec dev-rucio-1 rucio did list --short 'sda:*' 2>/dev/null | wc -l       # = "backfillable" from earlier when we count the files in SDA with an accession ID and an archive path (+ any newer files)
sdapsql() { docker exec -i postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -q -U postgres -d sda -v ON_ERROR_STOP=1 "$@"' sh "$@"; }
ID=$(sdapsql -Atc "select stable_id from sda.files where stable_id is not null limit 1")
docker exec dev-rucio-1 rucio replica list file --pfns sda:$ID
docker exec -w /tmp dev-rucio-1 rucio download sda:$ID --rses SDA_ARCHIVE
curl -s -u guest:guest http://localhost:15672/api/queues/sda/rucio_registrar | jq '{messages, consumers}'   # 0 and 1
```

**Why the download is the real proof.** It exercises the whole chain:

- Rucio's server signs a URL with the credentials from `rse-accounts.cfg`.
- The client fetches the file through `sda-s3`'s TLS and the dev CA.
- RGW authorises the read-only key.
- The client verifies the downloaded bytes against the adler32 the registrar recorded.

A queue at `0` messages with `1` consumer means every message was handled, nothing is stuck in retry, and the listener is attached.

### Confirm the S3 key really is read-only

```bash
docker exec dev-rucio-1 python3 -c "
import boto3, botocore
s3 = boto3.client('s3', endpoint_url='http://s3:9000', aws_access_key_id='rucioread',
                  aws_secret_access_key='rucioreadsecret', region_name='us-east-1')
try:
    s3.put_object(Bucket='archive1', Key='rucio-write-test', Body=b'x'); print('WRITE ALLOWED - fix the subuser')
except botocore.exceptions.ClientError as e: print(e.response['Error']['Code'])"   # AccessDenied
```

---

## Resetting and recovering, without touching SDA

In case you need to reset, you can reste only Rcuio. If you down the SDA stack, `down -v` it would destroy REMS, LS-AAI and the archive. Whereas backfill rebuilds Rucio's catalogue from `sda.files` at any time, so Rucio can be reset on its own.

Use this when Rucio's catalogue is wrong or stale, or after `dev-rucio-1` was recreated rather than restarted.

```bash
# terminal 1: stop the listener with Ctrl-C
docker network connect sda-deployment_default dev-rucio-1 2>/dev/null || true        # needed only after a recreate
docker exec -it dev-rucio-1 tools/run_tests.sh -i && bash ~/sda-rucio/rucio-side.sh && docker restart dev-rucio-1
docker exec dev-rucio-1 test -f /opt/rucio_registrar.py || docker cp /home/admin/Git/sda-deployment/sda-rucio/rucio_registrar.py dev-rucio-1:/opt/rucio_registrar.py
docker exec dev-rucio-1 python3 -c "import pika" 2>/dev/null || docker exec dev-rucio-1 python3 -m pip install pika
docker exec -it -e PYTHONWARNINGS=ignore::FutureWarning dev-rucio-1 python3 /opt/rucio_registrar.py            # terminal 1
docker exec -it -e PYTHONWARNINGS=ignore::FutureWarning dev-rucio-1 python3 /opt/rucio_registrar.py --backfill # terminal 2
```

1. **`run_tests.sh -i` wipes Rucio's catalogue.** That includes the `sda` scope and `SDA_ARCHIVE`.
2. **`rucio-side.sh` recreates them.** It also rewrites `rse-accounts.cfg`, which a recreated container loses.
3. **The restart makes the server load the new credentials.**
4. **Backfill rebuilds every replica from SDA's database.**

Messages that arrived during the reset wait in the queue, and the listener picks them up when it starts.

A recreate (rather than a restart) loses everything installed in the container: the network connection, the registrar file, `pika` and `rse-accounts.cfg`. That's why those checks are in the block above. Stopping the listener first also stops it from logging retry errors while `SDA_ARCHIVE` doesn't exist.

### After recreating an SDA service

If `s3`, `rabbitmq` or `postgres` is recreated, for example from a different compose copy, Rucio's identities may be gone. The RGW subuser, the MQ queue and user, and the database role live inside those services. Whether they survive depends on whether the service keeps its state in a volume.

Check:

```bash
docker exec s3 radosgw-admin -c /etc/ceph/ceph.conf -k /etc/ceph/keyring user info --uid=sda 2>/dev/null | jq -r '.subusers[].id'   # sda:rucio
curl -s -u guest:guest http://localhost:15672/api/queues/sda/rucio_registrar | jq '{messages, consumers}'
sdapsql() { docker exec -i postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -X -q -U postgres -d sda -v ON_ERROR_STOP=1 "$@"' sh "$@"; }
sdapsql -Atc "select rolname from pg_roles where rolname = 'rucio'" # rucio
```

If anything is missing, rerun `bash ~/sda-rucio/sda-side.sh` and then rerun `--backfill` for anything archived while the queue was gone.

If `rabbitmq` was recreated, also restart the listener. It reconnects on its own after connection loss, but it exits with an error if its queue or login is missing (`cannot consume from rucio_registrar` or `RabbitMQ refused the login`).

`sda-s3` needs no action: its resolver picks up a new `s3` address within 10 seconds.

---


## Sources

- Rucio 41.5.1: [`etc/docker/dev/docker-compose.yml`](https://github.com/rucio/rucio/blob/41.5.1/etc/docker/dev/docker-compose.yml), [`tools/run_tests.sh`](https://github.com/rucio/rucio/blob/41.5.1/tools/run_tests.sh), [`lib/rucio/core/monitor.py`](https://github.com/rucio/rucio/blob/41.5.1/lib/rucio/core/monitor.py), [`lib/rucio/core/trace.py`](https://github.com/rucio/rucio/blob/41.5.1/lib/rucio/core/trace.py), [`lib/rucio/core/credential.py`](https://github.com/rucio/rucio/blob/41.5.1/lib/rucio/core/credential.py), [`tools/docker_activate_rses.sh`](https://github.com/rucio/rucio/blob/41.5.1/tools/docker_activate_rses.sh), [`etc/docker/test/extra/rucio_default.cfg`](https://github.com/rucio/rucio/blob/41.5.1/etc/docker/test/extra/rucio_default.cfg).
- SDA v4.0.2-hotfix-2: [`postgresql/entrypoint.sh`](https://github.com/neicnordic/sensitive-data-archive/blob/v4.0.2-hotfix-2/postgresql/entrypoint.sh) (superuser `postgres`, scram everywhere), [`rabbitmq/definitions.json`](https://github.com/neicnordic/sensitive-data-archive/blob/v4.0.2-hotfix-2/rabbitmq/definitions.json) (vhost `sda` and the `completed` binding), and [`.github/integration/sda/config.yaml`](https://github.com/neicnordic/sensitive-data-archive/blob/v4.0.2-hotfix-2/.github/integration/sda/config.yaml).

