"""Decode an 8 kB PostgreSQL heap page (from memory or from the table file).

Only what the visualizer needs: page LSN, line pointers, tuple headers
(xmin, xmax, hint bits) and the column values for common types.
"""
import struct
import datetime

BLCKSZ = 8192

LP_UNUSED, LP_NORMAL, LP_REDIRECT, LP_DEAD = 0, 1, 2, 3

HEAP_HASNULL = 0x0001
HEAP_XMAX_LOCK_ONLY = 0x0080
HEAP_XMIN_COMMITTED = 0x0100
HEAP_XMIN_INVALID = 0x0200
HEAP_XMAX_COMMITTED = 0x0400
HEAP_XMAX_INVALID = 0x0800
HEAP_NATTS_MASK = 0x07FF

PG_EPOCH = datetime.date(2000, 1, 1)


def page_lsn(page):
    hi, lo = struct.unpack_from("<II", page, 0)
    return (hi << 32) | lo


def _align(off, align):
    n = {"c": 1, "s": 2, "i": 4, "d": 8}.get(align, 1)
    return (off + n - 1) // n * n


def _fixed_value(raw, typname, attlen):
    try:
        if typname == "bool":
            return "true" if raw[0] else "false"
        if typname in ("int2",):
            return str(struct.unpack("<h", raw)[0])
        if typname in ("int4",):
            return str(struct.unpack("<i", raw)[0])
        if typname in ("int8",):
            return str(struct.unpack("<q", raw)[0])
        if typname in ("oid", "xid", "cid"):
            return str(struct.unpack("<I", raw)[0])
        if typname == "float4":
            return repr(struct.unpack("<f", raw)[0])
        if typname == "float8":
            return repr(struct.unpack("<d", raw)[0])
        if typname == "date":
            return str(PG_EPOCH + datetime.timedelta(days=struct.unpack("<i", raw)[0]))
        if typname in ("timestamp", "timestamptz"):
            us = struct.unpack("<q", raw)[0]
            return str(datetime.datetime(2000, 1, 1) + datetime.timedelta(microseconds=us))
        if typname == "char":
            return raw[:1].decode("latin-1")
        if typname == "name":
            return raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    except (struct.error, OverflowError, ValueError):
        pass
    return "0x" + raw.hex()


def _decode_tuple(page, off, length, cols):
    t = page[off:off + length]
    xmin, xmax = struct.unpack_from("<II", t, 0)
    ctid_hi, ctid_lo, ctid_pos = struct.unpack_from("<HHH", t, 12)
    infomask2, infomask = struct.unpack_from("<HH", t, 18)
    hoff = t[22]
    natts = infomask2 & HEAP_NATTS_MASK
    hasnull = bool(infomask & HEAP_HASNULL)
    bits = t[23:hoff] if hasnull else b""

    values = []
    pos = hoff
    for i, col in enumerate(cols):
        if i >= natts:
            values.append(None)          # column added later: NULL/default
            continue
        if hasnull and not (bits[i >> 3] >> (i & 7)) & 1:
            values.append(None)
            continue
        attlen, align, typname = col["len"], col["align"], col["typname"]
        if attlen == -1:
            if pos < len(t) and t[pos] != 0:     # short (1-byte) header: no padding
                pass
            else:
                pos = _align(pos, align)
            b0 = t[pos]
            if b0 == 0x01:                       # TOAST pointer
                val, size = "(toasted)", 2 + t[pos + 1]
            elif b0 & 0x01:                      # 1-byte header
                size = b0 >> 1
                val = t[pos + 1:pos + size].decode("utf-8", "replace")
            else:                                # 4-byte header
                word = struct.unpack_from("<I", t, pos)[0]
                size = word >> 2
                if word & 0x03 == 0x02:
                    val = "(compressed)"
                else:
                    val = t[pos + 4:pos + size].decode("utf-8", "replace")
            if typname not in ("text", "varchar", "bpchar", "name", "json", "xml") and not val.startswith("("):
                val = "(%s)" % typname
            pos += size
        elif attlen == -2:
            end = t.index(b"\0", pos)
            val = t[pos:end].decode("utf-8", "replace")
            pos = end + 1
        else:
            pos = _align(pos, align)
            val = _fixed_value(t[pos:pos + attlen], typname, attlen)
            pos += attlen
        values.append(val if not col.get("dropped") else None)
    return {
        "xmin": xmin, "xmax": xmax, "infomask": infomask, "infomask2": infomask2,
        "ctid": [(ctid_hi << 16) | ctid_lo, ctid_pos], "values": values,
    }


def parse_page(page, cols):
    """Return {'lsn', 'new', 'items': [...]} for one heap page (bytes)."""
    if page is None or len(page) < BLCKSZ:
        return None
    pd_lower, pd_upper = struct.unpack_from("<HH", page, 12)
    if pd_upper == 0:                     # all-zero page: allocated, never initialised
        return {"lsn": page_lsn(page), "new": True, "items": []}
    items = []
    nlp = max(0, (pd_lower - 24) // 4)
    for i in range(nlp):
        word = struct.unpack_from("<I", page, 24 + 4 * i)[0]
        lp_off, lp_flags, lp_len = word & 0x7FFF, (word >> 15) & 0x3, word >> 17
        item = {"lp": i + 1, "flags": lp_flags}
        if lp_flags == LP_NORMAL and lp_len >= 23:
            try:
                item.update(_decode_tuple(page, lp_off, lp_len, cols))
            except (IndexError, ValueError, struct.error):
                item["broken"] = True
        items.append(item)
    return {"lsn": page_lsn(page), "new": False, "items": items}


def read_file_block(path, blk):
    try:
        with open(path, "rb") as f:
            f.seek(blk * BLCKSZ)
            data = f.read(BLCKSZ)
        return data if len(data) == BLCKSZ else None
    except OSError:
        return None


def file_nblocks(path):
    try:
        import os
        return os.path.getsize(path) // BLCKSZ
    except OSError:
        return 0
