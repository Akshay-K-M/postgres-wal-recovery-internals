# Week 1 (A): Runtime abort path — notes

PostgreSQL 20devel (master), built with
`--enable-debug --enable-cassert CFLAGS="-O0 -g3" --prefix=$HOME/pgsql`.
Cluster: `~/pgdata`, database `demo`, table `accounts(id int, balance int)`.

## Environment setup / gotchas

| Issue | Fix |
|---|---|
| `gdb -p` fails: `ptrace: Inappropriate ioctl for device` | WSL2 kernel has `ptrace_scope=1`. In Windows PowerShell: `wsl -u root sh -c "echo 0 > /proc/sys/kernel/yama/ptrace_scope"`. Resets on WSL restart. |
| Pasting into gdb in the VS Code terminal merges lines / adds `\r` | Type gdb commands by hand, or `source recovery_app/initial_experiments/abort.gdb`. Pasting into psql is fine. |
| `pageinspect` missing | `make -C contrib/pageinspect install` |
| Server not running after container restart | `~/pgsql/bin/pg_ctl -D ~/pgdata -l ~/pgdata/logfile start` |
| `pg_ctl`/`psql: command not found` after container **rebuild** (`~` is wiped) | `cd /workspaces/postgresql && make install && make -C contrib/pageinspect install`, then `export PATH=$HOME/pgsql/bin:$PATH; initdb -D ~/pgdata; pg_ctl -D ~/pgdata -l ~/pgdata/logfile start; createdb demo` |

## How to attach

1. Terminal 1: `~/pgsql/bin/psql demo` → `SELECT pg_backend_pid();`
2. Terminal 2: `gdb -p <pid>` → `source recovery_app/initial_experiments/abort.gdb` → `continue`

## Call path

```
ROLLBACK                                  error (ereport ERROR → longjmp)
  UserAbortTransactionBlock                 PostgresMain error handler (postgres.c:4629)
    INPROGRESS → ABORT_PENDING              AbortCurrentTransaction
  CommitTransactionCommandInternal            AbortCurrentTransactionInternal (xact.c:3606)
    (xact.c:3353)                               INPROGRESS → ABORT
          \                                   /
           AbortTransaction (xact.c:2870)
             RecordTransactionAbort (xact.c:1805)
               XactLogAbortRecord → XLogInsert(RM_XACT_ID, XLOG_XACT_ABORT)   [WAL]
               XLogSetAsyncXactLSN   (abort record is NOT flushed synchronously)
               TransactionIdAbortTree (transam.c:270)
                 TransactionIdSetTreeStatus → SetPageStatus → SetPageStatusInternal
                   TransactionIdSetStatusBit (clog.c:669)  status=2 ABORTED   [CLOG]
             ProcArrayEndTransaction (xid no longer "running")
           CleanupTransaction (xact.c:3076)
             ROLLBACK case: immediately after AbortTransaction (xact.c:3353)
             error case:    only at the later ROLLBACK (ABORT → ABORT_END, xact.c:3335)
```

Crash-recovery link (B): `xact_redo_abort` (xact.c:6370) replays the same
ABORT record and calls the same `TransactionIdAbortTree` (xact.c:6386).

## Observed run (2026-11-09)

### Run 1 — explicit ROLLBACK (xid 678)

```sql
BEGIN; INSERT INTO accounts VALUES (3,300); SELECT pg_current_xact_id(); ROLLBACK;
```

| # | Breakpoint | Command | Observed |
|---|---|---|---|
| 1 | UserAbortTransactionBlock | `p CurrentTransactionState->blockState` | `TBLOCK_INPROGRESS` |
| 2 | AbortTransaction | `p ...->blockState`, `bt 3` | `TBLOCK_ABORT_PENDING`; caller `CommitTransactionCommandInternal` xact.c:3353 |
| 3 | RecordTransactionAbort | `p ...->fullTransactionId.value` | `678` |
| 4 | XactLogAbortRecord | `finish`, `p/x $` | end LSN `0x1bf6648` = `0/1BF6648` |
| 5 | TransactionIdAbortTree | `p xid`, `p nxids` | `678`, `0` |
| 6 | TransactionIdSetStatusBit | `p status`, `bt 8` | `2` (ABORTED), `lsn=0` |
| 7 | CleanupTransaction | `p ...->state` | `TRANS_ABORT` |

### Run 2 — divide by zero (xid 679)

```sql
BEGIN; INSERT INTO accounts VALUES (4,400); SELECT pg_current_xact_id(); SELECT 1/0;
-- ERROR: division by zero
ROLLBACK;
```

| # | Breakpoint | Observed |
|---|---|---|
| 1 | int4div | called from `evaluate_function` (clauses.c) — 1/0 is constant-folded by the **planner** |
| 2 | AbortCurrentTransaction | `TBLOCK_INPROGRESS`, caller `PostgresMain` postgres.c:4629 |
| 3 | AbortTransaction | caller `AbortCurrentTransactionInternal` xact.c:3606 |
| 4–6 | RecordTransactionAbort → XactLogAbortRecord → AbortTree → SetStatusBit | xid `679`, end LSN `0/1BF6A28`, status `2` |
| — | CleanupTransaction | **not hit** at the error; xact left in `TBLOCK_ABORT` |
| 7 | (later ROLLBACK) UserAbortTransactionBlock | `TBLOCK_ABORT` → `TBLOCK_ABORT_END` |
| 8 | CleanupTransaction | caller `CommitTransactionCommandInternal`; no new WAL, no CLOG change |

## Verification after the runs

### WAL (`pg_waldump -p ~/pgdata/pg_wal -s 0/1B00000`, filtered)

```
Heap        tx: 678  lsn: 0/01BF63E8  INSERT off: 4  rel 1663/16384/16385 blk 0 FPW
Transaction tx: 678  lsn: 0/01BF6620  ABORT 2026-11-09 16:08:20 UTC
Heap        tx: 679  lsn: 0/01BF67A0  INSERT off: 5  rel 1663/16384/16385 blk 0 FPW
Transaction tx: 679  lsn: 0/01BF6A00  ABORT 2026-11-09 16:18:03 UTC
```

- ABORT record starts at `0/1BF6620`; 34 bytes, MAXALIGNed to 40 → ends at
  `0/1BF6648` — exactly what `XLogInsert` returned in gdb.
- Online checkpoints ran between the INSERTs (timed checkpoints while we sat in gdb),
  so each first-touch INSERT after a checkpoint carries a **full-page image (FPW)**.
- A later `FPI_FOR_HINT` record appears after reading the table: data checksums are on,
  so setting hint bits on the page is WAL-logged.

### CLOG

```sql
SELECT pg_xact_status('678'), pg_xact_status('679');   -- aborted | aborted
```

### Heap (pageinspect)

```
 lp | t_xmin | hint_xmin_invalid | hint_xmin_committed
  1 |   665  | f | t      -- row (1,100), committed
  2 |   665  | f | t      -- row (2,200), committed
  3 |   677  | t | f      -- leftover from an interrupted first attempt (aborted)
  4 |   678  | t | f      -- row (3,300), run 1
  5 |   679  | t | f      -- row (4,400), run 2
```

`SELECT * FROM accounts` returns only rows 1 and 2. The aborted tuples are still on the
page; readers look up `xmin` in CLOG, see ABORTED, skip them, and cache that as the
`HEAP_XMIN_INVALID` hint bit. No undo was ever performed — VACUUM reclaims the space later.

## Talking points

- Abort = WAL ABORT record + 2 CLOG bits. Nothing in the heap is undone.
- Error-driven abort is two-phase: real abort at the error, `CleanupTransaction` at `ROLLBACK`.
- Abort record is flushed asynchronously: if it is lost in a crash, the xid simply never
  committed — same outcome. Hence redo-only recovery with no undo pass.
- A transaction with no xid (read-only) writes no abort record at all
  (`RecordTransactionAbort` returns early).
