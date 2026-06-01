"""Builtin tools shipped with Eonlet.

Importing this package registers every offline-safe builtin via ``@tool``.
The durable-knowledge write surface is the single ``knowledge`` tool
(ADR-0005); it replaces the retired ``remember`` / ``note`` / ``forget`` trio.
"""

from . import (  # noqa: F401
    bash,
    email,
    files,
    knowledge,
    memory,
    recall,
    schedule,
    skill_tool,
    sleep_tool,
    task,
    web,
)

__all__: list[str] = []
