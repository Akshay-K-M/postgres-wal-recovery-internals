#!/bin/sh
# E: Crash with an open transaction that spans a checkpoint.
set -eu
export PATH=$HOME/pgsql/bin:$PATH
export LC_ALL=C

# Run as a normal (non-root) user. A fresh cluster and a private socket keep
# this demo separate from any existing server. No TCP listener is opened.
lab=$(mktemp -d /tmp/pg-recovery-demo.XXXXXX)
data="$lab/data"
echo "Demo files: $lab"
initdb -A trust -U postgres -D "$data" > "$lab/initdb.log"

# A long timeout avoids timed checkpoints during the demo. Synchronous
# commits flush WAL before returning control to the client.
pg_ctl -D "$data" -l "$lab/server.log" \
    -o "-h '' -k $lab -p 55432 -c checkpoint_timeout=1h -c synchronous_commit=on" start

# Point psql and any second psql session at this lab cluster.
# The SQL blocks below use -X to ignore local psql settings and
# ON_ERROR_STOP so a failed SQL statement stops the demo.
export PGHOST="$lab" PGPORT=55432 PGUSER=postgres PGDATABASE=postgres

# Find the table file while the server is still available. The path is
# relative to the data directory and identifies this table's main file.
psql -X -v ON_ERROR_STOP=1 -c 'CREATE TABLE recovery_demo (id int, note text);'
relation=$(psql -XAt -v ON_ERROR_STOP=1 -c "SELECT pg_relation_filepath('recovery_demo')")
heap_file="$data/$relation"
echo "Table file: $heap_file"

# Search for each row's unique text while PostgreSQL is stopped. Finding
# bytes in the file says nothing about whether the transaction committed.
# The OS can serve this read from its cache, even after PostgreSQL stops.
check_heap() {
    stage=$1
    shift
    for note do
        if [ -f "$heap_file" ] && grep -aqF "$note" "$heap_file"; then
            echo "$stage: '$note' found in the table file"
        else
            echo "$stage: '$note' absent from the table file"
        fi
    done
}

# The first session inserts row 1 and leaves its transaction open. The
# first \! runs CHECKPOINT in a second session. That can flush the page
# containing row 1 even though its transaction has not committed. Then the
# first session inserts row 2. The second \! commits row 99 to flush the
# newer WAL and stops the server while the first session is still open.
psql -X -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
SELECT pg_current_xact_id() AS target_xid;
INSERT INTO recovery_demo VALUES (1, 'open before checkpoint');
\! psql -X -v ON_ERROR_STOP=1 -c "CHECKPOINT"
INSERT INTO recovery_demo VALUES (2, 'open after checkpoint');
\! psql -X -v ON_ERROR_STOP=1 -c "INSERT INTO recovery_demo VALUES (99, 'committed WAL flush marker')" && pg_ctl -D "$PGHOST/data" stop -m immediate
SQL

# Inspect the file after the crash and before any restart can replay WAL.
check_heap 'After crash, before redo' 'open before checkpoint' 'open after checkpoint' 'committed WAL flush marker'

# Expect only row 99. The checkpoint covers row 1's page change, and row 2's
# INSERT is in the replay range. Neither row is visible because the
# transaction that inserted them never committed.
# Single-user mode runs recovery and the verification queries in one
# process. The first restart replays WAL and logs its redo positions.
# In the Seq Scan output, shared hit means the page was in PostgreSQL's
# buffers. A shared read can come from the OS cache or from storage.
postgres --single -D "$data" postgres > "$lab/recovery.log" 2>&1 <<'SQL'
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM recovery_demo;
SELECT * FROM recovery_demo;
SQL
cat "$lab/recovery.log"

# Compare after recovery and the final clean shutdown.
check_heap 'After recovery' 'open before checkpoint' 'open after checkpoint' 'committed WAL flush marker'
