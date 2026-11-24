#!/bin/bash
export LC_ALL=C
export PATH=/usr/local/pgsql/bin:$PATH

echo "--- 1. Setup: Clean state and Base Backup ---"
pg_ctl -D pg_data stop -m immediate >/dev/null 2>&1
rm -rf pg_data wal_archive backup_data
mkdir -p wal_archive backup_data

initdb -D pg_data >/dev/null 2>&1
echo "archive_mode = on" >> pg_data/postgresql.conf
echo "archive_command = 'cp %p /workspaces/postgresql/wal_archive/%f'" >> pg_data/postgresql.conf
pg_ctl -D pg_data -l logfile start >/dev/null 2>&1
sleep 2

pg_basebackup -D backup_data -Fp -Xs
echo "Base backup taken successfully."

echo -e "\n--- 2. Write Critical Data ---"
psql postgres -c "CREATE TABLE pitr_test (msg text);"
psql postgres -c "INSERT INTO pitr_test VALUES ('Critical data we MUST save');"

echo -e "\n--- 3. Create Restore Point & Archive WAL ---"
# Inject a named bookmark into the transaction log
psql postgres -c "SELECT pg_create_restore_point('safe_data');"
# Force the active WAL segment to close and copy to wal_archive
psql postgres -c "SELECT pg_switch_wal();" 
sleep 2

echo -e "\n--- 4. The Disaster (Bad Data & Media Failure) ---"
psql postgres -c "INSERT INTO pitr_test VALUES ('Bad data we want to undo');"
psql postgres -c "DROP TABLE pitr_test;"
echo "Table dropped! Simulating hard drive failure..."

# Use native immediate crash to simulate power loss and release ports safely
pg_ctl -D pg_data stop -m immediate >/dev/null 2>&1
rm -rf pg_data
echo "pg_data completely destroyed!"

echo -e "\n--- 5. PITR Restore Process ---"
cp -r backup_data pg_data
# Fix the folder permissions so Postgres accepts it
chmod 700 pg_data
touch pg_data/recovery.signal

echo "restore_command = 'cp /workspaces/postgresql/wal_archive/%f %p'" >> pg_data/postgresql.conf
# Recover exactly up to the bookmark we created
echo "recovery_target_name = 'safe_data'" >> pg_data/postgresql.conf
echo "recovery_target_action = 'promote'" >> pg_data/postgresql.conf

echo -e "\n--- 6. Booting Server for Recovery ---"
pg_ctl -D pg_data -l logfile start
sleep 3

echo -e "\n--- 7. Verification: Did we travel back in time? ---"
psql postgres -c "SELECT * FROM pitr_test;"