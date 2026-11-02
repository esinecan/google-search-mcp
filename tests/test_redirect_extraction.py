"""Offline browser regression for the opaque /goto links and their script-driven verification page."""
import pytest
from playwright.sync_api import sync_playwright

from google_search_mcp import client
from google_search_mcp.client import _extract_results
from google_search_mcp.errors import SchemaDrift

SERP = '''<div id="search">
  <div><a href="https://www.google.com/goto?url=one"><h3>First</h3></a>
  <p>A useful description of the first result with enough text.</p></div>
  <div><a href="https://www.google.com/goto?url=two"><h3>Second</h3></a></div>
  <div><a href="https://example.org/first"><h3>Duplicate</h3></a></div>
  <div id="tads"><a href="https://www.google.com/goto?url=ad"><h3>Ad</h3></a></div>
  <a href="https://www.google.com/aclk?x=ad"><h3>Ad click</h3></a>
  <a href="https://www.google.com/preferences"><h3>Settings</h3></a>
</div>'''

VERIFY = '''<!doctype html><title>Google</title><p>Verifying your request</p>
<script>setTimeout(() => location.replace("https://example.org/%s"), 300)</script>'''


@pytest.fixture
def page():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_context().new_page()
        page.set_content(SERP)
        yield page
        browser.close()


def test_resolve_deduplicate_and_exclude_ads(page):
    goto_calls, site_hits = [], []

    def goto(route):
        goto_calls.append(route.request.url)
        target = "first" if route.request.url.endswith("one") else "second"
        route.fulfill(status=200, content_type="text/html", body=VERIFY % target)

    page.context.route("https://www.google.com/goto?url=*", goto)
    page.context.route("https://example.org/**", lambda route: (site_hits.append(route.request.url), route.abort()))
    data = _extract_results(page, {"anchor": "h3"})
    assert [r["url"] for r in data["organic"]] == ["https://example.org/first", "https://example.org/second"]
    assert all(r["host"] == "example.org" for r in data["organic"])
    assert len(goto_calls) == 2
    assert not any("url=ad" in url for url in goto_calls)
    assert site_hits == []
    assert page.context.pages == [page]


def test_unresolved_redirect_is_not_silently_empty(page, monkeypatch):
    monkeypatch.setattr(client, "GOTO_TIMEOUT_S", 1.0)
    page.context.route("https://www.google.com/goto?url=*", lambda route: route.fulfill(status=200, content_type="text/html", body="<p>stuck</p>"))
    with pytest.raises(SchemaDrift, match="organic /goto"):
        _extract_results(page, {"anchor": "h3"})
