"""Global tool registry.

A single in-process registry holds every loaded tool instance. Builtins are
registered at module import (via ``@tool``); custom tools are registered when
their file is imported by ``runtime/definition.py``.

Per AGENT_CONFIG_SPEC §6 loader priority: builtin first, then custom (custom
may overshadow if the author wants — we warn but allow).
"""

from __future__ import annotations

import warnings
from typing import Any

from .protocol import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool_instance: Tool) -> None:
        name = tool_instance.name
        if name in self._tools:
            warnings.warn(f"Tool {name!r} re-registered (custom shadows builtin?)", stacklevel=2)
        self._tools[name] = tool_instance

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as e:
            raise KeyError(f"Unknown tool: {name!r}") from e

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def select(self, names: list[str]) -> list[Tool]:
        """Return tools matching the requested name list, preserving order."""
        out: list[Tool] = []
        for n in names:
            if n in self._tools:
                out.append(self._tools[n])
            else:
                raise KeyError(f"Tool {n!r} not registered")
        return out

    def schemas(self, names: list[str]) -> list[dict[str, Any]]:
        """JSON Schema for each named tool, in provider-neutral form.

        Returns [{name, description, input_schema}, ...] — provider modules map
        this into Anthropic or OpenAI tool specs.
        """
        out: list[dict[str, Any]] = []
        for t in self.select(names):
            out.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema.model_json_schema(),
                    "annotations": t.annotations.model_dump(),
                }
            )
        return out


_registry: ToolRegistry | None = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


def reset_registry() -> None:
    """Test helper — drop all registered tools."""
    global _registry
    _registry = ToolRegistry()
