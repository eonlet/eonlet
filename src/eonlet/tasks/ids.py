"""ID minting for tasks.

Same shape as dynamic trigger / memory IDs (ADR-0002):
``task-<YYYY-MM-DD>-<4hex>`` — the date keeps IDs sortable; the random suffix
avoids same-day collisions.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime


def mint_task_id() -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"task-{today}-{os.urandom(2).hex()}"
