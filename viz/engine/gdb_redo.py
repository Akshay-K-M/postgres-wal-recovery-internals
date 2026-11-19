"""Runs INSIDE gdb (gdb -batch -x gdb_redo.py --args postgres --single ...).

Pauses crash recovery for real and reports what is in memory:

  start      PerformWalRecovery() entered, nothing replayed yet
  applied    one WAL record was just applied by its redo routine
  redo_done  the redo loop ended (next WAL position held no valid record)
  eor        the end-of-recovery checkpoint finished
  exit       the single-user backend exited

After every event it blocks until the app writes a line to the control FIFO.
Records that don't touch the user's tables or transactions are replayed
without stopping (they are only counted).

Inputs (environment):
  VIZ_EVENTS  file to append JSON events to
  VIZ_CTRL    FIFO to read "next" commands from
  VIZ_RELS    JSON list of {"relnumber", "nblocks"} for the user tables
  VIZ_XIDS    JSON list of interesting transaction ids
  VIZ_SRC     PostgreSQL source tree (to locate breakpoint lines)
  VIZ_PGARGS  arguments for postgres, e.g. "--single -D /path/data postgres"
"""
import base64
import json
import os
import struct

import gdb

EVENTS = os.environ["VIZ_EVENTS"]
CTRL = os.environ["VIZ_CTRL"]
RELS = {int(r["relnumber"]): int(r["nblocks"]) for r in json.loads(os.environ["VIZ_RELS"])}
XIDS = [int(x) for x in json.loads(os.environ["VIZ_XIDS"])]
SRC = os.environ["VIZ_SRC"]
MAXPAGES = 4
CLOG_XACTS_PER_PAGE = 8192 * 4

skipped = 0
eor_armed = False


def emit(obj):
    with open(EVENTS, "a") as f:
        f.write(json.dumps(obj) + "\n")


def wait_next():
    with open(CTRL) as f:
        f.readline()


def val(expr):
    return gdb.parse_and_eval(expr)


# ---------------------------------------------------------------- memory readers
_layout = {}


def _buf_layout():
    if not _layout:
        _layout["size"] = int(val("sizeof(BufferDescPadded)"))
        _layout["tag"] = int(val("(long) &((BufferDesc *) 0)->tag"))
    return _layout


def read_pages():
    """User-table pages currently in shared buffers: {relnumber: {blk: base64}}."""
    lay = _buf_layout()
    nbuf = int(val("NBuffers"))
    descs = int(val("(long) BufferDescriptors"))
    blocks = int(val("(long) BufferBlocks"))
    inf = gdb.selected_inferior()
    raw = bytes(inf.read_memory(descs, nbuf * lay["size"]))
    out = {}
    for i in range(nbuf):
        spc, db, relnum, fork, blk = struct.unpack_from("<IIIiI", raw, i * lay["size"] + lay["tag"])
        if relnum in RELS and fork == 0 and blk < MAXPAGES:
            page = bytes(inf.read_memory(blocks + i * 8192, 8192))
            out.setdefault(str(relnum), {})[str(blk)] = base64.b64encode(page).decode()
    return out


def read_procarray():
    """Read the actual new ProcArray while the single-user backend is paused."""
    try:
        array = val("'procarray.c'::procArray")
        if not int(array):
            return {"source": "uninitialized", "entries": []}
        procs = val("ProcGlobal->allProcs")
        entries = []
        for i in range(int(array["numProcs"])):
            procno = int(array["pgprocnos"][i])
            proc = procs[procno]
            entries.append({"pid": int(proc["pid"]), "procno": procno,
                            "xid": int(proc["xid"]) or None,
                            "xmin": int(proc["xmin"]) or None,
                            "app": "single-user recovery", "state": "paused in recovery"})
        return {"source": "gdb", "entries": entries}
    except Exception as e:
        # An unavailable optional panel must not prevent WAL recovery stepping.
        return {"source": "unavailable", "entries": [], "error": str(e)}


def read_clog():
    """Raw 2-bit status of each interesting xid in the in-memory CLOG (None = page not loaded)."""
    shared = val("'clog.c'::XactSlruDesc.shared")
    nslots = int(shared["num_slots"])
    inf = gdb.selected_inferior()
    res = {}
    for xid in XIDS:
        pageno = xid // CLOG_XACTS_PER_PAGE
        res[str(xid)] = None
        for s in range(nslots):
            if int(shared["page_status"][s]) != 0 and int(shared["page_number"][s]) == pageno:
                buf = int(shared["page_buffer"][s])
                idx = xid % CLOG_XACTS_PER_PAGE
                byte = bytes(inf.read_memory(buf + idx // 4, 1))[0]
                res[str(xid)] = (byte >> ((idx % 4) * 2)) & 3
                break
    return res


def record_info():
    r = val("xlogreader")
    rec = r["record"]
    hdr = rec["header"]
    rmid = int(hdr["xl_rmid"])
    info = int(hdr["xl_info"])
    ident = val("RmgrTable[%d].rm_identify(%d)" % (rmid, info))
    blocks = []
    for b in range(int(rec["max_block_id"]) + 1):
        blk = rec["blocks"][b]
        if not bool(blk["in_use"]):
            continue
        blocks.append({"relnumber": int(blk["rlocator"]["relNumber"]), "fork": int(blk["forknum"]),
                       "blk": int(blk["blkno"]), "image": bool(blk["has_image"]),
                       "apply_image": bool(blk["apply_image"])})
    return {
        "start": int(r["ReadRecPtr"]), "end": int(r["EndRecPtr"]),
        "rmgr": val("RmgrTable[%d].rm_name" % rmid).string(),
        "type": ident.string() if int(ident) else "UNKNOWN",
        "xid": int(hdr["xl_xid"]), "blocks": blocks,
    }


def relevant(rec):
    if any(b["relnumber"] in RELS for b in rec["blocks"]):
        return True
    if rec["rmgr"] == "Transaction" and rec["xid"] in XIDS:
        return True
    # an online checkpoint is CHECKPOINT_REDO ... CHECKPOINT_ONLINE: stop once, at the REDO record
    if rec["rmgr"] == "XLOG" and rec["type"] in ("CHECKPOINT_REDO", "CHECKPOINT_SHUTDOWN", "END_OF_RECOVERY"):
        return True
    return False


# ---------------------------------------------------------------- breakpoints
def find_line(path, needle, after=0):
    with open(path) as f:
        for n, line in enumerate(f, start=1):
            if n > after and needle in line:
                return n
    raise RuntimeError("cannot find %r in %s" % (needle, path))


class Stop(gdb.Breakpoint):
    def __init__(self, spec, handler):
        super().__init__(spec, internal=True)
        self.handler = handler

    def stop(self):
        try:
            self.handler()
        except Exception as e:          # never leave the app waiting forever
            emit({"event": "error", "message": "%s: %s" % (type(e).__name__, e)})
            wait_next()
        return False


def on_start():
    redo = int(val("'xlog.c'::ControlFile->checkPointCopy.redo"))
    ckpt = int(val("'xlog.c'::ControlFile->checkPoint"))
    state = int(val("'xlog.c'::ControlFile->state"))
    emit({"event": "start", "redo": redo, "checkpoint": ckpt, "dbstate": state,
          "pages": read_pages(), "clog": read_clog(), "procarray": read_procarray()})
    wait_next()


def on_applied():
    global skipped
    rec = record_info()
    if not relevant(rec):
        skipped += 1
        return
    emit({"event": "applied", "record": rec, "skipped_before": skipped,
          "pages": read_pages(), "clog": read_clog(), "procarray": read_procarray()})
    skipped = 0
    wait_next()


def on_redo_done():
    global eor_armed
    eor_armed = True
    emit({"event": "redo_done", "last": int(val("xlogreader->ReadRecPtr")),
          "skipped_before": skipped, "pages": read_pages(), "clog": read_clog(), "procarray": read_procarray()})
    wait_next()


def on_eor():
    global eor_armed
    if not eor_armed:
        return
    eor_armed = False
    emit({"event": "eor", "redo": int(val("'xlog.c'::ControlFile->checkPointCopy.redo")),
          "checkpoint": int(val("'xlog.c'::ControlFile->checkPoint")),
          "pages": read_pages(), "clog": read_clog(), "procarray": read_procarray()})
    wait_next()


def on_exit(event):
    code = getattr(event, "exit_code", None)
    emit({"event": "exit", "code": code})


rec_c = os.path.join(SRC, "src/backend/access/transam/xlogrecovery.c")
xlog_c = os.path.join(SRC, "src/backend/access/transam/xlog.c")
apply_line = find_line(rec_c, "ApplyWalRecord(xlogreader, record, &replayTLI);")
done_line = find_line(rec_c, "RmgrCleanup();", after=apply_line)
eor_line = find_line(xlog_c, "return promoted;")

gdb.execute("set pagination off")
gdb.execute("set confirm off")
gdb.execute("set breakpoint pending on")
gdb.execute("set startup-with-shell on")
Stop("PerformWalRecovery", on_start)
Stop("xlogrecovery.c:%d" % (apply_line + 1), on_applied)   # first statement after the call
Stop("xlogrecovery.c:%d" % done_line, on_redo_done)
Stop("xlog.c:%d" % eor_line, on_eor)
gdb.events.exited.connect(on_exit)
gdb.execute("set args " + os.environ["VIZ_PGARGS"] + " < /dev/null > %s 2>&1" % os.path.join(os.path.dirname(EVENTS), "single-user.log"))
gdb.execute("run")
