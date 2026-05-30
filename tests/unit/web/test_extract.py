"""Content extraction — HTML → markdown via trafilatura; text/JSON passthrough."""

from __future__ import annotations

import json

import pytest

from eonlet.web.fetch import (
    extract_html,
    extract_text,
    is_html_content_type,
    is_text_content_type,
)

_NEWS_HTML = b"""<!doctype html>
<html><head><title>Headline Story</title>
<meta name="author" content="Jane Reporter">
<meta property="article:published_time" content="2026-05-20T10:00:00Z">
</head><body>
<article>
<h1>Headline Story</h1>
<p>The lede paragraph of the news article. It is long enough to look like
prose so that the boilerplate-stripper keeps it.</p>
<h2>A subsection</h2>
<p>Another paragraph with an <a href="https://example.com/source">inline
source link</a> that should survive markdown conversion.</p>
<ul><li>One bullet</li><li>Two bullet</li></ul>
</article>
<footer>navigation that should be stripped</footer>
</body></html>
"""

_SPA_HTML = b"""<!doctype html><html><head><title>SPA Shell</title></head>
<body><div id="app"></div><script>window.__INIT__=1;</script></body></html>
"""


def test_extract_html_news_returns_title_and_markdown() -> None:
    result = extract_html(_NEWS_HTML, url="https://news.example/story")
    assert result.title == "Headline Story"
    assert "lede paragraph" in result.content_markdown
    # The author meta should be picked up by trafilatura's metadata pass.
    assert result.metadata.get("author") == "Jane Reporter"


def test_extract_html_preserves_link() -> None:
    result = extract_html(_NEWS_HTML, url="https://news.example/story")
    # trafilatura's markdown mode renders <a href> as [text](href).
    assert "https://example.com/source" in result.content_markdown


def test_extract_html_warns_on_empty_body() -> None:
    result = extract_html(_SPA_HTML, url="https://spa.example/")
    assert result.content_markdown == ""
    assert result.metadata.get("warning") == "no_main_content"


def test_extract_text_json_pretty_print() -> None:
    raw = b'{"b": 1, "a": [2, 3]}'
    result = extract_text(raw, content_type="application/json", url="https://j.example/")
    parsed = json.loads(result.content_markdown)
    assert parsed == {"a": [2, 3], "b": 1}
    # Pretty-printed output is multi-line.
    assert "\n" in result.content_markdown
    assert result.metadata.get("formatted") == "json"


def test_extract_text_invalid_json_passthrough() -> None:
    raw = b"not-actually-json"
    result = extract_text(raw, content_type="application/json", url="https://j.example/")
    assert result.content_markdown == "not-actually-json"
    assert result.metadata.get("warning") == "invalid_json_passthrough"


def test_extract_text_plain() -> None:
    result = extract_text(b"hello", content_type="text/plain", url="https://t.example/")
    assert result.content_markdown == "hello"


def test_extract_text_decodes_lossy() -> None:
    # Invalid UTF-8 byte must not raise.
    result = extract_text(b"hello \xff world", content_type="text/plain", url="x")
    assert "hello" in result.content_markdown


@pytest.mark.parametrize(
    "ctype",
    ["text/html", "text/html; charset=utf-8", "application/xhtml+xml"],
)
def test_is_html_content_type(ctype: str) -> None:
    assert is_html_content_type(ctype)


@pytest.mark.parametrize(
    "ctype",
    ["application/pdf", "image/png", "application/octet-stream"],
)
def test_is_html_content_type_negative(ctype: str) -> None:
    assert not is_html_content_type(ctype)


@pytest.mark.parametrize(
    "ctype",
    ["text/plain", "application/json", "text/csv; charset=utf-8"],
)
def test_is_text_content_type(ctype: str) -> None:
    assert is_text_content_type(ctype)


def test_is_text_content_type_excludes_html() -> None:
    assert not is_text_content_type("text/html")


@pytest.mark.parametrize(
    "ctype",
    ["application/xml", "application/xml; charset=utf-8", "application/rss+xml"],
)
def test_is_text_content_type_excludes_xml(ctype: str) -> None:
    # ADR-0004: XML/RSS/Atom fall through to the unsupported-type branch so
    # the caller is steered to a custom feed_read tool instead of getting
    # raw XML rendered as text.
    assert not is_text_content_type(ctype)
