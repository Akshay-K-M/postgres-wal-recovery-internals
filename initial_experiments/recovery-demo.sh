#!/bin/sh
# A: Checkpoint before a committed insert.
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

# CHECKPOINT waits for a new recovery point. The next INSERT commits
# automatically, so its INSERT and COMMIT WAL follow that point.
psql -X -v ON_ERROR_STOP=1 <<'SQL'
CREATE TABLE recovery_demo (id int, note text);
CHECKPOINT;
INSERT INTO recovery_demo VALUES (1, 'committed after checkpoint');
SQL

# Ask PostgreSQL for the exact heap file and the WAL range to inspect.
# The redo LSN is where recovery starts. The flush LSN bounds the WAL that
# is already durable, including the commit for this insert.
relation=$(psql -XAtc "SELECT pg_relation_filepath('recovery_demo')")
redo=$(psql -XAtc 'SELECT redo_lsn FROM pg_control_checkpoint()')
wal_end=$(psql -XAtc 'SELECT pg_current_wal_flush_lsn()')
heap_file="$data/$relation"

# Search only the table file for the unique text we inserted. A match
# means those bytes are visible through the filesystem, which can serve
# them from the OS cache. This is a PostgreSQL crash, not a power loss.
check_heap() {
    if [ -f "$heap_file" ] && grep -aqF 'committed after checkpoint' "$heap_file"; then
        echo "$1: row text found in the table file"
    else
        echo "$1: row text absent from the table file"
    fi
}

# Immediate shutdown skips the normal shutdown checkpoint to simulate a crash.
pg_ctl -D "$data" stop -m immediate
echo "Table file: $heap_file"
check_heap 'After crash, before redo'

# WAL identifies the insert separately from the heap file check. This table
# is in pg_default (OID 1663), so base/DBOID/RELFILE maps to 1663/DBOID/RELFILE.
# INSERT+INIT tells redo to rebuild the page and insert the tuple from WAL,
# even if the previous copy of the page already contained the row.
echo "Table WAL from the redo point ($redo):"
pg_waldump -p "$data/pg_wal" -s "$redo" -e "$wal_end" \
    -r Heap -R "1663/${relation#base/}" > "$lab/wal.log"
cat "$lab/wal.log"

# Expect row 1. Recovery can replay its INSERT and COMMIT.
# Single-user mode runs recovery and the verification queries in one
# process. The first restart replays WAL and logs its redo positions.
# In the Seq Scan output, shared hit means the page was in PostgreSQL's
# buffers. A shared read can come from the OS cache or from storage.
postgres --single -D "$data" postgres > "$lab/recovery.log" 2>&1 <<'SQL'
EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM recovery_demo;
SELECT * FROM recovery_demo;
SQL
cat "$lab/recovery.log"
check_heap 'After recovery'
