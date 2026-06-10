"""Self-contained HTML viewer for a context trace (ADR-0010).

``render_html`` turns the parsed ``trace/context.jsonl`` records into one
static HTML file — embedded JSON data + vanilla JS/CSS, no dependencies, no
server. The browser does what the terminal can't: fold/unfold multi-thousand-
token contexts, jump between fork points, and diff a line against where it
forked from. This mirrors the claude-trace deliverable (a single HTML file
you open locally) while staying inside the project's minimal-deps rule.

Rendering layout per line: system-prompt versions live in their own section
at the top (one collapsible entry per change), the conversation below shows
each call's delta with the reply (``response`` record) inline — deduped by
hash against the next delta — and tool results nested under the tool call
they answer.

The only subtle part is embedding: record content is user/model text and may
contain ``</script>``. The JSON payload therefore escapes every ``</`` as
``<\\/`` (legal, identical JSON) so the data block can never terminate the
script element. All rendering uses ``textContent`` — nothing from a record is
ever interpreted as HTML.
"""

from __future__ import annotations

import json
from html import escape
from typing import Any


def render_html(records: list[dict[str, Any]], *, title: str = "context trace") -> str:
    """Render trace records into a self-contained HTML page."""
    payload = json.dumps(records, ensure_ascii=False).replace("</", "<\\/")
    return _PAGE.replace("__TITLE__", escape(title)).replace("__DATA__", payload)


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { --bg:#14161a; --panel:#1d2026; --line:#2a2e36; --fg:#d6d9de; --dim:#8a8f98;
        --user:#3b82f6; --assistant:#10b981; --tool:#f59e0b; --err:#ef4444; --accent:#7aa2f7; }
* { box-sizing:border-box; }
body { margin:0; display:flex; height:100vh; background:var(--bg); color:var(--fg);
       font:14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
#side { width:360px; min-width:260px; overflow:auto; background:var(--panel);
        border-right:1px solid var(--line); padding:14px; flex-shrink:0; }
#main { flex:1; overflow:auto; padding:18px 28px 60px; }
h1 { font-size:15px; margin:0 0 4px; }
.dim { color:var(--dim); font-size:12px; }
#stats { color:var(--dim); font-size:12px; margin-bottom:14px; }
.ln { padding:7px 9px; border-radius:6px; cursor:pointer; margin:2px 0; border:1px solid transparent; }
.ln:hover { background:#262a32; }
.ln.active { border-color:var(--accent); background:#262a32; }
.ln .id { color:var(--accent); }
.ln .meta { color:var(--dim); font-size:11.5px; }
.fork { border-left:1px dashed var(--line); margin-left:11px; padding-left:9px; }
.syssec { border:1px solid var(--line); border-radius:6px; background:var(--panel);
          padding:8px 12px; margin:14px 0 4px; }
.syssec .sechdr { font-size:11.5px; color:var(--dim); text-transform:uppercase;
                  letter-spacing:.04em; }
.sep { display:flex; align-items:center; gap:8px; color:var(--dim); font-size:11.5px; margin:22px 0 8px; }
.sep::after { content:""; flex:1; border-top:1px dashed var(--line); }
.sep b { color:var(--fg); }
.msg { border:1px solid var(--line); border-left-width:3px; border-radius:6px;
       padding:8px 12px; margin:8px 0; background:var(--panel); }
.msg .hdr { font-size:11.5px; color:var(--dim); margin-bottom:4px;
            text-transform:uppercase; letter-spacing:.04em; }
.msg pre, details pre { margin:0; white-space:pre-wrap; word-break:break-word; }
.msg.user { border-left-color:var(--user); }
.msg.assistant { border-left-color:var(--assistant); }
.msg.tool { border-left-color:var(--tool); }
.msg.error { border-left-color:var(--err); }
.msg.reply { border-style:dashed; border-left-style:solid; }
.tc { margin-top:6px; padding:6px 8px; background:#11131a; border-radius:5px;
      font-size:12.5px; white-space:pre-wrap; word-break:break-word; }
.tc .name { color:var(--tool); }
.tr { border-top:1px dashed var(--line); margin-top:6px; padding-top:6px; }
.tr .trh { font-size:11px; color:var(--dim); text-transform:uppercase; letter-spacing:.04em; }
.tr.err .trh { color:var(--err); }
details { margin:6px 0; }
summary { cursor:pointer; color:var(--dim); font-size:12.5px; }
details > pre { background:#11131a; border:1px solid var(--line); border-radius:6px;
                padding:8px 12px; margin-top:6px; }
.parentlink { color:var(--accent); cursor:pointer; text-decoration:underline; }
#linehdr { margin-bottom:6px; }
.note { color:var(--dim); font-size:12px; margin:6px 0; }
</style>
</head>
<body>
<div id="side">
  <h1>__TITLE__</h1>
  <div id="stats"></div>
  <div id="tree"></div>
</div>
<div id="main"><div id="linehdr"></div><div id="content"></div></div>
<script>
const RECORDS = __DATA__;
const isRequest = (r) => r.kind === "root" || r.kind === "delta";

// ── group records into lines, derive the fork tree ──────────────────────────
const byLine = new Map();
for (const r of RECORDS) {
  if (!byLine.has(r.line)) byLine.set(r.line, []);
  byLine.get(r.line).push(r);
}
const children = new Map(), roots = [];
for (const [ln, recs] of byLine) {
  const p = recs[0].parent;
  if (p && p.line && byLine.has(p.line)) {
    if (!children.has(p.line)) children.set(p.line, []);
    children.get(p.line).push(ln);
  } else {
    roots.push(ln);
  }
}
const nCalls = RECORDS.filter(isRequest).length;
document.getElementById("stats").textContent =
  nCalls + " calls \\u00b7 " + byLine.size + " lines";

// ── tiny DOM helpers (textContent only — record data is never HTML) ─────────
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function fmtTs(ts) { return (ts || "").slice(0, 19).replace("T", " "); }
function lineInfo(ln) {
  const recs = byLine.get(ln);
  const reqs = recs.filter(isRequest);
  const first = recs[0], last = recs[recs.length - 1];
  const lastReq = reqs.length ? reqs[reqs.length - 1] : last;
  const span = first.seq === last.seq
    ? "seq " + first.seq
    : "seq " + first.seq + "-" + last.seq;
  return { recs, reqs, first, last, lastReq, span };
}

// ── sidebar tree ─────────────────────────────────────────────────────────────
function buildTree(ln, container) {
  const { reqs, first, lastReq, span } = lineInfo(ln);
  const node = el("div", "ln");
  node.id = "side-" + ln;
  node.appendChild(el("div", "id", ln));
  let meta = reqs.length + " call(s) \\u00b7 " + span + " \\u00b7 "
           + lastReq.n_messages + " msgs \\u00b7 " + fmtTs(first.ts);
  if (lastReq.task_id) meta += " \\u00b7 task " + lastReq.task_id;
  node.appendChild(el("div", "meta", meta));
  node.onclick = () => show(ln);
  container.appendChild(node);
  const kids = children.get(ln) || [];
  if (kids.length) {
    const sub = el("div", "fork");
    container.appendChild(sub);
    for (const k of kids) buildTree(k, sub);
  }
}
const tree = document.getElementById("tree");
for (const r of roots) buildTree(r, tree);

// ── line view ────────────────────────────────────────────────────────────────
function renderToolResult(m, chip) {
  const wrap = el("div", "tr" + (m.is_error ? " err" : ""));
  const content = m.content || "";
  wrap.appendChild(el("div", "trh", "result" + (m.is_error ? " \\u00b7 ERROR" : "")));
  if (content.length > 1200) {
    const d = el("details");
    d.appendChild(el("summary", null, content.length + " chars (click to expand)"));
    d.appendChild(el("pre", null, content));
    wrap.appendChild(d);
  } else if (content) {
    wrap.appendChild(el("pre", null, content));
  }
  chip.appendChild(wrap);
}

function renderMessage(m, idx, chips, replyFor) {
  const box = el("div", "msg " + (m.is_error ? "error" : m.role)
                        + (replyFor ? " reply" : ""));
  let hdr = "#" + idx + " " + m.role;
  if (replyFor) hdr += " \\u00b7 reply to seq " + replyFor;
  if (m.tool_call_id) {
    hdr += " \\u00b7 result for " + m.tool_call_id + (m.is_error ? " \\u00b7 ERROR" : "");
  }
  box.appendChild(el("div", "hdr", hdr));
  if (m.reasoning_content) {
    const d = el("details");
    d.appendChild(el("summary", null, "reasoning (" + m.reasoning_content.length + " chars)"));
    d.appendChild(el("pre", null, m.reasoning_content));
    box.appendChild(d);
  }
  const content = m.content || "";
  const isResult = m.role === "tool" || m.tool_call_id;
  if (content.length > 1200 && isResult) {
    const d = el("details");
    d.appendChild(el("summary", null, content.length + " chars (click to expand)"));
    d.appendChild(el("pre", null, content));
    box.appendChild(d);
  } else if (content) {
    box.appendChild(el("pre", null, content));
  }
  for (const tc of m.tool_calls || []) {
    const t = el("div", "tc");
    t.appendChild(el("span", "name", tc.name));
    t.appendChild(document.createTextNode("(" + JSON.stringify(tc.arguments) + ")"));
    box.appendChild(t);
    if (tc.id) chips.set(tc.id, t);
  }
  return box;
}

let active = null;
function show(ln) {
  if (active) {
    const prev = document.getElementById("side-" + active);
    if (prev) prev.classList.remove("active");
  }
  active = ln;
  const side = document.getElementById("side-" + ln);
  if (side) side.classList.add("active");

  const hdr = document.getElementById("linehdr");
  const content = document.getElementById("content");
  hdr.textContent = "";
  content.textContent = "";

  const { recs, reqs, first, last, lastReq, span } = lineInfo(ln);
  hdr.appendChild(el("h1", null, ln));
  const meta = el("div", "dim");
  meta.append(reqs.length + " call(s) \\u00b7 " + span + " \\u00b7 model "
              + (lastReq.model || "?") + " \\u00b7 " + fmtTs(first.ts) + " \\u2192 " + fmtTs(last.ts));
  const p = recs[0].parent;
  if (p && p.line) {
    meta.append(" \\u00b7 ");
    const a = el("span", "parentlink", "forked from " + p.line + " @ seq " + p.seq);
    a.onclick = () => show(p.line);
    meta.appendChild(a);
  }
  hdr.appendChild(meta);

  // System prompt versions — their own section, outside the conversation.
  const sysVersions = recs.filter((r) => r.system != null);
  if (sysVersions.length) {
    const sec = el("div", "syssec");
    sec.appendChild(el("div", "sechdr",
                       "system prompt \\u00b7 " + sysVersions.length + " version(s)"));
    sysVersions.forEach((r, i) => {
      const d = el("details");
      d.appendChild(el("summary", null, "v" + (i + 1) + " @ seq " + r.seq
                                        + " \\u00b7 " + r.system.length + " chars"));
      d.appendChild(el("pre", null, r.system));
      sec.appendChild(d);
    });
    content.appendChild(sec);
  }

  // Conversation: deltas + inline replies; tool results nest under their call.
  const chips = new Map();      // tool_call_id → .tc element
  let lastReplyHash = null;     // dedupe the reply out of the next delta
  let idx = 0;
  for (const r of recs) {
    if (r.kind === "response") {
      idx += 1;
      content.appendChild(renderMessage(r.message, idx, chips, r.for_seq));
      lastReplyHash = r.hash;
      continue;
    }
    const sep = el("div", "sep");
    const lbl = el("span");
    lbl.append("call ");
    lbl.appendChild(el("b", null, "seq " + r.seq));
    let txt = " \\u00b7 " + r.kind + " \\u00b7 " + fmtTs(r.ts)
            + " \\u00b7 context " + r.n_messages + " msgs";
    if (r.system != null && r.kind === "delta") txt += " \\u00b7 system updated";
    lbl.append(txt);
    sep.appendChild(lbl);
    content.appendChild(sep);
    const msgs = r.messages || [], hs = r.hashes || [];
    msgs.forEach((m, i) => {
      if (r.kind === "delta" && lastReplyHash && hs[i] === lastReplyHash) {
        lastReplyHash = null;   // already on screen as the reply above
        return;
      }
      if (m.tool_call_id && chips.has(m.tool_call_id)) {
        renderToolResult(m, chips.get(m.tool_call_id));
        return;
      }
      idx += 1;
      content.appendChild(renderMessage(m, idx, chips));
    });
    if (!msgs.length) {
      content.appendChild(el("div", "note", "(no new messages \\u2014 identical context)"));
    }
  }
}
if (RECORDS.length) show(RECORDS[RECORDS.length - 1].line);
</script>
</body>
</html>
"""
