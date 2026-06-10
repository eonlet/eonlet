"""Lineage-aware recorder for LLM request contexts (ADR-0010).

Every request the runtime sends to a provider is appended as one JSON record
to ``trace/context.jsonl``. Requests whose message list *prefix-extends* the
previous one stay on the same **line** and store only the appended suffix
(a ``delta`` record); any rewrite of the prefix — episodic compaction, a
working-window slide, a task-scope switch — starts a fresh line with a full
snapshot (a ``root`` record) carrying a ``parent: {line, seq}`` pointer to
where the old line left off. The fork points are therefore exactly the
context rewrites.

Trace records are observability data, never events (same ruling as token
deltas, SPEC §8.1): they never enter the event store, recording failures
must never break the agent loop, and deleting ``trace/`` is always safe.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..llm.protocol import LLMMessage

log = logging.getLogger("eonlet.trace.recorder")

TRACE_FILENAME = "context.jsonl"


def mint_line_id() -> str:
    """Same id shape as dynamic trigger / task ids (ADR-0002)."""
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"ln-{today}-{os.urandom(2).hex()}"


def _fingerprint(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return sha256(blob.encode("utf-8")).hexdigest()[:16]


def serialize_message(m: LLMMessage) -> dict[str, Any]:
    """Provider-neutral message → stable JSON shape (optional fields elided)."""
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls
        ]
    if m.tool_call_id is not None:
        out["tool_call_id"] = m.tool_call_id
    if m.is_error:
        out["is_error"] = True
    if m.reasoning_content is not None:
        out["reasoning_content"] = m.reasoning_content
    return out


def read_trace(path: Path) -> list[dict[str, Any]]:
    """Parse a trace file, skipping corrupt lines (e.g. a crash mid-append)."""
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("trace %s: skipping corrupt record at line %d", path, i)
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def fold_line(records: list[dict[str, Any]], line_id: str) -> dict[str, Any]:
    """Reconstruct one line's latest full context from its root + deltas.

    Returns ``{"line", "parent", "system", "messages", "hashes", "records"}``
    where ``messages``/``hashes`` are the concatenation up to the line's last
    record and ``system`` is the last non-null system prompt seen on the line.
    ``response`` records stay in ``records`` but never enter the context fold
    — they describe what came *back*, not what was sent.
    """
    own = [r for r in records if r.get("line") == line_id]
    messages: list[dict[str, Any]] = []
    hashes: list[str] = []
    system = ""
    parent: dict[str, Any] | None = None
    for r in own:
        if r.get("kind") == "response":
            continue
        if r.get("kind") == "root":
            messages = list(r.get("messages") or [])
            hashes = list(r.get("hashes") or [])
            parent = r.get("parent")
        else:
            messages.extend(r.get("messages") or [])
            hashes.extend(r.get("hashes") or [])
        if r.get("system") is not None:
            system = r["system"]
    return {
        "line": line_id,
        "parent": parent,
        "system": system,
        "messages": messages,
        "hashes": hashes,
        "records": own,
    }


class ContextTracer:
    """Appends one lineage-aware record per LLM request (ADR-0010).

    Single-writer by construction: the worker's main loop processes one
    trigger at a time, so ``record`` is never called concurrently. On
    construction, the cursor (current line + its cumulative fingerprints)
    is restored by folding the existing file, so a worker restart continues
    the line it left off when the rebuilt context happens to match — and
    forks naturally when it doesn't.
    """

    def __init__(self, trace_dir: Path) -> None:
        self.trace_dir = trace_dir
        self.path = trace_dir / TRACE_FILENAME
        self._seq = 0
        self._line: str | None = None
        self._hashes: list[str] = []
        self._system_hash: str | None = None
        self._request_seq: int | None = None
        self._restore()

    # ── public API ────────────────────────────────────────────────────────

    def record(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        tools: list[dict[str, Any]] | None = None,
        model: str = "",
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Record one outbound request; returns the appended record."""
        serialized = [serialize_message(m) for m in messages]
        hashes = [_fingerprint(s) for s in serialized]
        system_hash = _fingerprint(system)
        self._seq += 1

        extends = (
            self._line is not None
            and len(hashes) >= len(self._hashes)
            and hashes[: len(self._hashes)] == self._hashes
        )
        if extends:
            kind = "delta"
            line = self._line
            parent: dict[str, Any] | None = None
            body = serialized[len(self._hashes) :]
            body_hashes = hashes[len(self._hashes) :]
            # System prompt is versioned within the line, not part of lineage
            # (it is rebuilt every turn by design — e.g. <task_progress>).
            system_out: str | None = system if system_hash != self._system_hash else None
        else:
            kind = "root"
            # The previous record — wherever its line was — is seq-1, because
            # seq counts every record globally.
            parent = {"line": self._line, "seq": self._seq - 1} if self._line else None
            line = mint_line_id()
            body = serialized
            body_hashes = hashes
            system_out = system

        rec: dict[str, Any] = {
            "seq": self._seq,
            "ts": datetime.now(UTC).isoformat(),
            "line": line,
            "parent": parent,
            "kind": kind,
            "model": model,
            "task_id": task_id,
            "n_messages": len(serialized),
            "system_hash": system_hash,
            "system": system_out,
            "tools_hash": _fingerprint(tools or []),
            "messages": body,
            "hashes": body_hashes,
        }
        self._append(rec)
        self._line = line
        self._hashes = hashes
        self._system_hash = system_hash
        self._request_seq = self._seq
        return rec

    def record_response(self, message: LLMMessage) -> dict[str, Any] | None:
        """Record the assistant reply to the last recorded request.

        A ``response`` record is pure observability glue: it carries the reply
        that would otherwise only surface in the *next* request's delta — and
        never surfaces at all for the final turn of a run. It does not touch
        the lineage state (``_hashes``), so it can never cause a fork; viewers
        dedupe the same reply out of the following delta by ``hash``.
        """
        if self._line is None or self._request_seq is None:
            return None  # no request on file to attach to
        serialized = serialize_message(message)
        self._seq += 1
        rec: dict[str, Any] = {
            "seq": self._seq,
            "ts": datetime.now(UTC).isoformat(),
            "line": self._line,
            "kind": "response",
            "for_seq": self._request_seq,
            "message": serialized,
            "hash": _fingerprint(serialized),
        }
        self._append(rec)
        return rec

    # ── internal ──────────────────────────────────────────────────────────

    def _restore(self) -> None:
        records = read_trace(self.path)
        if not records:
            return
        self._seq = max(int(r.get("seq") or 0) for r in records)
        # Anchor the cursor on the last *request* record — a trailing response
        # carries no line context or system hash of its own.
        last_req = next((r for r in reversed(records) if r.get("kind") in ("root", "delta")), None)
        if last_req is None:
            return
        line_id = last_req.get("line")
        if not isinstance(line_id, str):
            return
        folded = fold_line(records, line_id)
        self._line = line_id
        self._hashes = list(folded["hashes"])
        sys_hash = last_req.get("system_hash")
        self._system_hash = sys_hash if isinstance(sys_hash, str) else None
        seq = last_req.get("seq")
        self._request_seq = seq if isinstance(seq, int) else None

    def _append(self, rec: dict[str, Any]) -> None:
        # Plain append, not atomic_write_text: this is an append-only log like
        # the SQLite event store, not a rewritten memory document. A torn tail
        # from a crash is tolerated by read_trace (the record is skipped).
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
