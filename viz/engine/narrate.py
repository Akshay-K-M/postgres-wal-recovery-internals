"""Turn a raw snapshot into the view the browser renders, plus plain-English notes."""

BITS = {0: "in progress", 1: "committed", 2: "aborted", 3: "sub-committed"}
CODE = {0: "00", 1: "01", 2: "10", 3: "11"}
STATUS_BITS = {"in progress": 0, "committed": 1, "aborted": 2}


def L(n):
    return None if n is None else "%X/%08X" % (n >> 32, n & 0xFFFFFFFF)


# ------------------------------------------------------------------ naming
def tx(run, xid):
    if not xid:
        return None
    name = run.names.get(xid)
    color = run.labels.index(name) if name in run.labels else -1
    return {"xid": xid, "name": name or "xid %d" % xid, "color": color}


def fmt_values(vals):
    if vals is None:
        return "?"
    return "(" + ", ".join("NULL" if v is None else v for v in vals) + ")"


def wal_text(w, run):
    k = w["kind"]
    who = (w.get("txname") or run.names.get(w.get("xid")) or ("xid %s" % w.get("xid"))) if w.get("xid") else ""
    where = "%s p%s" % (w["rel"], w["blk"]) if w.get("rel") else ""
    if k == "insert":
        slot = w["slots"][0] if w.get("slots") else None
        vals = run.cache.get((w["rel"], w["blk"], slot, w.get("xid")))
        return "INSERT %s → %s slot %s" % (fmt_values(vals) if vals else "row", where, slot if slot else "?")
    if k == "update":
        a, b = (w.get("slots") or [None, None])[:2]
        return "UPDATE %s slot %s → slot %s" % (where, a or "?", b or "?")
    if k == "delete":
        return "DELETE %s slot %s" % (where, (w.get("slots") or ["?"])[0])
    if k == "lock":
        return "LOCK row in %s" % where
    if k == "prune":
        slots = w.get("slots") or []
        return "VACUUM %s%s" % (where, " → free slot%s %s" % ("s" if len(slots) > 1 else "", ", ".join(map(str, slots))) if slots else "")
    if k == "visible":
        return "mark %s all-visible" % where
    if k == "commit":
        return "COMMIT " + who
    if k == "abort":
        return "ABORT " + who
    if k == "ckpt":
        if w.get("partial"):
            return "CHECKPOINT starts (REDO point)"
        if w.get("redo") in run.eor_redos:
            return "CHECKPOINT (end of recovery)"
        if "SHUTDOWN" in (w.get("type") or ""):
            return "CHECKPOINT (shutdown)"
        return "CHECKPOINT"
    return "%s %s" % (w.get("rmgr", ""), w.get("type", ""))


# ------------------------------------------------------------------ CLOG / status
def txstate(run, state, xid, infomask=0, which="xmin"):
    if which == "xmin" and infomask & 0x0300 == 0x0300:
        return "committed"                      # frozen
    if xid < 3:
        return "committed"
    c = state["clog"].get(xid)
    if c is None:
        return "unknown"
    if state["server"] == "up":
        s = c["mem"] or BITS[c["disk"]]
        if s == "aborted" and xid in run.orphans:
            return "crashed"
    elif state["server"] == "recovering":
        s = BITS[c["raw"] if c.get("raw") is not None else c["disk"]]
    else:
        s = BITS[c["disk"]]
    if s == "in progress" and xid in run.orphans:
        return "crashed"            # its backend died in a crash: it can never commit
    return {"in progress": "inprog"}.get(s, s)


def clog_view(run, state, prev):
    out = []
    pv = {c["xid"]: c for c in (prev or {}).get("clog", [])}
    for xid in sorted(state["clog"]):
        c = state["clog"][xid]
        t = tx(run, xid)
        disk = {"code": CODE[c["disk"]], "text": BITS[c["disk"]]}
        if state["server"] == "down":
            mem = None
        elif state["server"] == "recovering":
            raw = c.get("raw")
            mem = ({"code": CODE[raw], "text": BITS[raw]} if raw is not None
                   else {"code": "··", "text": "not loaded yet", "note": "read from pg_xact when needed"})
        else:
            s = c["mem"] or BITS[c["disk"]]
            if s == "aborted" and xid in run.orphans:
                mem = {"code": "00", "text": "in progress", "note": "nobody runs it any more → treated as aborted",
                       "crashed": True}
            else:
                mem = {"code": CODE[STATUS_BITS.get(s, 0)], "text": s}
        e = dict(t, mem=mem, disk=disk)
        e["lag"] = bool(mem and mem["code"] not in (disk["code"], "··") and not mem.get("crashed"))
        p = pv.get(xid)
        e["chg"] = prev is not None and (p is None or p.get("mem") != mem or p.get("disk") != disk)
        out.append(e)
    return out


def procarray_view(run, state, prev):
    raw = state.get("procarray", {"source": "unavailable", "entries": []})
    previous = {p["pid"]: p for p in (prev or {}).get("procarray", {}).get("entries", [])}
    entries = []
    for proc in sorted(raw["entries"], key=lambda p: p["pid"]):
        row = dict(proc)
        row["name"] = row.pop("app", "backend").removeprefix("viz:")
        old = previous.get(row["pid"])
        fields = ("xid", "xmin", "state", "wait", "wait_type")
        row["chg"] = prev is not None and (old is None or any(old.get(k) != row.get(k) for k in fields))
        entries.append(row)
    notes = {
        "activity": "Scenario sessions only, via pg_stat_activity backend_xid/backend_xmin. "
                    "This is a projection of backend state, not a full ProcArray dump; inspector and background processes are omitted.",
        "wiped": "Shared memory was lost in the crash. The old ProcArray has no surviving entries and no on-disk copy.",
        "gdb": "Actual ProcArray entries read through GDB in the new single-user recovery process. "
               "Replaying a transaction's WAL does not recreate its old session.",
        "uninitialized": "The new process has not initialized ProcArray yet.",
        "unavailable": "ProcArray could not be read at this recovery stop.",
    }
    return dict(raw, entries=entries, note=notes.get(raw["source"], ""))


# ------------------------------------------------------------------ table rows
STATUS_TEXT = {
    "live": "committed",
    "inprog": "inserted by {i}, not committed",
    "aborted": "inserted by {i}, aborted",
    "crashed": "inserted by {i}, never committed (crash) → treated as aborted",
    "deleting": "deleted by {d}, not committed yet",
    "deleted": "deleted by {d} (committed): dead",
    "removed": "free slot (row removed)",
    "redirect": "redirect (HOT chain)",
    "ghost": "lost from memory · only in the WAL",
}


def row_status(run, state, it):
    if it["flags"] == 2:
        return "redirect"
    if it["flags"] != 1:
        return "removed"
    s1 = txstate(run, state, it["xmin"], it["infomask"], "xmin")
    if s1 in ("aborted", "crashed", "inprog"):
        return s1
    xmax = it.get("xmax") or 0
    if xmax and not it["infomask"] & 0x0080 and not it["infomask"] & 0x0800 and not it["infomask"] & 0x1000:
        s2 = txstate(run, state, xmax, which="xmax")
        if s2 == "committed":
            return "deleted"
        if s2 == "inprog":
            return "deleting"
    return "live"


def creating_record(run, table, blk, lp, xmin, wal_entries):
    for w in reversed(wal_entries):
        if w.get("rel") == table and w.get("blk") == blk and w.get("xid") == xmin:
            if (w["kind"] == "insert" and lp in (w.get("slots") or [])) or \
               (w["kind"] == "update" and (w.get("slots") or [None, None])[1] == lp):
                return w
    return None


def table_views(run, state, prev, wal_entries):
    pv_rows = {}
    for t in (prev or {}).get("tables", []):
        for p in t["pages"]:
            for r in p["rows"]:
                pv_rows[(t["name"], p["blk"], r["lp"])] = r["sig"]
    out = []
    up = state["server"] == "up"
    for name, t in run.tables.items():
        cols = [c["name"] for c in t["cols"] if not c.get("dropped")]
        keep = [i for i, c in enumerate(t["cols"]) if not c.get("dropped")]
        pages = []
        for blk, p in sorted(state["pages"].get(name, {}).items()):
            mem, fil = p["mem"], p["file"]
            mitems = {it["lp"]: it for it in (mem or {}).get("items", [])}
            fitems = {it["lp"]: it for it in (fil or {}).get("items", [])}
            slots = set(mitems) | set(fitems)
            ghosts = set()
            if state["server"] != "up":
                for w in wal_entries:
                    if w["src"] == "real" and w.get("rel") == name and w.get("blk") == blk and w["kind"] in ("insert", "update"):
                        s = (w.get("slots") or [None])[-1]
                        if s and s not in slots:
                            ghosts.add(s)
                            slots.add(s)
            rows = []
            vis = (state.get("visible") or {}).get(name) if up else None
            for lp in sorted(slots):
                m, f = mitems.get(lp), fitems.get(lp)
                if mem is not None and m is None and f is not None:
                    m = {"lp": lp, "flags": 0}       # VACUUM truncated the slot array in memory
                src = m if mem is not None else f
                if lp in ghosts:
                    w = next(w for w in reversed(wal_entries) if w.get("rel") == name and w.get("blk") == blk
                             and w["kind"] in ("insert", "update") and (w.get("slots") or [None])[-1] == lp)
                    vals = run.cache.get((name, blk, lp, w.get("xid")))
                    r = {"lp": lp, "status": "ghost", "xmin": tx(run, w.get("xid")), "xmax": None,
                         "values": [vals[i] if vals else None for i in keep] if vals else None,
                         "wal": "disk", "file": "no", "visible": None}
                elif src is None:
                    continue
                else:
                    st = row_status(run, state, src)
                    vals = src.get("values")
                    if vals is None:   # removed slot: remember what used to be here
                        old = [v for (tn, b, l, x), v in run.cache.items() if (tn, b, l) == (name, blk, lp)]
                        vals = old[-1] if old else None
                    w = creating_record(run, name, blk, lp, src.get("xmin"), wal_entries) if src.get("xmin") else None
                    if mem is None and state["server"] == "down":
                        file_b = "yes"
                    elif f is None:
                        file_b = "no" if st not in ("removed",) else "freed"
                    elif st in ("removed", "redirect"):
                        file_b = "stale" if f["flags"] == 1 else "freed"
                    elif f["flags"] == 1 and f.get("xmin") == src.get("xmin"):
                        file_b = "yes" if f.get("xmax") == src.get("xmax") else "stale"
                    else:
                        file_b = "no"
                    r = {"lp": lp, "status": st,
                         "xmin": tx(run, src.get("xmin")) if st not in ("removed", "redirect") else None,
                         "xmax": tx(run, src.get("xmax")) if src.get("xmax") and st in ("deleted", "deleting") else None,
                         "values": [vals[i] if i < len(vals) else None for i in keep] if vals else None,
                         "wal": w["state"] if w else None, "file": file_b,
                         "visible": ("(%d,%d)" % (blk, lp)) in vis if vis is not None else None}
                i_name = r["xmin"]["name"] if r.get("xmin") else "?"
                d_name = r["xmax"]["name"] if r.get("xmax") else "?"
                r["text"] = STATUS_TEXT[r["status"]].format(i=i_name, d=d_name)
                r["sig"] = "|".join(str(x) for x in (r["status"], r["visible"], r["wal"], r["file"], r["values"]))
                old = pv_rows.get((name, blk, lp))
                r["chg"] = "new" if prev is not None and old is None else ("chg" if old is not None and old != r["sig"] else "")
                rows.append(r)
            if state["server"] == "down":
                mstate = "wiped"
            elif mem is None:
                mstate = "notloaded" if state["server"] == "recovering" else "absent"
            else:
                mstate = "present"
            if fil is None:
                fstate = "none"
            elif fil["new"]:
                fstate = "blank"
            else:
                fstate = "present"
            pages.append({"blk": blk, "mem": mstate, "file": fstate,
                          "mem_lsn": L(mem["lsn"]) if mem and not mem["new"] else None,
                          "file_lsn": L(fil["lsn"]) if fil and not fil["new"] else None,
                          "dirty": bool(p["mem_raw"] and p["mem_raw"] != p["file_raw"]),
                          "rows": rows})
        out.append({"name": name, "cols": cols, "pages": pages, "nblocks": t["nblocks"]})
    return out


# ------------------------------------------------------------------ WAL list
def wal_views(run, state, info):
    interesting = set(state["clog"].keys()) | set(run.names.keys())
    entries = []
    for w in run.wal:
        if w["kind"] in ("commit", "abort") and w["xid"] not in interesting:
            continue
        if w["kind"] == "other":
            continue
        e = dict(w, state="disk")
        entries.append(e)
    for g in run.pending:
        for it in g["items"]:
            entries.append(dict(it, state="mem"))
    for it in run.lost:
        entries.append(dict(it, state="lost"))
    entries.sort(key=lambda e: (e["start"], {"disk": 0, "mem": 1, "lost": 2}[e["state"]]))
    rc = run.rctx if state["server"] == "recovering" else {}
    for e in entries:
        e["replay"] = None
        if rc and e["state"] == "disk":
            if rc.get("redo") is not None and e["start"] < rc["redo"]:
                e["replay"] = "skip"
            elif rc.get("done") or (rc.get("applied") is not None and e["start"] < rc["applied"]):
                e["replay"] = "done"
            elif rc.get("applied") == e["start"]:
                e["replay"] = "now"
    return entries


def wal_public(run, state, entries, prev):
    prev_keys = {(w["key"]): w["state"] for w in (prev or {}).get("wal", [])}
    out = []
    for e in entries:
        key = "%s:%s:%s" % (e["start"], e["kind"], ",".join(str(s) for s in e.get("slots") or []))
        tags = []
        if e["kind"] == "ckpt" and e.get("redo") is not None and e["redo"] == state.get("redo"):
            tags.append("REDO pointer")
        if e.get("init"):
            tags.append("new page")
        if e.get("fpi"):
            tags.append("full-page image")
        pst = prev_keys.get(key)
        out.append({"key": key, "lsn": L(e["start"]), "kind": e["kind"], "text": wal_text(e, run),
                    "tx": (dict(tx(run, e.get("xid")) or {}, name=e["txname"], color=run.labels.index(e["txname"]) if e["txname"] in run.labels else -1)
                           if e.get("txname") else tx(run, e.get("xid"))) if e["kind"] != "ckpt" else None,
                    "state": e["state"], "tags": tags, "replay": e.get("replay"),
                    "chg": "new" if prev is not None and pst is None else ("chg" if pst and pst != e["state"] else "")})
    return out


# ------------------------------------------------------------------ narration
def verb(sql):
    w = (sql or "").strip().split(None, 1)
    return w[0].upper().rstrip(";") if w else ""


def tip(info, view, run):
    k = info["kind"]
    rec = info.get("record")
    if k == "init":
        return ("Four places to watch: the table (page in memory vs. the table file on disk), the WAL "
                "(in memory vs. flushed to disk), the CLOG (commit status) and ProcArray (current backend transaction state).", "")
    if k == "crash":
        return ("Immediate shutdown: no shutdown checkpoint. Shared buffers, WAL buffers and the in-memory CLOG "
                "and ProcArray are gone. What survives: the table file, pg_xact and the WAL that was flushed.",
                "pg_ctl stop -m immediate → SIGQUIT")
    if k == "restart":
        return ("pg_control says the server didn't shut down cleanly, so the startup process replays WAL starting "
                "at the last checkpoint's REDO pointer. Older WAL is already reflected in the files (greyed out).",
                "StartupXLOG → InitWalRecovery → PerformWalRecovery")
    if k == "redo":
        if not rec:
            return ("Redo applied a record.", "ApplyWalRecord")
        if rec["kind"] in ("insert", "update", "delete", "lock", "prune", "visible"):
            s = ("Redo re-applies this change whether or not the transaction committed. Because the page in memory "
                 "is older than this record (page LSN < record LSN), the change is applied.")
            if rec.get("fpi"):
                s = ("Redo re-applies this change whether or not the transaction committed. This record carries a "
                     "full-page image, so redo restores the whole page from the WAL.")
            return (s, "ApplyWalRecord → heap_redo → heap_xlog_%s → XLogReadBufferForRedo" %
                    {"insert": "insert", "update": "update", "delete": "delete"}.get(rec["kind"], "…"))
        if rec["kind"] == "commit":
            return ("Replaying the COMMIT record sets the transaction's CLOG bits to committed (01) again. "
                    "That update had been lost with the in-memory CLOG.",
                    "ApplyWalRecord → xact_redo → xact_redo_commit → TransactionIdCommitTree")
        if rec["kind"] == "abort":
            return ("Replaying the ABORT record marks the transaction aborted (10) in the CLOG.",
                    "ApplyWalRecord → xact_redo → xact_redo_abort → TransactionIdAbortTree")
        if rec["kind"] == "ckpt":
            return ("A checkpoint record: nothing to change in the table.", "ApplyWalRecord → xlog_redo")
        return ("Redo applied this record.", "ApplyWalRecord")
    if k == "redo_done":
        return ("No more valid WAL, so redo stops. That was the only pass: there is no undo. A transaction "
                "with no COMMIT and no ABORT record just stays 00 (in progress) in the CLOG.",
                "PerformWalRecovery: redo loop ends")
    if k == "eor":
        return ("The end-of-recovery checkpoint writes the replayed pages and the CLOG to disk.",
                "PerformRecoveryXLogAction → RequestCheckpoint(CHECKPOINT_END_OF_RECOVERY)")
    if k == "open":
        return ("Normal operation again. Rows of transactions that never committed are still on the page, but "
                "no one will ever see them: their xid is aborted or orphaned in the CLOG.", "")
    v = verb(info.get("sql"))
    if info.get("error"):
        return ("An ERROR aborts the transaction on the spot: an ABORT record is written and the CLOG says "
                "aborted, before anyone types ROLLBACK.",
                "elog(ERROR) → AbortCurrentTransaction → AbortTransaction → RecordTransactionAbort")
    if info.get("blocked"):
        return ("This statement is waiting for a lock held by another open transaction. It will finish once "
                "that transaction ends.", "")
    if v in ("BEGIN", "START"):
        return ("BEGIN opens a transaction block; its backend appears with no xid yet. An xid is usually assigned at the first write.", "")
    if v in ("COMMIT", "END"):
        return ("COMMIT writes a commit record and waits until the WAL is flushed to disk; then the CLOG marks "
                "the transaction committed. The table page itself can stay in memory: the WAL makes it durable.",
                "CommitTransaction → RecordTransactionCommit → XLogFlush → TransactionIdCommitTree")
    if v in ("ROLLBACK", "ABORT"):
        if not any(w["chg"] == "new" for w in view["wal"]):
            return ("In a transaction that already failed, ROLLBACK only cleans up. No new WAL.",
                    "UserAbortTransactionBlock → CleanupTransaction")
        return ("ROLLBACK writes an ABORT record, which is not flushed (after a crash, no COMMIT means aborted "
                "anyway), and marks the CLOG aborted. The rows stay on the page: nothing is undone.",
                "AbortTransaction → RecordTransactionAbort → TransactionIdAbortTree")
    if v in ("INSERT", "UPDATE", "DELETE"):
        s = ("The change happens in the page in shared buffers, and a WAL record goes to the WAL buffers. "
             "Neither is on disk yet.")
        if not info.get("label"):
            s += " Without BEGIN the statement commits immediately, so its WAL is flushed."
        return (s, "heap_%s → XLogInsert" % v.lower())
    if v == "CHECKPOINT":
        return ("A checkpoint writes every dirty page and the CLOG to disk, committed or not, and moves the "
                "REDO pointer. After a crash, recovery starts from here.",
                "CreateCheckPoint → CheckPointGuts → CheckPointCLOG, CheckPointBuffers")
    if v == "VACUUM":
        return ("VACUUM removes row versions nobody can ever see: inserted by aborted or crashed transactions, or "
                "deleted by committed ones. Their slots become free.",
                "heap_vacuum_rel → heap_page_prune_and_freeze → HeapTupleSatisfiesVacuum")
    if v in ("SELECT", "TABLE"):
        return ("A row is visible when its xmin committed (per the CLOG) and its xmax did not. "
                "Readers also set hint bits so later readers can skip the CLOG.",
                "heapam_scan → HeapTupleSatisfiesMVCC → TransactionIdDidCommit")
    if v in ("CREATE", "DROP", "ALTER", "TRUNCATE"):
        return ("DDL is WAL-logged as well: it changes the system catalogs (pg_class, pg_attribute, pg_type and "
                "their indexes) and commits, which takes dozens of WAL records (about a hundred for a CREATE "
                "TABLE). To keep the panel readable they are not listed; they are "
                "counted in \"catalog/other records hidden\" under the WAL panel.", "")
    return ("", "")


def changes(run, view, prev, info):
    out = list(info.get("notes") or [])
    if prev is None:
        return out
    new_wal = [w for w in view["wal"] if w["chg"] == "new" and w["state"] != "lost"]
    for w in new_wal[:3]:
        out.append("WAL %s: %s" % ("in memory" if w["state"] == "mem" else "on disk", w["text"]))
    if len(new_wal) > 3:
        out.append("…and %d more WAL records" % (len(new_wal) - 3))
    if view["lsn"]["flush"] != prev["lsn"]["flush"] and view["server"] == "up" and prev["server"] == "up":
        flushed = [w for w in view["wal"] if w["chg"] == "chg" and w["state"] == "disk"]
        out.append("WAL flushed to disk up to %s%s" % (view["lsn"]["flush"],
                   " (including %d record%s that were only in memory)" % (len(flushed), "" if len(flushed) == 1 else "s") if flushed else ""))
    if view["lsn"]["redo"] != prev["lsn"]["redo"]:
        out.append("REDO pointer moved to %s" % view["lsn"]["redo"])
    pc = {c["xid"]: c for c in prev["clog"]}
    for c in view["clog"]:
        p = pc.get(c["xid"])
        if p is None:
            continue
        rec = info.get("record") or {}
        loaded_only = p["mem"] and p["mem"]["code"] == "··" and rec.get("xid") != c["xid"]
        if c["mem"] and p["mem"] and c["mem"]["code"] != p["mem"]["code"] and not loaded_only:
            out.append("CLOG in memory: %s %s → %s" % (c["name"], p["mem"]["text"], c["mem"]["text"]))
        if c["disk"]["code"] != p["disk"]["code"]:
            out.append("pg_xact file on disk: %s → %s" % (c["name"], c["disk"]["text"]))
    ppages = {(t["name"], p["blk"]): p for t in prev["tables"] for p in t["pages"]}
    for t in view["tables"]:
        for p in t["pages"]:
            q = ppages.get((t["name"], p["blk"]))
            if (q is None and p["file"] == "present") or (q and q["file_lsn"] != p["file_lsn"] and p["file"] == "present"):
                out.append("%s page %d written to the table file" % (t["name"], p["blk"]))
    if view["select"] is not None and prev.get("select") is not None:
        for t in view["tables"]:
            def rowset(sel):
                return {tuple("NULL" if v is None else v for v in r) for r in (sel.get(t["name"]) or []) if r}
            a, b = rowset(view["select"]), rowset(prev["select"])
            for r in sorted(a - b):
                out.append("now visible to everyone: %s %s" % (t["name"], fmt_values(list(r))))
            for r in sorted(b - a):
                out.append("no longer visible: %s %s" % (t["name"], fmt_values(list(r))))
    if info.get("skipped"):
        out.append("replayed %d other WAL record%s without stopping (catalog, standby info, …)"
                   % (info["skipped"], "" if info["skipped"] == 1 else "s"))
    return out


def build_view(run, state, info, prev):
    entries = wal_views(run, state, info)
    wal = wal_public(run, state, entries, prev)
    tables = table_views(run, state, prev, entries)
    select = None
    if state["server"] == "up" and state.get("visible") is not None:
        select = {}
        for t in tables:
            select[t["name"]] = [r["values"] for p in t["pages"] for r in p["rows"] if r.get("visible")]
    view = {
        "kind": info["kind"], "title": info["title"], "caption": info.get("caption") or "",
        "label": info.get("label"), "sql": info.get("sql") or "", "output": info.get("output") or "",
        "error": bool(info.get("error")), "blocked": bool(info.get("blocked")), "line": info.get("line"),
        "server": state["server"],
        "lsn": {"insert": L(state["insert"]), "flush": L(state["flush"]), "redo": L(state["redo"])},
        "tables": tables, "wal": wal, "wal_hidden": run.hidden,
        "clog": clog_view(run, state, prev), "select": select,
        "procarray": procarray_view(run, state, prev),
        "labels": run.labels,
    }
    view["changes"] = changes(run, view, prev, info)
    view["tip"], view["code"] = tip(info, view, run)
    return view
