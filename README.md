# Recovery Lab

A local web app that runs a scenario you type against a fresh PostgreSQL
cluster, one step per click, and shows the table pages, the WAL and the CLOG
after every step. A `crash` line kills the server; the following clicks step
through the **real** crash recovery: PostgreSQL runs in single-user mode under
gdb and pauses after every relevant WAL record it replays.

## Run it

    ./recovery_app/setup.sh                 # once per container (re)build
    cd recovery_app
    python3 manage.py runserver --noreload  # then open http://localhost:8000

`--noreload` matters: the auto-reloader would restart the process and lose the
running cluster.

## Scenario syntax

    -- a caption for the next step
    CREATE TABLE t (id int, note text);     -- no label: one-off session
    T1: BEGIN;                              -- label: session T1 (its own connection)
    T1: INSERT INTO t VALUES (1, 'a');
    T2: BEGIN;
    T2: INSERT INTO t VALUES (2, 'b');      -- T2 stays open
    CHECKPOINT;
    crash                                   -- immediate shutdown, then recovery

## What is real and what is derived

* Table pages "in memory" come from `get_raw_page` (or, during recovery, from
  the buffer pool read by gdb). Pages "on disk" are read straight from the
  table file.
* CLOG "memory" is `pg_xact_status()` (during recovery: the raw bits of the
  in-memory CLOG, read by gdb). CLOG "disk" is read from the `pg_xact` file.
* WAL records come from `pg_walinspect`, which can only read flushed WAL.
  Records that are still only in the WAL buffers are shown as *pending*,
  derived from the page change the step made. When they are flushed, the
  real records replace them. If the server crashes first, they are marked lost.
* Catalog records, running-xacts records etc. are counted, not shown.

## Settings used for the demo cluster

`checkpoint_timeout=1h`, `autovacuum=off`, `bgwriter_lru_maxpages=0`,
`shared_buffers=4MB`, data checksums off. They keep background processes from
writing table pages on their own, so every page change you see is caused by the
step you just ran. The WAL writer keeps its default `wal_writer_delay` (200ms),
and it flushes in the background after a ROLLBACK or an error abort. So WAL
can reach disk without a COMMIT, and the step where that becomes visible can
vary from run to run.
