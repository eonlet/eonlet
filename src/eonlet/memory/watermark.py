"""Compaction watermark (MEMORY_SPEC §4.2).

Single integer per eonlet, stored as text in ``memory/watermark``. Reads are
liberal (missing or unparseable file → 0, the safe fallback that causes the
runtime to replay everything as raw history). Writes go through the atomic
write helper.
"""

from __future__ import annotations

from pathlib import Path

from .paths import watermark_path
from .storage import atomic_write_text


def read_watermark(memory_dir: Path) -> int:
    """Return the current watermark, or 0 if missing/unparseable.

    The 0 fallback is deliberate: a missing watermark means the runtime has
    no compacted history yet, so all events are still raw — the recent
    window can include any of them. This is the same state as a freshly
    created eonlet.
    """
    p = watermark_path(memory_dir)
    if not p.exists():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return 0


def write_watermark(memory_dir: Path, value: int) -> None:
    """Persist a new watermark value.

    Callers MUST enforce monotonicity (invariant M-I2); the helper itself
    is permissive so it can be used by the migration tool and by recovery
    paths that need to reset state.
    """
    if value < 0:
        raise ValueError(f"watermark must be non-negative, got {value}")
    atomic_write_text(watermark_path(memory_dir), str(value) + "\n")
