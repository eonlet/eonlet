"""Token-window pagination."""

from __future__ import annotations

import pytest

from eonlet.web.pagination import paginate


def test_paginate_short_text_not_truncated() -> None:
    result = paginate("hello world", offset_tokens=0, max_tokens=100)
    assert result.text == "hello world"
    assert result.truncated is False
    assert result.next_offset is None


def test_paginate_yields_window() -> None:
    # 400-char body → 100 tokens (4 chars/token estimator).
    body = "x" * 400
    page = paginate(body, offset_tokens=0, max_tokens=10)
    # 10 tokens = 40 chars.
    assert page.text == "x" * 40
    assert page.truncated is True
    assert page.next_offset == 10


def test_paginate_chains_to_end() -> None:
    body = "x" * 400  # 100 tokens
    offsets: list[int] = []
    offset = 0
    while True:
        page = paginate(body, offset_tokens=offset, max_tokens=30)
        offsets.append(offset)
        if page.next_offset is None:
            break
        offset = page.next_offset
        if len(offsets) > 20:
            pytest.fail("pagination did not terminate")
    assert offsets == [0, 30, 60, 90]


def test_paginate_offset_past_end() -> None:
    page = paginate("hi", offset_tokens=99, max_tokens=10)
    assert page.text == ""
    assert page.next_offset is None
    assert page.truncated is False


def test_paginate_rejects_bad_params() -> None:
    with pytest.raises(ValueError):
        paginate("x", offset_tokens=-1, max_tokens=10)
    with pytest.raises(ValueError):
        paginate("x", offset_tokens=0, max_tokens=0)


def test_paginate_reports_total_tokens() -> None:
    page = paginate("x" * 400, offset_tokens=0, max_tokens=10)
    assert page.total_tokens == 100  # 400 chars / 4
