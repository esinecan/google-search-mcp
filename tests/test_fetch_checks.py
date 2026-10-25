from __future__ import annotations

from types import SimpleNamespace

import pytest

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
