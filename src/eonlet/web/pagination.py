"""Token-window slicing for paginated tool output.

Uses the same 4-chars-per-token estimator as ``memory/tokens.py`` so the
caller's ``max_tokens`` budget translates predictably into a character
window. Returns a :class:`PaginatedSlice` that tells the caller whether
more content is available and where to continue.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..memory.tokens import estimate

_CHARS_PER_TOKEN = 4


class PaginatedSlice(BaseModel):
    """One window of a longer body, paged by ``offset_tokens``."""

    text: str
    truncated: bool
    total_tokens: int
    next_offset: int | None


def paginate(text: str, *, offset_tokens: int = 0, max_tokens: int = 4000) -> PaginatedSlice:
    """Return the ``max_tokens``-wide slice of ``text`` starting at ``offset_tokens``.

    ``offset_tokens`` past the end yields an empty slice with
    ``next_offset=None``. Slicing is character-based for cheapness; the
    token counts reflect the estimator, not a real tokenizer.
    """
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    if offset_tokens < 0:
        raise ValueError("offset_tokens must be non-negative")

    total_tokens = estimate(text)
    char_offset = offset_tokens * _CHARS_PER_TOKEN
    char_window = max_tokens * _CHARS_PER_TOKEN

    if char_offset >= len(text):
        return PaginatedSlice(
            text="",
            truncated=False,
            total_tokens=total_tokens,
            next_offset=None,
        )

    slice_end = char_offset + char_window
    sliced = text[char_offset:slice_end]
    truncated = slice_end < len(text)
    next_offset = offset_tokens + max_tokens if truncated else None
    return PaginatedSlice(
        text=sliced,
        truncated=truncated,
        total_tokens=total_tokens,
        next_offset=next_offset,
    )
