"""Permission gate.

Per SPEC §7.7 and TOOL_SPEC §7:

1. Hardcoded deny (always enforced; cannot be overridden).
2. User-extended deny (``agent.yaml.permissions.extra_deny``).
3. Mode check:
   - ``yolo``: allowed (unless tool has ``requires_confirmation: true``).
   - ``ask``: destructive tools need an attached session to confirm; otherwise denied.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..tools.protocol import Tool

Mode = Literal["ask", "yolo"]


# Hardcoded deny list — cannot be overridden (TOOL_SPEC §7 / SPEC §7.7).
# Encoded as (tool_name, glob_pattern_over_input_repr).
HARDCODED_DENY: list[tuple[str, str]] = [
    ("bash", "rm -rf /*"),
    ("bash", "rm -rf ~*"),
    ("bash", ":(){*"),
    ("bash", "sudo*"),
    ("bash", "curl * | sh*"),
    ("bash", "wget * | sh*"),
    ("file_write", "/etc/**"),
    ("file_write", "~/.ssh/**"),
    ("file_write", "~/.aws/**"),
    ("file_write", "~/.eonlet/**"),
    ("file_edit", "/etc/**"),
    ("file_edit", "~/.ssh/**"),
    ("file_edit", "~/.aws/**"),
]


@dataclass(slots=True)
class Decision:
    allowed: bool
    reason: str
    rule: str  # e.g. "hardcoded_deny", "ask_no_session", "yolo", "default_allow"


# Pattern syntax: `Tool(pattern)` where pattern is a glob.
_USER_PATTERN_RE = re.compile(r"^([A-Za-z_]\w*)\((.*)\)$")


class PermissionGate:
    """Stateful per-eonlet permission evaluator."""

    def __init__(
        self,
        mode: Mode,
        extra_deny: list[str],
        *,
        session_attached: bool = False,
    ) -> None:
        self.mode: Mode = mode
        self.extra_deny = list(extra_deny)
        self.session_attached = session_attached

    def evaluate(self, tool_instance: Tool, args: Any) -> Decision:
        """Decide whether ``tool_instance(args)`` may run."""
        name = tool_instance.name
        ann = tool_instance.annotations
        repr_str = _input_repr(name, args)

        # 1. Hardcoded deny.
        for t, pat in HARDCODED_DENY:
            if t == name and _match(repr_str, pat):
                return Decision(False, f"matches hardcoded deny {t}({pat})", "hardcoded_deny")

        # 2. User extra deny. Patterns are conventionally title-cased
        # (``Bash(...)``, ``FileWrite(...)``) but tool names are snake_case —
        # normalize by stripping case and underscores on both sides.
        for raw in self.extra_deny:
            parsed = _parse_user_pattern(raw)
            if parsed is None:
                continue
            t, pat = parsed
            if _norm(t) == _norm(name) and _match(repr_str, pat):
                return Decision(False, f"matches extra_deny {raw}", "extra_deny")

        # 3. Mode + annotations.
        if ann.requires_confirmation and not self.session_attached:
            return Decision(
                False, "tool requires confirmation but no session attached", "needs_confirm"
            )

        if self.mode == "yolo":
            return Decision(True, "yolo mode", "yolo")

        # ask mode
        if not ann.destructive:
            return Decision(True, "non-destructive read", "ask_non_destructive")

        if self.session_attached:
            # The worker IPC layer is responsible for prompting and overriding
            # this decision interactively. For now, allow — v0.0.2 will gate
            # interactively via a session callback.
            return Decision(
                True, "ask mode, session attached (auto-allowed in v0.0.1)", "ask_attached"
            )

        return Decision(False, "ask mode and no session attached", "ask_no_session")


# ── helpers ──────────────────────────────────────────────────────────────────


def _input_repr(tool_name: str, args: Any) -> str:
    """Render tool args into a string the pattern matcher can scan.

    For ``bash`` we match against the command string; for file tools we match
    against the resolved path. Other tools fall back to JSON-ish repr.
    """
    if tool_name == "bash":
        return getattr(args, "command", "") or ""
    if tool_name in {"file_write", "file_edit", "file_read"}:
        return getattr(args, "path", "") or ""
    return repr(args)


def _match(value: str, pattern: str) -> bool:
    # Expand ~ in patterns to the user's actual home, so we catch real paths.
    if pattern.startswith("~"):
        pattern = str(Path(pattern).expanduser())
    # ``**`` glob recursion isn't supported by fnmatch directly, but our deny
    # patterns are simple enough that the leading-prefix check below covers it.
    if "**" in pattern:
        prefix = pattern.split("**", 1)[0]
        return value.startswith(prefix)
    return fnmatch.fnmatch(value, pattern)


def _norm(name: str) -> str:
    return name.replace("_", "").lower()


def _parse_user_pattern(raw: str) -> tuple[str, str] | None:
    m = _USER_PATTERN_RE.match(raw.strip())
    if not m:
        return None
    return m.group(1), m.group(2)
