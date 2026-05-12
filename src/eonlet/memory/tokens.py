"""Cheap token estimation for context injection budgeting.

Per MEMORY_SPEC §3.2 the recent-messages window is sized by token budget,
not by an exact tokenizer. A 4-chars-per-token heuristic is close enough
for English-and-code mixed conversation; it under-counts CJK slightly but
that's a feature, not a bug — we want to be conservative.

If a real tokenizer is later required (e.g. for tight model-window
budgeting), swap ``estimate`` for a provider-specific implementation behind
the same call signature.
"""

from __future__ import annotations

_CHARS_PER_TOKEN = 4


def estimate(text: str) -> int:
    """Estimate token count of ``text``. Empty string returns 0."""
    if not text:
        return 0
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def estimate_message(role: str, content: str, *, tool_calls: int = 0) -> int:
    """Estimate tokens for one message including a small role/tool overhead.

    Each message has framing overhead in the wire format (role tag, JSON
    delimiters); we approximate that as 4 tokens, plus 6 tokens per tool
    call's structural overhead.
    """
    base = 4 + estimate(content) + estimate(role)
    return base + 6 * tool_calls
