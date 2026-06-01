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


class WebError(EonletError):
    """Root of web-subsystem errors (transport, extraction, SSRF)."""


class SSRFRejectedError(WebError):
    """Outbound HTTP target resolves to a forbidden network destination."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason


class UnsupportedSchemeError(WebError):
    """URL scheme outside the allow-list (http / https only)."""

    def __init__(self, url: str, scheme: str) -> None:
        super().__init__(f"{url}: scheme {scheme!r} not allowed")
        self.url = url
        self.scheme = scheme


class ResponseTooLargeError(WebError):
    """Streaming response body exceeded ``max_bytes``."""

    def __init__(self, url: str, max_bytes: int) -> None:
        super().__init__(f"{url}: response exceeded {max_bytes} bytes")
        self.url = url
        self.max_bytes = max_bytes


class HTTPFetchError(WebError):
    """Wraps transport or status errors after retries are exhausted."""

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason


class KnowledgeError(EonletError):
    """Root of knowledge-axis errors (ADR-0005)."""


class KnowledgePathError(KnowledgeError):
    """A knowledge path is absent, malformed, reserved, or escapes the tree root."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path!r}: {reason}")
        self.path = path
        self.reason = reason
