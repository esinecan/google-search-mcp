"""A failed browser launch must not poison the process.

The failure this guards is not the launch error itself -- a locked profile dir is normal
and self-healing. It is what the old code left behind: `sync_playwright()` had already
started when `launch_persistent_context` raised, and the started handle stayed in
`session._pw_cm`. On the browser thread that handle *is* a running asyncio loop (the sync
API drives one from a suspended dispatcher greenlet), so the next call's `sync_playwright()`
raised "It looks like you are using Playwright Sync API inside the asyncio loop." Every
tool in the process then reported an asyncio problem that did not exist, for the process
lifetime, while the real cause -- another process holds the profile -- was never mentioned
again.

Offline: no browser and no Playwright. The launch is a stub that raises, and the assertion
is about the handle the failure leaves behind.
"""
from __future__ import annotations

import pytest

from google_search_mcp import session
from google_search_mcp.errors import RateLimited, SchemaDrift


class _FakePlaywrightCM:
    """Stands in for the `sync_playwright()` context manager."""

    def __init__(self, error: Exception):
        self.error = error
        self.exited = False

    def __enter__(self):
        return _FakePlaywright(self.error)

    def __exit__(self, *_exc):
        self.exited = True
        return False


class _FakePlaywright:
    def __init__(self, error: Exception):
        self.chromium = _FakeChromium(error)


class _FakeChromium:
    def __init__(self, error: Exception):
        self.error = error
        self.launches = 0

    def launch_persistent_context(self, **_kw):
        self.launches += 1
        raise self.error


@pytest.fixture
def failing_launch(monkeypatch, tmp_path):
    """Make every launch attempt fail, and record the context managers that were started."""
    monkeypatch.setattr(session, "SESSION_ROOT", tmp_path)
    started: list[_FakePlaywrightCM] = []

    def _install(error: Exception):
        def _cm():
            cm = _FakePlaywrightCM(error)
            started.append(cm)
            return cm

        monkeypatch.setattr(session, "_playwright_cm", _cm)
        return started

    session._pw_cm = session._pw = session._ctx = session._page = None
    yield _install
    session._pw_cm = session._pw = session._ctx = session._page = None


def test_locked_profile_leaves_no_playwright_handle(failing_launch):
    started = failing_launch(Exception("SingletonLock: profile appears to be in use"))

    with pytest.raises(RateLimited):
        session._ensure_browser("someprofile")

    assert session._pw_cm is None, "a failed launch left its Playwright handle behind"
    assert session._pw is None
    assert started[0].exited, "the started Playwright was never closed"


def test_second_attempt_reports_the_real_cause_again(failing_launch):
    """The second call must classify the launch failure, not an invented asyncio error."""
    failing_launch(Exception("SingletonLock: profile appears to be in use"))

    for _ in range(3):
        with pytest.raises(RateLimited) as caught:
            session._ensure_browser("someprofile")
        assert "locked by another process" in str(caught.value)


def test_missing_browser_also_drops_the_handle(failing_launch):
    started = failing_launch(Exception("Executable doesn't exist -- run playwright install"))

    with pytest.raises(SchemaDrift):
        session._ensure_browser("someprofile")

    assert session._pw_cm is None
    # Both attempts (channel=chrome, then bundled Chromium) run against one handle.
    assert started[0].exited
