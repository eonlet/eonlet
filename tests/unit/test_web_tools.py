"""web_search + web_fetch — exercise both backends with monkey-patched httpx."""
from __future__ import annotations

import json
from typing import Any

import anyio
import httpx
import pytest

from eonlet.tools.builtin.web import (
    WebFetchArgs,
    WebFetchTool,
    WebSearchArgs,
    WebSearchTool,
)
from eonlet.tools.protocol import ToolContext


def _ctx(tmp_path) -> ToolContext:
    return ToolContext(
        eonlet_id="t.x",
        workspace=tmp_path,
        memory_dir=tmp_path,
        notes_files=[],
        skills={},
        env={},
    )


class _FakeResp:
    def __init__(self, *, status: int = 200, text: str = "", json_body: Any = None, headers: dict | None = None):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self._json = json_body
        self.headers = headers or {"content-type": "text/html"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> Any:
        return self._json


class _FakeClient:
    def __init__(self, get_resp: _FakeResp | None = None, post_resp: _FakeResp | None = None):
        self._get = get_resp
        self._post = post_resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get(self, *args, **kwargs):
        return self._get

    async def post(self, *args, **kwargs):
        return self._post


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, *, get_resp=None, post_resp=None) -> None:
    from eonlet.tools.builtin import web as web_mod

    def _factory(*a, **k):
        return _FakeClient(get_resp=get_resp, post_resp=post_resp)

    monkeypatch.setattr(web_mod.httpx, "AsyncClient", _factory)


# ── web_search ───────────────────────────────────────────────────────────────


def test_web_search_uses_tavily_when_key_present(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "secret")
    _patch_httpx(
        monkeypatch,
        post_resp=_FakeResp(
            json_body={
                "results": [
                    {"title": "Py docs", "url": "https://docs.python.org", "content": "Python rocks"}
                ]
            }
        ),
    )
    tool = WebSearchTool()
    result = anyio.run(tool.__call__, WebSearchArgs(query="python"), _ctx(tmp_path))
    assert not result.is_error
    assert "Py docs" in result.content
    assert result.structured_output["results"][0]["url"] == "https://docs.python.org"


def test_web_search_falls_back_to_ddg_without_key(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    # Minimal DDG HTML mimicking the class names the parser scrapes.
    html = '''
    <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Example A</a>
    <a class="result__snippet">A is a thing.</a>
    <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fb">Example B</a>
    <a class="result__snippet">B is another.</a>
    '''
    _patch_httpx(monkeypatch, get_resp=_FakeResp(text=html))
    tool = WebSearchTool()
    result = anyio.run(tool.__call__, WebSearchArgs(query="example"), _ctx(tmp_path))
    assert not result.is_error
    results = result.structured_output["results"]
    assert results[0]["url"] == "https://example.com/a"
    assert "Example A" in result.content


# ── web_fetch ────────────────────────────────────────────────────────────────


def test_web_fetch_strips_html_and_extracts_title(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = """
    <html><head><title>Hello Title</title></head>
    <body>
      <script>var x = 1;</script>
      <h1>Hi</h1>
      <p>Hello &amp; goodbye</p>
    </body></html>
    """
    _patch_httpx(monkeypatch, get_resp=_FakeResp(text=html))
    tool = WebFetchTool()
    result = anyio.run(tool.__call__, WebFetchArgs(url="http://x.test"), _ctx(tmp_path))
    assert not result.is_error
    assert "Hi" in result.content
    assert "Hello & goodbye" in result.content  # entity decoded
    assert "var x" not in result.content  # script stripped
    assert result.structured_output["title"] == "Hello Title"


def test_web_fetch_handles_non_html(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_httpx(
        monkeypatch,
        get_resp=_FakeResp(
            text='{"k": "v"}', headers={"content-type": "application/json"}
        ),
    )
    result = anyio.run(WebFetchTool().__call__, WebFetchArgs(url="http://x.test/data.json"), _ctx(tmp_path))
    assert not result.is_error
    assert '"k": "v"' in result.content


def test_web_fetch_network_error(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Failing(_FakeClient):
        async def get(self, *a, **k):
            raise httpx.ConnectError("nope")

    from eonlet.tools.builtin import web as web_mod

    monkeypatch.setattr(web_mod.httpx, "AsyncClient", lambda *a, **k: _Failing())
    result = anyio.run(WebFetchTool().__call__, WebFetchArgs(url="http://x.test"), _ctx(tmp_path))
    assert result.is_error
    assert "fetch failed" in result.content
