#!/bin/bash

# Set environment for the postgres user
export LC_ALL=C
export PATH=/usr/local/pgsql/bin:$PATH

# echo "--- 1. Setting up deep logging ---"
# echo "log_checkpoints = on" >> pg_data/postgresql.conf
# echo "log_min_messages = DEBUG1" >> pg_data/postgresql.conf
# pg_ctl -D pg_data reload

echo "--- 2. Flooding WAL with unwritten dirty pages ---"
psql postgres -c "CREATE TABLE IF NOT EXISTS crash_test (id serial); INSERT INTO crash_test SELECT generate_series(1, 100000);"

echo "--- 3. Crashing the server (kill -9) ---"
read PG_PID < pg_data/postmaster.pid
kill -9 $PG_PID
echo "Server abruptly killed! Waiting 2 seconds..."
sleep 2

echo "--- 4. Booting server to trigger REDO recovery ---"
pg_ctl -D pg_data -l logfile start
sleep 2

echo "--- 5. Recovery Telemetry (End of Log) ---"
grep -E "automatic recovery in progress|redo starts at|redo done at" logfile | tail -n 5