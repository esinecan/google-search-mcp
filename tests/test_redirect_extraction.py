"""Offline browser regression for the opaque /goto links observed 2026-09-06."""
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from google_search_mcp.client import _extract_results
from google_search_mcp.errors import SchemaDrift


@pytest.fixture
def page():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.set_content('''<div id="search">
          <div><a href="https://www.google.com/goto?url=one"><h3>First</h3></a>
          <p>A useful description of the first result with enough text.</p></div>
          <div><a href="https://www.google.com/goto?url=two"><h3>Second</h3></a></div>
          <div><a href="https://example.org/first"><h3>Duplicate</h3></a></div>
          <div id="tads"><a href="https://www.google.com/goto?url=ad"><h3>Ad</h3></a></div>
          <a href="https://www.google.com/aclk?x=ad"><h3>Ad click</h3></a>
          <a href="https://www.google.com/preferences"><h3>Settings</h3></a>
        </div>''')
        yield page
        browser.close()


def test_resolve_deduplicate_and_exclude_ads(page):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        assert kwargs == {"max_redirects": 0, "timeout": 10000}
        target = "first" if url.endswith("one") else "second"
        return SimpleNamespace(status=302, headers={"location": f"https://example.org/{target}"}, dispose=lambda: None)
    proxy = SimpleNamespace(evaluate=page.evaluate, request=SimpleNamespace(get=get))
    data = _extract_results(proxy, {"anchor": "h3"})
    assert [r["url"] for r in data["organic"]] == ["https://example.org/first", "https://example.org/second"]
    assert all(r["host"] == "example.org" for r in data["organic"])
    assert len(calls) == 2
    assert not any("url=ad" in url for url in calls)


def test_unresolved_redirect_is_not_silently_empty(page):
    response = SimpleNamespace(status=200, headers={}, dispose=lambda: None)
    proxy = SimpleNamespace(evaluate=page.evaluate, request=SimpleNamespace(get=lambda *a, **k: response))
    with pytest.raises(SchemaDrift, match="organic /goto"):
        _extract_results(proxy, {"anchor": "h3"})
