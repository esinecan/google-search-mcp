from __future__ import annotations

import threading
import time

import pytest

from google_search_mcp import session
from google_search_mcp.errors import RateLimited


@pytest.fixture
def short_deadline(monkeypatch):
    monkeypatch.setattr(session, "BROWSER_CALL_TIMEOUT_S", 0.3)


def test_call_past_deadline_returns_rate_limited(short_deadline):
    release = threading.Event()
    try:
        started = time.monotonic()
        with pytest.raises(RateLimited) as info:
            session.in_browser_thread(release.wait, 10)
        assert time.monotonic() - started < 2
        assert info.value.as_result()["kind"] == "rate_limited"
        assert info.value.detail["reason"] == "browser_busy"
    finally:
        release.set()


def test_queued_call_that_timed_out_never_runs(short_deadline):
    release = threading.Event()
    ran = []
    blocker = session._executor.submit(release.wait, 10)
    try:
        with pytest.raises(RateLimited):
            session.in_browser_thread(ran.append, "late")
    finally:
        release.set()
    blocker.result(timeout=5)
    session._executor.submit(lambda: None).result(timeout=5)
    assert ran == []


def test_fast_call_still_returns_its_value(short_deadline):
    assert session.in_browser_thread(lambda: 42) == 42
