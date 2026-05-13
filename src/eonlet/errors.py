"""Exception hierarchy. Per SPEC §15: never raise plain Exception."""

from __future__ import annotations


class EonletError(Exception):
    """Root of every Eonlet-raised exception."""


class ConfigError(EonletError):
    """`agent.yaml` or `config.yaml` is invalid."""


class DefinitionNotFoundError(EonletError):
    """Agent definition directory missing."""


class EonletNotFoundError(EonletError):
    """Eonlet instance directory missing."""


class EonletAlreadyExistsError(EonletError):
    """Eonlet instance with given id already exists."""


class EonletNotRunningError(EonletError):
    """Operation requires a live worker, but none is running."""


class PermissionDeniedError(EonletError):
    """A tool call was rejected by the permission gate."""

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"{tool_name}: {reason}")
        self.tool_name = tool_name
        self.reason = reason


class ToolError(EonletError):
    """A tool failed during execution. Distinct from PermissionDeniedError."""


class LLMError(EonletError):
    """LLM provider failure."""


class BudgetExceededError(EonletError):
    """Budget cap hit."""


class IPCError(EonletError):
    """JSON-RPC framing / transport error."""
