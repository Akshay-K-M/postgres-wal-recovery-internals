"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const TX_COLORS = ["#4f46e5", "#0e7490", "#a21caf", "#b45309", "#be123c", "#4d7c0f", "#1d4ed8", "#7c2d12"];
const EXAMPLE = $("script").value;
let current = null;

function csrf() {
  const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}
async function api(path, body) {
  $("busy").hidden = false;
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
                                  body: JSON.stringify(body || {}) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  } finally { $("busy").hidden = true; }
}

/* ------------------------------------------------------------ editor */
$("example").onclick = () => { $("script").value = EXAMPLE; };
$("run").onclick = async () => {
  $("ed-error").textContent = "";
  $("run").disabled = true; $("run").textContent = "Starting PostgreSQL…";
  try {
    const data = await api("/api/start", { script: $("script").value });
    $("editor").hidden = true; $("viewer").hidden = false;
    show(data);
  } catch (e) { $("ed-error").textContent = e.message; }
  finally { $("run").disabled = false; $("run").textContent = "Run scenario →"; }
};
$("edit").onclick = async () => {
  await api("/api/stop").catch(() => {});
  $("viewer").hidden = true; $("editor").hidden = false;
};

/* ------------------------------------------------------------ stepping */
async function step(dir) {
  if (dir === "next" && $("next").disabled) return;
  if (dir === "prev" && $("prev").disabled) return;
  try { show(await api("/api/step", { dir })); }
  catch (e) { $("tip").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}
$("next").onclick = () => step("next");
$("prev").onclick = () => step("prev");
document.addEventListener("keydown", e => {
  if ($("viewer").hidden || e.target.tagName === "TEXTAREA") return;
  if (["ArrowRight", " ", "PageDown"].includes(e.key)) { e.preventDefault(); step("next"); }
  if (["ArrowLeft", "PageUp"].includes(e.key)) { e.preventDefault(); step("prev"); }
});

/* ------------------------------------------------------------ render */
function txChip(t, labels) {
  if (!t) return "";
  const i = labels.indexOf(t.name);
  const bg = i >= 0 ? TX_COLORS[i % TX_COLORS.length] : "#6b7280";
  return `<span class="tx" style="background:${bg}" title="xid ${t.xid}">${esc(t.name)}</span>`;
}

function show(data) {
  current = data;
  const v = data.view;
  $("count").textContent = `step ${data.index + 1} / ${data.count}${data.can_next && data.index === data.count - 1 ? "+" : ""}`;
  $("prev").disabled = data.index === 0;
  $("next").disabled = !data.can_next;
  $("next").textContent = data.can_next ? "Next ▶" : "Done";

  const srv = { up: ["up", "Server running"], down: ["down", "Server crashed"], recovering: ["recovering", "Recovering · paused in gdb"] }[v.server];
  $("server").className = "server " + srv[0]; $("server").textContent = srv[1];

  renderLines(data.script, v.line, v.kind);
  document.querySelector(".story").classList.toggle("error", v.error);
  $("caption").textContent = v.caption;
  $("title").textContent = v.title;
  $("tip").textContent = v.blocked ? "Still running: this statement is waiting for a lock. " + v.tip : v.tip;
  $("changes").innerHTML = v.changes.map(c => `<li>${esc(c)}</li>`).join("");
  $("code").textContent = v.code ? "code path: " + v.code : "";
  $("output").textContent = v.output || "(no output)";
  $("outbox").open = v.error;

  renderTables(v);
  renderWal(v);
  renderClog(v);
  renderProcArray(v);
}

function renderLines(script, line, kind) {
  const lines = script.split("\n");
  $("lines").innerHTML = lines.map((t, i) => {
    const n = i + 1;
    if (!t.trim()) return "";
    const cls = [t.trim().startsWith("--") ? "comment" : "", line && n < line ? "done" : "", n === line ? "cur" : "",
                 /^\s*crash\s*;?\s*$/i.test(t) ? "crashline" : ""].join(" ");
    return `<li class="${cls}" data-n="${n}">${esc(t)}</li>`;
  }).join("");
  const cur = $("lines").querySelector(".cur");
  if (cur) cur.scrollIntoView({ block: "nearest" });
}

const MEM_TEXT = { present: "in memory", wiped: "memory wiped by the crash", notloaded: "not read into memory yet", absent: "not in memory" };
const FILE_TEXT = { present: "in the table file", blank: "table file: blank page", none: "not in the table file yet" };
const CHIP_WAL = { disk: ["on", "its WAL record is on disk"], mem: ["pend", "its WAL record is only in memory"], lost: ["lost", "its WAL record was lost in the crash"] };
const CHIP_DISK = { yes: ["on", "DISK", "this version is in the table file"], no: ["no", "DISK", "not in the table file yet (only in memory)"],
                    stale: ["stale", "DISK", "the table file still has the old version"], freed: ["freed", "DISK", "the slot is free in the table file too"] };

function renderTables(v) {
  if (!v.tables.length) { $("tables").innerHTML = '<p class="empty">No tables yet.</p>'; return; }
  $("tables").innerHTML = v.tables.map(t => {
    const pages = t.pages.length ? t.pages.map(p => {
      const mem = `<span class="where ${p.mem === "wiped" ? "gone" : p.dirty ? "dirty" : ""}">${MEM_TEXT[p.mem]}${p.mem === "present" && p.dirty ? " · newer than disk" : ""}</span>`;
      const file = `<span class="where">${FILE_TEXT[p.file]}</span>`;
      const head = `<div class="pagehead"><b>${esc(t.name)}</b><span class="muted">page ${p.blk}</span>${mem}${file}</div>`;
      if (!p.rows.length) return head + '<p class="empty">No rows on this page.</p>';
      const rows = p.rows.map(r => {
        const vals = t.cols.map((_, i) => `<td class="val">${r.values ? esc(r.values[i] ?? "NULL") : "?"}</td>`).join("");
        const vis = r.visible === true ? '<span class="vis-yes">✓</span>' : r.visible === false ? '<span class="vis-no">–</span>' : '<span class="vis-no">·</span>';
        const w = r.wal && CHIP_WAL[r.wal] && !["removed", "redirect"].includes(r.status) ? `<span class="chip ${CHIP_WAL[r.wal][0]}" title="${CHIP_WAL[r.wal][1]}">WAL</span>` : "";
        const d = CHIP_DISK[r.file] ? `<span class="chip ${CHIP_DISK[r.file][0]}" title="${CHIP_DISK[r.file][2]}">DISK</span>` : "";
        const who = r.xmax ? txChip(r.xmin, v.labels) + " " + txChip(r.xmax, v.labels) : txChip(r.xmin, v.labels);
        return `<tr class="${r.status} ${r.chg ? "chg-" + r.chg : ""}">
          <td class="slot">${r.lp}</td>${vals}<td>${who}</td><td class="st">${esc(r.text)}</td>
          <td class="c">${vis}</td><td class="stored">${w}${d}</td></tr>`;
      }).join("");
      return head + `<table class="rows"><thead><tr><th>slot</th>${t.cols.map(c => `<th>${esc(c)}</th>`).join("")}
        <th>by</th><th>status</th><th class="c">visible</th><th>stored</th></tr></thead><tbody>${rows}</tbody></table>`;
    }).join("") : `<div class="pagehead"><b>${esc(t.name)}</b><span class="where">no pages yet</span></div>`;
    return `<div class="tbl">${pages}${t.nblocks > 4 ? `<p class="muted">+${t.nblocks - 4} more pages not shown</p>` : ""}</div>`;
  }).join("");
}

const KIND_ICON = { insert: "+", update: "~", delete: "−", commit: "✓", abort: "✕", ckpt: "⟲", prune: "⌫", lock: "🔒", visible: "◇", other: "·" };
function renderWal(v) {
  let html = "", memShown = false, lostShown = false, focus = null;
  for (const w of v.wal) {
    if (w.state === "mem" && !memShown) { html += '<div class="divider">▲ on disk · ▼ only in memory</div>'; memShown = true; }
    if (w.state === "lost" && !lostShown) { html += '<div class="divider lost">lost in the crash · never flushed</div>'; lostShown = true; }
    const cls = ["w", w.state, w.replay || "", w.chg ? "chg-" + w.chg : ""].join(" ");
    if (w.replay === "now" || w.chg) focus = w.key;
    html += `<div class="${cls}" data-key="${esc(w.key)}">
      <span class="ico k-${w.kind}">${KIND_ICON[w.kind] || "·"}</span>
      <span class="txt" title="${esc(w.text)}">${esc(w.text)}</span>
      <span class="right">${txChip(w.tx, v.labels)}${w.tags.map(t => `<span class="tag ${t === "REDO pointer" ? "redo" : ""}">${esc(t)}</span>`).join("")}</span>
      <span class="meta">LSN ${esc(w.lsn)}${w.state === "mem" ? " · not flushed" : ""}</span>
    </div>`;
  }
  $("wal").innerHTML = html || '<p class="empty">No WAL yet.</p>';
  $("walnote").textContent = `insert ${v.lsn.insert} · flushed ${v.lsn.flush} · REDO ${v.lsn.redo}` +
    (v.wal_hidden ? ` · ${v.wal_hidden} catalog/other records hidden` : "");
  const el = focus && $("wal").querySelector(`[data-key="${CSS.escape(focus)}"]`);
  const box = $("wal");
  if (el) box.scrollTop = el.offsetTop - box.offsetTop - box.clientHeight / 2;
  else box.scrollTop = box.scrollHeight;
}

function cell(x, kind) {
  if (!x) return `<td class="cell gone">${kind === "mem" ? "wiped" : "—"}</td>`;
  const k = x.crashed ? "crashed" : x.text === "committed" ? "committed" : x.text === "aborted" ? "aborted" : x.text === "in progress" ? "inprog" : "";
  return `<td class="cell ${k}"><span class="bits"><i>${esc(x.code[0])}</i><i>${esc(x.code[1])}</i></span>
    <small>${esc(x.text)}${x.note ? " · " + esc(x.note) : ""}</small></td>`;
}
function renderClog(v) {
  if (!v.clog.length) { $("clog").innerHTML = '<p class="empty">No transaction has written anything yet.</p>'; return; }
  $("clog").innerHTML = `<table class="clog"><thead><tr><th>tx</th><th>memory</th><th>disk</th></tr></thead><tbody>` +
    v.clog.map(c => `<tr class="${c.chg ? "chg-chg" : ""}"><td>${txChip(c, v.labels)}<div class="xid">xid ${c.xid}</div></td>
      ${cell(c.mem, "mem")}${cell(c.disk, "disk")}</tr>` +
      (c.lag ? `<tr><td></td><td colspan="2" class="cell"><small class="lag">memory is ahead of the pg_xact file</small></td></tr>` : "")).join("") +
    "</tbody></table>";
}

function renderProcArray(v) {
  const p = v.procarray || {entries: [], source: "unavailable"};
  const entries = p.entries.filter(r => r.xid != null);
  const empty = p.source === "wiped" ? "Crash: the old ProcArray is gone." :
    p.source === "unavailable" ? "ProcArray unavailable. Start a new scenario." :
    p.source === "uninitialized" ? "ProcArray not initialized yet." :
    "No running scenario transactions with an assigned XID.";
  const rows = entries.map(r => `<tr class="${r.chg ? "chg-chg" : ""}">
    <td class="xid">${esc(r.pid)}</td><td class="xid">${esc(r.xid)}</td></tr>`).join("");
  $("procarray").innerHTML = rows ? `<table class="clog"><thead><tr>
    <th>PID</th><th>XID</th>
    </tr></thead><tbody>${rows}</tbody></table>` : `<p class="empty">${empty}</p>`;
  $("procarray-note").textContent =
    "Only assigned transaction IDs are shown. After crash recovery: no COMMIT + no running transaction = treated as aborted, even if CLOG still says 00 (in progress).";
}
