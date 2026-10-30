# Couchbase CE for the Render demo (private service). Runs the idempotent init script
# (cluster + bucket) in the background while the server starts. Build context: repo root.
FROM couchbase/server:community-8.0.0
COPY services/risk/couchbase-init.sh /init.sh
CMD ["/bin/sh", "-c", "(CB_USER=\"$COUCHBASE_USER\" CB_PASSWORD=\"$COUCHBASE_PASSWORD\" sh /init.sh &) ; exec /entrypoint.sh couchbase-server"]
