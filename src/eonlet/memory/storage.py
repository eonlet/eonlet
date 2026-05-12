"""Atomic writes and per-file locks for memory documents.

Per MEMORY_SPEC §2 (I-S2 / I-S3 / I-S4): the worker is the only writer; every
write is temp-then-rename so a half-written file is never observable; one
``anyio.Lock`` per file path (not one global lock) so that, e.g., recall
queries don't block LTM writes.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio

# ── Per-path lock registry ──────────────────────────────────────────────────
#
# Locks are keyed by the resolved absolute path. The registry itself is
# guarded by a small synchronous lock; the per-file locks it hands out are
# `anyio.Lock` instances which the caller awaits.
#
# Locks are intentionally process-local. Cross-process locking is unnecessary
# in the v0.1 architecture — the worker is the single writer per eonlet
# (MEMORY_SPEC §2 I-S2), and CLI commands route through the worker over IPC.

_locks: dict[str, anyio.Lock] = {}
_locks_guard = anyio.Lock()


def _lock_key(path: Path) -> str:
    # Pure-string normalization; no filesystem access. Callers MUST use
    # consistent path forms (the runtime always passes absolute paths from
    # ``memory.paths`` helpers, so this holds in practice).
    return os.path.normpath(os.fspath(path))


async def _get_or_create_lock(key: str) -> anyio.Lock:
    async with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = anyio.Lock()
            _locks[key] = lock
        return lock


@asynccontextmanager
async def file_lock(path: Path) -> AsyncIterator[None]:
    """Acquire the per-path lock for the duration of the context."""
    lock = await _get_or_create_lock(_lock_key(path))
    async with lock:
        yield


# ── Atomic writes ───────────────────────────────────────────────────────────


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via write-temp-then-rename.

    Synchronous because file I/O on the small documents memory writes
    (typically < 64 KB) is dominated by ``fsync`` latency, not by event-loop
    blocking. Callers that want non-blocking semantics can wrap in
    ``anyio.to_thread.run_sync``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile is awkward here because we need the temp to be in
    # the same directory as the target (rename is only atomic on one
    # filesystem). Use a deterministic suffix instead.
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)  # atomic on POSIX and Windows
    except Exception:
        # Best effort cleanup; never mask the real error.
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))
