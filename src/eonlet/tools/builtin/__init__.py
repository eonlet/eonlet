"""Builtin tools shipped with Eonlet.

Importing this package registers every offline-safe builtin via ``@tool``.
Memory tools (``note`` / ``todo``) replace the legacy ``notes_read`` /
``notes_append`` pair as of MEMORY_SPEC §5 (P2).
"""

from . import (  # noqa: F401
    bash,
    email,
    files,
    forget,
    memory,
    note,
    recall,
    remember,
    schedule,
    skill_tool,
    sleep_tool,
    todo,
    web,
)

__all__: list[str] = []
