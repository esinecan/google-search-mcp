from __future__ import annotations

from types import SimpleNamespace

import pytest
import trafilatura

from google_search_mcp import client, session

ARTICLE = "<html><body><article><h1>Title</h1>" + "".join(
    f"<p>Paragraph {i} carries enough plain prose for the extractor to keep it as article text and not as boilerplate.</p>"
    for i in range(12)
) + "</article></body></html>"


class _FakePage:
    def __init__(self, html, status):
        self.html, self.status = html, status

    def goto(self, url, **_kw):
        return SimpleNamespace(status=self.status)

    def content(self):
        return self.html


@pytest.fixture
def serve(monkeypatch):
    monkeypatch.setattr(client._throttle, "wait", lambda url: None)
    monkeypatch.setattr(client.time, "sleep", lambda s: None)

    def _install(html, status=200):
        monkeypatch.setattr(session, "get_page", lambda *a, **k: _FakePage(html, status))

    return _install


def test_article_is_ok(serve):
    serve(ARTICLE)
    got = client._fetch_one("https://example.test/a", 2000)
    assert got["ok"] is True and got["status"] == 200 and got["chars_total"] >= 500


def test_wall_status_is_walled(serve):
    serve(ARTICLE, status=403)
    got = client._fetch_one("https://example.test/a", 2000)
    assert got["ok"] is False and got["reason"] == "walled" and got["markdown"]


def test_short_markdown_is_thin(serve, monkeypatch):
    serve(ARTICLE)
    monkeypatch.setattr(client, "_to_markdown", lambda html, url: "word " * 60)
    got = client._fetch_one("https://example.test/a", 2000)
    assert got["ok"] is False and got["reason"] == "thin" and got["chars_total"] == 300


def test_missing_pre_is_lossy(serve, monkeypatch):
    serve(ARTICLE.replace("</article>", "<pre>install-the-thing --now</pre></article>"))
    monkeypatch.setattr(client, "_to_markdown", lambda html, url: "Long prose without the command. " * 40)
    got = client._fetch_one("https://example.test/a", 2000)
    assert got["ok"] is False and got["reason"] == "lossy" and got["pre_missing"] == 1


PARA = "<p>The agent server runs the agent loop behind an HTTP API, so an application backend can drive conversations remotely and keep the runtime isolated from the caller's process.</p>"
CODE_BLOCK = ('<div class="code-block mt-5 mb-8 rounded-2xl"><div class="flex toolbar"><span>bash</span>'
    '<button aria-label="Copy the contents from the code block">Copy</button></div>'
    '<div class="w-0"><div class="size-full py-3.5 px-4 base-ui-disable-scrollbar"><div class="font-mono"><pre class="shiki shiki-themes github-light-default"><code>'
    '<span class="line"><span>uv venv</span></span>\n<span class="line"><span>uv pip install -U openhands-agent-server</span></span>'
    '</code></pre></div></div></div></div>')
CALLOUT = ('<div class="callout my-4 px-5 py-4 overflow-hidden rounded-2xl" data-callout-type="warning"><div class="mt-0.5 w-4" data-component-part="callout-icon"><svg></svg></div>'
    '<div class="text-sm prose min-w-0 w-full" data-component-part="callout-content"><span data-as="p">ACP-backed remote conversations use the ACP-capable conversation endpoints under <code>/api/acp/conversations</code> for creation, reads, and counts.</span></div></div>')
DOCS_PAGE = ("<html><head><title>Agent Server</title></head><body><nav><a href='/'>Home</a><a href='/docs'>Docs</a></nav><main><article><h1>Agent Server</h1>"
    + PARA + "<h2>Install</h2><p>Install the server package and its SDK dependencies into a Python environment:</p>" + CODE_BLOCK
    + PARA.replace("agent loop", "event stream") + CALLOUT + PARA.replace("agent loop", "workspace") + "</article></main><footer>(c) 2026</footer></body></html>")
DOCS_URL = "https://docs.example.test/agent-server"


def test_fixture_reproduces_the_drop():
    raw = trafilatura.extract(DOCS_PAGE, url=DOCS_URL, output_format="markdown", include_links=True,
                              include_tables=True, favor_precision=True) or ""
    assert "uv venv" not in raw and "ACP-capable" not in raw


def test_to_markdown_keeps_code_block_and_callout():
    md = client._to_markdown(DOCS_PAGE, DOCS_URL)
    assert "\n```\nuv venv\n" in md
    assert "ACP-capable conversation endpoints" in md


def test_docs_page_is_ok(serve):
    serve(DOCS_PAGE)
    got = client._fetch_one(DOCS_URL, 2000)
    assert got["ok"] is True
