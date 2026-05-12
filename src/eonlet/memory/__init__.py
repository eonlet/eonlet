"""Memory subsystem (MEMORY_SPEC.md / ADR-0003).

P1 scope: package skeleton, configuration, storage primitives, watermark,
and event kinds. Notes/TODOs/recall/compaction land in P2-P5.
"""

from __future__ import annotations

from .config import (
    ConversationMemoryConfig,
    MemoryConfig,
    NotesMemoryConfig,
    TodosMemoryConfig,
)
from .paths import (
    index_db_path,
    long_term_path,
    memory_root,
    notes_path,
    short_term_path,
    todos_path,
    watermark_path,
)
from .recall import IndexedMsg, RecallIndex
from .storage import (
    atomic_write_bytes,
    atomic_write_text,
    file_lock,
)
from .watermark import read_watermark, write_watermark

__all__ = [
    "ConversationMemoryConfig",
    "IndexedMsg",
    "MemoryConfig",
    "NotesMemoryConfig",
    "RecallIndex",
    "TodosMemoryConfig",
    "atomic_write_bytes",
    "atomic_write_text",
    "file_lock",
    "index_db_path",
    "long_term_path",
    "memory_root",
    "notes_path",
    "read_watermark",
    "short_term_path",
    "todos_path",
    "watermark_path",
    "write_watermark",
]
