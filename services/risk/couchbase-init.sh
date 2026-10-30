#!/bin/sh
# One-time Couchbase setup: initialise the single-node cluster, then create the bucket.
# Idempotent: skips steps that are already done; any real error fails the script.
# Usage: CB_HOST=couchbase CB_USER=... CB_PASSWORD=... sh couchbase-init.sh
set -eu
CLI=/opt/couchbase/bin/couchbase-cli
HOST="${CB_HOST:-localhost}"

until curl -sf "http://${HOST}:8091/ui/index.html" > /dev/null; do
  echo "waiting for couchbase REST on ${HOST}:8091"; sleep 2
done

# /pools/default only answers 200 once the node has been initialised.
if curl -sf -u "$CB_USER:$CB_PASSWORD" "http://${HOST}:8091/pools/default" > /dev/null; then
  echo "cluster already initialised"
else
  # data = KV, index + query = SQL++ with GSI. Small RAM quotas: this is a laptop.
  $CLI cluster-init -c "$HOST" --cluster-username "$CB_USER" --cluster-password "$CB_PASSWORD" \
    --services data,index,query --cluster-ramsize 512 --cluster-index-ramsize 256 \
    --index-storage-setting default
fi

if $CLI bucket-list -c "$HOST" -u "$CB_USER" -p "$CB_PASSWORD" | grep -qx ledgerlens; then
  echo "bucket already exists"
else
  # couchstore: Community Edition doesn't support the Magma storage engine.
  $CLI bucket-create -c "$HOST" -u "$CB_USER" -p "$CB_PASSWORD" --bucket ledgerlens \
    --bucket-type couchbase --storage-backend couchstore --bucket-ramsize 256 --bucket-replica 0 --wait
fi
# The GSI index for the top-risky report is created by the risk service at startup (CREATE INDEX IF NOT EXISTS).
