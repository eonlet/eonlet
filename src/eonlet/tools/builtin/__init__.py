"""Builtin tools shipped with Eonlet.

Importing this package registers all 9 offline-safe builtins via ``@tool``.
Network-bound builtins (``web_search``, ``web_fetch``, ``send_email``) are
deferred to v0.0.2.
"""

from . import bash, email, files, notes, skill_tool, sleep_tool, web  # noqa: F401

__all__: list[str] = []
