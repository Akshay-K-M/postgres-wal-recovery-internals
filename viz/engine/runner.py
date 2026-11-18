"""Execute a scenario one step at a time and describe the database after each step.

A Run owns a throw-away cluster. Every call to next() performs exactly one
action (one SQL statement, the crash, or one stop of the real recovery under
gdb), takes a snapshot of memory + disk + WAL + CLOG, and appends a "view"
(a plain dict the browser renders) to the history.
"""
import base64
import os
import re

from . import heappage, scenario, narrate
from .pgcluster import Cluster, Session
from .redo import RedoUnderGdb

MAXPAGES = 4

# enum DBState in src/include/catalog/pg_control.h
DBSTATE = {0: "starting up", 1: "shut down", 2: "shut down in recovery", 3: "shutting down",
           4: "in crash recovery", 5: "in archive recovery", 6: "in production"}


def L(n):
    return None if n is None else "%X/%08X" % (n >> 32, n & 0xFFFFFFFF)


def lsn(s):
    if not s:
        return None
    hi, lo = s.split("/")
    return (int(hi, 16) << 32) | int(lo, 16)


META_SQL = r"""
WITH rels AS (
  SELECT c.oid, c.relname, c.relfilenode, c.reltablespace, pg_relation_filepath(c.oid) AS path,
         (pg_relation_size(c.oid) / 8192)::int AS nblocks
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public' AND c.relkind = 'r'
)
SELECT json_build_object(
  'insert', pg_current_wal_insert_lsn()::text,
  'flush',  pg_current_wal_flush_lsn()::text,
  'redo',   (SELECT redo_lsn::text FROM pg_control_checkpoint()),
  'ckpt',   (SELECT checkpoint_lsn::text FROM pg_control_checkpoint()),
  'rels', coalesce((SELECT json_agg(json_build_object(
      'name', relname, 'relfilenode', relfilenode, 'path', path, 'nblocks', nblocks,
      'cols', (SELECT json_agg(json_build_object('name', a.attname, 'typname', t.typname,
                   'len', a.attlen, 'align', a.attalign, 'dropped', a.attisdropped) ORDER BY a.attnum)
               FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
               WHERE a.attrelid = rels.oid AND a.attnum > 0),
      'pages', (SELECT json_agg(encode(get_raw_page(rels.oid::regclass::text, b), 'base64') ORDER BY b)
                FROM generate_series(0, least(nblocks, %d) - 1) b)
    ) ORDER BY relname) FROM rels), '[]'::json),
  'activity', (SELECT json_agg(json_build_object('app', application_name, 'xid', backend_xid::text::bigint))
               FROM pg_stat_activity WHERE application_name LIKE 'viz:%%')
);
""" % MAXPAGES

WAL_SQL = """
SELECT coalesce(json_agg(json_build_object('start', start_lsn::text, 'end', end_lsn::text,
         'xid', xid::text::bigint, 'rmgr', resource_manager, 'type', record_type,
         'desc', description, 'blk', block_ref, 'fpi', fpi_length) ORDER BY start_lsn), '[]'::json)
FROM pg_get_wal_records_info(:'from', :'to');
"""

BLKREF_RE = re.compile(r"blkref #\d+: rel (\d+)/(\d+)/(\d+)(?: fork (\w+))? blk (\d+)( \(FPW\))?")


class Run:
    def __init__(self, text):
        self.text = text
        self.steps = scenario.parse(text)
        self.pos = 0
        self.cluster = Cluster()
        self.sessions = {}
        self.auto_n = 0
        self.names = {}            # xid -> session label (None = unlabeled)
        self.labels = []           # labels in order of appearance (colour index)
        self.tables = {}           # name -> meta
        self.relnames = {}         # relfilenode -> table name (kept forever)
        self.wal = []              # real WAL records (normalized)
        self.wal_end = None
        self.hidden = 0
        self.pending = []          # derived records for unflushed WAL (by step)
        self.lost = []             # pending records lost in a crash
        self.abort_xids = set()
        self.orphans = set()       # xids that were in progress at a crash
        self.cache = {}            # (table, blk, lp, xmin) -> values
        self.eor_redos = set()
        self.mode = "up"
        self.redo = None
        self.rctx = {}
        self.history = []
        self.state = None
        self.finished = False

    # ================================================================= setup
    def start(self):
        c = self.cluster
        c.init()
        c.start()
        c.query("CREATE EXTENSION pageinspect; CREATE EXTENSION pg_walinspect; CHECKPOINT;")
        self.wal_end = lsn(c.query("SELECT redo_lsn FROM pg_control_checkpoint()"))
        state = self.snapshot_up()
        self.push(state, {"kind": "init", "title": "A fresh database",
                          "sql": "initdb  +  pg_ctl start  +  CHECKPOINT",
                          "output": "Cluster: %s" % c.data})

    def close(self):
        for s in self.sessions.values():
            s.kill()
        if self.redo:
            self.redo.kill()
        self.cluster.destroy()

    # ================================================================= stepping
    def next(self):
        if self.mode == "down":
            return self._start_recovery()
        if self.mode == "recovering":
            return self._recovery_step()
        if self.pos >= len(self.steps):
            self.finished = True
            return None
        step = self.steps[self.pos]
        self.pos += 1
        if step["kind"] == "crash":
            return self._crash(step)
        return self._sql(step)

    def has_next(self):
        return self.mode != "up" or self.pos < len(self.steps)

    # ---------------------------------------------------------------- SQL step
    def _session_for(self, label):
        notes = []
        if label is None:
            self.auto_n += 1
            return Session(self.cluster, "auto%d" % self.auto_n), notes
        s = self.sessions.get(label)
        if s is not None and s.dead:
            notes.append("New connection for %s: its previous connection died in the crash." % label)
            s = None
        if s is None:
            s = Session(self.cluster, label)
            self.sessions[label] = s
        if label not in self.labels:
            self.labels.append(label)
        return s, notes

    def _sql(self, step):
        label, sql = step["label"], step["sql"]
        late = []
        for name, s in list(self.sessions.items()):
            out = s.drain_late()
            if out is not None:
                late.append("%s: an earlier statement finished: %s" % (name, " ".join(out[-2:])))
        sess, notes = self._session_for(label)
        out, done = sess.run(sql)
        if label is None:
            if done:
                sess.close()
            else:
                self.sessions[sess.label] = sess       # keep a blocked one-off session alive
        before = self.state
        state = self.snapshot_up(step_label=label if label else None)
        error = any(l.startswith("ERROR:") for l in out)
        self._derive_pending(before, state, step, error)
        self.push(state, {"kind": "sql", "title": (label + ": " if label else "") + _first_line(sql),
                          "label": label, "sql": sql, "line": step["line"], "caption": step["caption"],
                          "output": "\n".join(out), "error": error, "blocked": not done,
                          "notes": notes + late})

    # ---------------------------------------------------------------- crash
    def _crash(self, step):
        before = self.state
        self.cluster.crash()
        for s in self.sessions.values():
            s.kill()
        # what was still in progress can never commit now
        for xid, c in before["clog"].items():
            if c.get("mem") == "in progress":
                self.orphans.add(xid)
        # WAL that never reached disk is gone
        for g in self.pending:
            for it in g["items"]:
                self.lost.append(dict(it, src="lost", txname=self.names.get(it.get("xid"))))
        self.pending = []
        ctl = self.cluster.controldata()
        state = self.snapshot_down(before, ctl)
        self.mode = "down"
        self.push(state, {"kind": "crash", "title": "CRASH  (pg_ctl stop -m immediate)", "line": step["line"],
                          "caption": step["caption"], "sql": "crash",
                          "output": "pg_control: Database cluster state = %s\npg_control: Latest checkpoint's REDO location = %s"
                                    % (ctl.get("Database cluster state"), ctl.get("Latest checkpoint's REDO location"))})

    # ---------------------------------------------------------------- recovery
    def _start_recovery(self):
        rels = [{"relnumber": t["relfilenode"], "nblocks": t["nblocks"]} for t in self.tables.values()]
        xids = sorted(set(self.state["clog"].keys()))
        self.redo = RedoUnderGdb(self.cluster, rels, xids)
        self.mode = "recovering"
        ev = self.redo.next_event()
        if ev["event"] == "error" or ev.get("unexpected"):
            return self._recovery_failed(ev)
        self.rctx = {"redo": ev.get("redo"), "applied": None, "done": False}
        state = self.snapshot_recovering(ev)
        self.push(state, {"kind": "restart", "title": "Restart: crash recovery begins",
                          "sql": "postgres --single   (under gdb: the startup code is paused)",
                          "output": "pg_control state = %s (not 'shut down')\nREDO pointer = %s\nbreakpoint: PerformWalRecovery()"
                                    % (DBSTATE.get(ev.get("dbstate"), ev.get("dbstate")), L(ev.get("redo")))})

    def _recovery_step(self):
        self.redo.resume()
        ev = self.redo.next_event()
        kind = ev["event"]
        if kind == "error" or ev.get("unexpected"):
            return self._recovery_failed(ev)
        if kind == "applied":
            rec = ev["record"]
            self.rctx["applied"] = rec["start"]
            if not any(w["start"] == rec["start"] for w in self.wal):
                self._add_wal_from_gdb(rec)
            state = self.snapshot_recovering(ev)
            w = self._wal_at(rec["start"])
            self.push(state, {"kind": "redo", "title": "Redo: " + narrate.wal_text(w, self) if w else "Redo",
                              "sql": "breakpoint after ApplyWalRecord()  ·  record at %s" % L(rec["start"]),
                              "output": "%s %s  xid=%s  %s" % (rec["rmgr"], rec["type"], rec["xid"],
                                                               "  ".join("rel %d blk %d%s" % (b["relnumber"], b["blk"], " (full-page image)" if b["image"] else "") for b in rec["blocks"])),
                              "record": w, "skipped": ev.get("skipped_before", 0)})
        elif kind == "redo_done":
            self.rctx["done"] = True
            state = self.snapshot_recovering(ev)
            self.push(state, {"kind": "redo_done", "title": "Redo finished",
                              "sql": "breakpoint: end of the redo loop in PerformWalRecovery()",
                              "output": "redo done at %s" % L(ev.get("last")), "skipped": ev.get("skipped_before", 0)})
        elif kind == "eor":
            self.eor_redos.add(ev.get("redo"))
            state = self.snapshot_recovering(ev)
            state["redo"] = ev.get("redo")
            self.push(state, {"kind": "eor", "title": "End-of-recovery checkpoint",
                              "sql": "breakpoint: after RequestCheckpoint(CHECKPOINT_END_OF_RECOVERY)",
                              "output": "new REDO pointer = %s" % L(ev.get("redo"))})
        else:   # exit: recovery is over, open the database normally
            return self._open_after_recovery(ev)

    def _forget_lost_xids(self):
        """xids that never reached the WAL will be handed out again after recovery."""
        nxt = int(self.cluster.query("SELECT pg_snapshot_xmax(pg_current_snapshot())::text::bigint"))
        for x in [x for x in self.names if x >= nxt]:
            del self.names[x]
        self.orphans = {x for x in self.orphans if x < nxt}

    def _open_after_recovery(self, ev):
        self.redo.kill()
        self.redo = None
        self.cluster.start()
        self.mode = "up"
        self._forget_lost_xids()
        state = self.snapshot_up()
        self.push(state, {"kind": "open", "title": "Database is open again",
                          "sql": "pg_ctl start   (no recovery needed now)",
                          "output": "single-user backend exited with code %s" % ev.get("code")})

    def _recovery_failed(self, ev):
        msg = ev.get("message") or ev.get("log") or "gdb stopped unexpectedly"
        self.redo.kill()
        self.redo = None
        self.mode = "up"
        self.cluster.start()           # let PostgreSQL recover on its own
        self._forget_lost_xids()
        state = self.snapshot_up()
        self.push(state, {"kind": "open", "title": "Recovery finished without gdb",
                          "sql": "pg_ctl start", "error": True,
                          "output": "The gdb-driven recovery failed, so PostgreSQL recovered normally.\n" + msg})

    # ================================================================= snapshots
    def snapshot_up(self, step_label=None):
        c = self.cluster
        meta = c.query_json(META_SQL)
        self.tables = {}
        pages = {}
        for r in meta["rels"]:
            name = r["name"]
            relnum = int(r["relfilenode"])          # oid arrives as a JSON string
            t = {"name": name, "relfilenode": relnum, "path": os.path.join(c.data, r["path"]),
                 "nblocks": int(r["nblocks"]), "cols": r["cols"]}
            self.tables[name] = t
            self.relnames[relnum] = name
            mem = {i: base64.b64decode(p) for i, p in enumerate(r["pages"] or [])}
            pages[name] = self._pages(t, mem)
        for a in meta["activity"] or []:
            if a["xid"]:
                self._name(a["xid"], a["app"][4:])
        state = {"server": "up", "insert": lsn(meta["insert"]), "flush": lsn(meta["flush"]),
                 "redo": lsn(meta["redo"]), "ckpt": lsn(meta["ckpt"]), "pages": pages}
        self._fetch_wal(state["flush"])
        xids = self._interesting(pages)
        # a step's brand-new xids belong to the session that ran it
        for x in xids:
            if x not in self.names:
                self.names[x] = step_label
        state["visible"] = self._visible()
        state["clog"] = self._clog(xids, mem=True)
        self.state = state
        return state

    def snapshot_down(self, before, ctl):
        pages = {}
        for name, t in self.tables.items():
            pages[name] = self._pages(t, None)
        state = {"server": "down", "insert": before["flush"], "flush": before["flush"],
                 "redo": lsn(ctl.get("Latest checkpoint's REDO location")),
                 "ckpt": lsn(ctl.get("Latest checkpoint location")), "pages": pages, "visible": None,
                 "clog": self._clog(before["clog"].keys(), mem=False)}
        self.state = state
        return state

    def snapshot_recovering(self, ev):
        pages = {}
        gp = ev.get("pages") or {}
        for name, t in self.tables.items():
            mem = {int(b): base64.b64decode(p) for b, p in gp.get(str(t["relfilenode"]), {}).items()}
            pages[name] = self._pages(t, mem)
        prev = self.state
        clog = self._clog(prev["clog"].keys(), mem=False)
        for x, bits in (ev.get("clog") or {}).items():
            if int(x) in clog:
                clog[int(x)]["raw"] = bits
        state = {"server": "recovering", "insert": prev["insert"], "flush": prev["flush"],
                 "redo": prev["redo"], "ckpt": prev["ckpt"], "pages": pages, "visible": None, "clog": clog}
        self.state = state
        return state

    # ---------------------------------------------------------------- helpers
    def _name(self, xid, label):
        self.names[xid] = label
        if label and label not in self.labels and not label.startswith("auto"):
            self.labels.append(label)

    def _pages(self, t, mem):
        """{blk: {'mem': parsed|None, 'file': parsed|None, 'mem_raw', 'file_raw'}}; mem=None means wiped."""
        out = {}
        nfile = min(heappage.file_nblocks(t["path"]), MAXPAGES)
        blks = set(range(nfile)) | set((mem or {}).keys())
        for b in sorted(blks):
            fraw = heappage.read_file_block(t["path"], b)
            mraw = (mem or {}).get(b)
            p = {"mem": heappage.parse_page(mraw, t["cols"]) if mraw else None,
                 "file": heappage.parse_page(fraw, t["cols"]) if fraw else None,
                 "mem_raw": mraw, "file_raw": fraw, "mem_known": mem is not None}
            for src in ("mem", "file"):
                if p[src]:
                    for it in p[src]["items"]:
                        if "values" in it:
                            self.cache[(t["name"], b, it["lp"], it["xmin"])] = it["values"]
            out[b] = p
        return out

    def _interesting(self, pages):
        xids = {x for x, n in self.names.items()}
        for tp in pages.values():
            for p in tp.values():
                for src in ("mem", "file"):
                    for it in (p[src] or {}).get("items", []):
                        if it.get("xmin", 0) >= 3 and not (it["infomask"] & 0x0300 == 0x0300):
                            xids.add(it["xmin"])
                        if it.get("xmax", 0) >= 3 and not it["infomask"] & 0x1000:
                            xids.add(it["xmax"])
        return xids

    def _visible(self):
        if not self.tables:
            return {}
        parts = []
        for name in self.tables:
            q = '"' + name.replace('"', '""') + '"'
            lit = "'" + name.replace("'", "''") + "'"
            parts.append("%s, (SELECT coalesce(json_agg(ctid::text), '[]'::json) FROM ONLY public.%s)" % (lit, q))
        try:
            res = self.cluster.query_json("SELECT json_build_object(%s);" % ", ".join(parts))
            return {k: set(v) for k, v in res.items()}
        except RuntimeError:
            return None          # e.g. a table is locked by an open DDL transaction

    def _clog(self, xids, mem):
        xids = sorted(int(x) for x in xids if int(x) >= 3)
        res = {x: {"mem": None, "raw": None, "disk": self._clog_disk(x)} for x in xids}
        if mem and xids:
            # an xid whose WAL never reached disk is "in the future" after a crash
            q = ("SELECT json_object_agg(x, CASE WHEN x::text::xid8 < pg_snapshot_xmax(pg_current_snapshot()) "
                 "THEN pg_xact_status(x::text::xid8) END) FROM unnest(ARRAY[%s]::bigint[]) x;" %
                 ",".join(str(x) for x in xids))
            for k, v in (self.cluster.query_json(q) or {}).items():
                res[int(k)]["mem"] = v
        return res

    def _clog_disk(self, xid):
        per_seg = 8192 * 4 * 32
        path = os.path.join(self.cluster.data, "pg_xact", "%04X" % (xid // per_seg))
        try:
            with open(path, "rb") as f:
                f.seek((xid % per_seg) // 4)
                b = f.read(1)
            return (b[0] >> ((xid % 4) * 2)) & 3 if b else 0
        except OSError:
            return 0

    # ---------------------------------------------------------------- WAL
    def _fetch_wal(self, flush):
        if self.wal_end is None or flush is None or flush <= self.wal_end:
            return
        rows = self.cluster.query_json(WAL_SQL, {"from": L(self.wal_end), "to": L(flush)}) or []
        before = len(self.wal)
        for r in rows:
            self._add_wal(r)
        # the PRUNE description doesn't list slots; keep the ones we derived earlier
        derived = [it for g in self.pending for it in g["items"] if it["kind"] == "prune"]
        for w in self.wal[before:]:
            if w["kind"] == "prune" and not w["slots"]:
                it = next((i for i in derived if i["rel"] == w["rel"] and i["blk"] == w["blk"]), None)
                if it:
                    w["slots"] = it["slots"]
        if rows:
            self.wal_end = max(self.wal_end, lsn(rows[-1]["end"]))
        # derived records that are now on disk are replaced by the real ones
        self.pending = [g for g in self.pending if g["start"] >= flush]

    def _add_wal(self, r):
        start, end = lsn(r["start"]), lsn(r["end"])
        rmgr, rtype, desc = r["rmgr"], r["type"], r["desc"] or ""
        w = {"start": start, "end": end, "rmgr": rmgr, "type": rtype, "xid": r["xid"], "desc": desc,
             "src": "real", "fpi": (r["fpi"] or 0) > 0, "init": "INIT" in rtype, "rel": None, "blk": None,
             "slots": [], "kind": "other"}
        for m in BLKREF_RE.finditer(r["blk"] or ""):
            relnum, fork = int(m.group(3)), m.group(4) or "main"
            if relnum in self.relnames and fork == "main":
                w["rel"], w["blk"] = self.relnames[relnum], int(m.group(5))
                break
        if rmgr == "XLOG" and rtype == "CHECKPOINT_REDO":
            w["kind"] = "ckpt"
            w["redo"] = start
            w["partial"] = True
            self.wal.append(w)
            return
        if rmgr == "XLOG" and rtype.startswith("CHECKPOINT"):
            m = re.search(r"redo ([0-9A-F]+/[0-9A-F]+)", desc)
            redo = lsn(m.group(1)) if m else None
            for o in self.wal:
                if o["kind"] == "ckpt" and o.get("partial") and o["redo"] == redo:
                    o.update(end=end, partial=False, type=rtype)
                    return
            w.update(kind="ckpt", redo=redo)
            self.wal.append(w)
            return
        if rmgr == "Transaction" and rtype.startswith("COMMIT"):
            w["kind"] = "commit"
        elif rmgr == "Transaction" and rtype.startswith("ABORT"):
            w["kind"] = "abort"
            self.abort_xids.add(r["xid"])
        elif rmgr in ("Heap", "Heap2") and w["rel"]:
            if rtype.startswith("INSERT") or rtype.startswith("MULTI_INSERT"):
                w["kind"] = "insert"
                w["slots"] = [int(x) for x in re.findall(r"\boff: (\d+)", desc)]
            elif rtype.startswith("DELETE"):
                w["kind"] = "delete"
                w["slots"] = [int(x) for x in re.findall(r"\boff: (\d+)", desc)]
            elif "UPDATE" in rtype:
                w["kind"] = "update"
                old = re.search(r"old_off: (\d+)", desc)
                new = re.search(r"new_off: (\d+)", desc)
                w["slots"] = [int(old.group(1)) if old else None, int(new.group(1)) if new else None]
            elif rtype.startswith("LOCK"):
                w["kind"] = "lock"
            elif "PRUNE" in rtype or "VACUUM" in rtype:
                w["kind"] = "prune"
            elif rtype.startswith("VISIBLE"):
                w["kind"] = "visible"
        else:
            self.hidden += 1
            return
        self.wal.append(w)

    def _add_wal_from_gdb(self, rec):
        blk = next((b for b in rec["blocks"] if b["relnumber"] in self.relnames and b["fork"] == 0), None)
        self._add_wal({"start": L(rec["start"]), "end": L(rec["end"]), "xid": rec["xid"], "rmgr": rec["rmgr"],
                       "type": rec["type"], "desc": "",
                       "blk": ("blkref #0: rel 0/0/%d fork main blk %d%s" % (blk["relnumber"], blk["blk"], " (FPW)" if blk["image"] else "")) if blk else "",
                       "fpi": 1 if blk and blk["image"] else 0})
        self.wal.sort(key=lambda w: w["start"])

    def _wal_at(self, start):
        return next((w for w in self.wal if w["start"] == start), None)

    def _derive_pending(self, before, after, step, error):
        """Describe WAL that this step inserted but nobody has flushed yet."""
        if before is None or after["insert"] <= before["insert"] or after["flush"] >= after["insert"]:
            return
        items = []
        start = max(before["insert"], after["flush"])
        for name, tp in after["pages"].items():
            bp = (before["pages"] or {}).get(name, {})
            for blk, p in tp.items():
                now = {it["lp"]: it for it in (p["mem"] or {}).get("items", [])}
                old = {it["lp"]: it for it in ((bp.get(blk) or {}).get("mem") or {}).get("items", [])}
                for lp, it in now.items():
                    o = old.get(lp)
                    if it["flags"] == 1 and (o is None or o["flags"] != 1 or o.get("xmin") != it.get("xmin")):
                        items.append({"kind": "insert", "rel": name, "blk": blk, "slots": [lp], "xid": it["xmin"]})
                    elif it["flags"] == 1 and o and o.get("xmax") != it.get("xmax") and it.get("xmax"):
                        k = "lock" if it["infomask"] & 0x0080 else "delete"
                        items.append({"kind": k, "rel": name, "blk": blk, "slots": [lp], "xid": it["xmax"]})
                    elif it["flags"] != 1 and o and o["flags"] == 1:
                        items.append({"kind": "prune", "rel": name, "blk": blk, "slots": [lp], "xid": 0})
                for lp, o in old.items():            # slot array truncated by VACUUM
                    if lp not in now and o["flags"] == 1 and p["mem"]:
                        items.append({"kind": "prune", "rel": name, "blk": blk, "slots": [lp], "xid": 0})
        # VACUUM frees many slots of one page with a single record
        prunes = {}
        for i in [i for i in items if i["kind"] == "prune"]:
            items.remove(i)
            prunes.setdefault((i["rel"], i["blk"]), []).extend(i["slots"])
        for (rel, blk), slots in prunes.items():
            items.append({"kind": "prune", "rel": rel, "blk": blk, "slots": sorted(slots), "xid": 0})
        # an UPDATE shows up as delete(old) + insert(new) by the same xid
        dels = [i for i in items if i["kind"] == "delete"]
        for d in dels:
            ins = next((i for i in items if i["kind"] == "insert" and i["xid"] == d["xid"] and i["rel"] == d["rel"]), None)
            if ins:
                items.remove(d)
                ins.update(kind="update", slots=[d["slots"][0], ins["slots"][0]])
        word = step["sql"].strip().split(None, 1)[0].upper().rstrip(";") if step["sql"].strip() else ""
        sx = [x for x, n in self.names.items() if n == step["label"] and step["label"]]
        if (word in ("ROLLBACK", "ABORT") or error) and sx:
            x = max(sx)
            if x not in self.abort_xids:
                items.append({"kind": "abort", "xid": x})
                self.abort_xids.add(x)
        for i in items:
            i.update(src="pending", start=start, end=after["insert"], fpi=False, init=False)
        if items:
            self.pending.append({"start": start, "end": after["insert"], "items": items})

    # ================================================================= views
    def push(self, state, info):
        # recovery steps belong to the scenario line that crashed the server
        if info.get("line"):
            self._last_line = info["line"]
        else:
            info["line"] = getattr(self, "_last_line", None)
        prev = self.history[-1] if self.history else None
        view = narrate.build_view(self, state, info, prev)
        view["index"] = len(self.history)
        self.history.append(view)
        return view


def _first_line(sql):
    line = sql.strip().splitlines()[0]
    return line if len(line) <= 70 else line[:67] + "…"
