"""ID minting for memory records.

Note and todo IDs follow the same shape as dynamic trigger IDs (ADR-0002):
``<prefix>-<YYYY-MM-DD>-<4hex>``. The date keeps IDs sortable when listing;
the random suffix avoids collisions within a single day.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _suffix() -> str:
    return os.urandom(2).hex()


def mint_note_id() -> str:
    return f"note-{_today()}-{_suffix()}"


def mint_todo_id() -> str:
    return f"todo-{_today()}-{_suffix()}"
