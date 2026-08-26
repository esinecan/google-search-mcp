"""Offline tests for the non-blocking login path and the pause it puts the daemon into.

No browser and no subprocess: the registry, the guard and the envelopes are pure state, and
they are the parts that fail silently. A pause guard that does not fire looks exactly like a
working one until a login is actually open, at which point the symptom is a mislabelled
profile-lock error in someone else's session.

`session` keeps its login state in module globals (one process serves every seat, so the
registry is deliberately process-wide). Every test therefore resets them.
"""
from __future__ import annotations

import pytest

from google_search_mcp import session
from google_search_mcp.errors import AuthExpired, RateLimited


@pytest.fixture(autouse=True)
def _clean_registry():
    for holder in (session._login_procs, session._login_last_failure):
        holder.clear()
    for holder in (session._login_reserved, session._login_unclassified):
        holder.clear()
    yield
    for holder in (session._login_procs, session._login_last_failure):
        holder.clear()
    for holder in (session._login_reserved, session._login_unclassified):
        holder.clear()


class _FakeProc:
    """A Popen stand-in. `rc=None` is still running."""

    def __init__(self, rc=None, pid=4242):
        self._rc = rc
        self.pid = pid

    def poll(self):
        return self._rc


# --------------------------------------------------------------------- the pause guard


def test_browser_acquisition_is_refused_while_a_login_is_open():
    """The daemon-wide pause. Without it a search races the login child for the profile
    lock and surfaces as `_locked` -- "another agent has it open" -- which sends the reader
    hunting for an agent that is really a window this server opened on purpose."""
    session._login_procs["p"] = (_FakeProc(), 0.0)

    with pytest.raises(RateLimited) as exc:
        session._ensure_browser("p")

    msg = str(exc.value)
    assert "a login window is open on the user's screen" in msg
    assert "google_session_status" in msg
    assert exc.value.kind == "rate_limited"


def test_guard_is_armed_by_the_reservation_before_the_browser_is_released():
    """The window between "we decided to spawn" and "the child exists" is the dangerous one:
    a search landing there would relaunch the browser and take back the profile lock the
    child is about to need."""
    session._login_reserved.add("p")
    with pytest.raises(RateLimited):
        session._ensure_browser("p")


def test_guard_is_per_profile():
    session._login_procs["p"] = (_FakeProc(), 0.0)
    assert session.login_in_flight("p") is True
    assert session.login_in_flight("other") is False


# ------------------------------------------------------------------------ the registry


def test_a_live_child_reports_elapsed_seconds():
    session._login_procs["p"] = (_FakeProc(), session.time.monotonic() - 12.0)
    elapsed = session.login_in_flight_seconds("p")
    assert elapsed is not None and 11.0 <= elapsed <= 14.0


def test_an_exited_child_is_reaped_and_left_unclassified():
    """Reaping must not decide the outcome. A child killed from outside exits non-zero having
    possibly succeeded, and a clean exit can still leave no session, so the verdict waits for
    a real cookie probe."""
    session._login_procs["p"] = (_FakeProc(rc=0), 0.0)

    assert session.login_in_flight_seconds("p") is None
    assert "p" not in session._login_procs
    assert "p" in session._login_unclassified
    assert session._login_last_failure == {}


def test_a_finished_login_that_produced_no_session_starts_the_cooldown():
    session._login_unclassified.add("p")
    session._classify_finished_login("p", signed_in=False)
    assert "p" in session._login_last_failure
    assert "p" not in session._login_unclassified


def test_a_finished_login_that_produced_a_session_clears_the_cooldown():
    session._login_unclassified.add("p")
    session._login_last_failure["p"] = session.time.monotonic()
    session._classify_finished_login("p", signed_in=True)
    assert "p" not in session._login_last_failure


def test_ordinary_probes_do_not_rewrite_the_cooldown():
    """`_classify_finished_login` runs on every status call; only a just-exited child may
    move the cooldown, or a signed-out profile nobody tried to log into would back itself off."""
    session._classify_finished_login("p", signed_in=False)
    assert session._login_last_failure == {}


# --------------------------------------------------------------------- start_login states


def test_start_login_reports_progress_instead_of_opening_a_second_window(monkeypatch):
    session._login_procs["p"] = (_FakeProc(), session.time.monotonic() - 5.0)
    monkeypatch.setattr(session, "shutdown", _must_not_run)

    out = session.start_login("p")
    assert out["status"] == "login_in_progress"
    assert out["poll"] == "google_session_status"
    assert 4 <= out["started_s_ago"] <= 7


def test_start_login_short_circuits_on_a_live_session(monkeypatch):
    """The cheap probe runs before any spawn, so calling this on a live session opens
    nothing -- that is what makes the tool safe to call after an auth_expired that turned out
    to be something else."""
    monkeypatch.setattr(session, "_live_session_now", lambda profile: True)
    monkeypatch.setattr(session, "shutdown", _must_not_run)

    out = session.start_login("p")
    assert out["status"] == "already_signed_in"
    assert "error" not in out


def test_start_login_honours_the_cooldown(monkeypatch):
    monkeypatch.setattr(session, "_live_session_now", lambda profile: False)
    monkeypatch.setattr(session, "shutdown", _must_not_run)
    session._login_last_failure["p"] = session.time.monotonic() - 10.0

    out = session.start_login("p")
    assert out["status"] == "cooldown"
    assert 0 < out["retry_after_s"] <= session._LOGIN_COOLDOWN


def test_start_login_spawns_and_returns_login_started(monkeypatch):
    released = []
    monkeypatch.setattr(session, "_live_session_now", lambda profile: False)
    monkeypatch.setattr(session, "shutdown", lambda: released.append(True))
    monkeypatch.setattr(session.subprocess, "Popen", lambda *a, **kw: _FakeProc())

    out = session.start_login("p", timeout_s=600)
    assert out["status"] == "login_started"
    assert out["note"] == "a browser window is now open on the user's screen"
    assert out["poll"] == "google_session_status"
    assert out["timeout_s"] == 600
    # The browser must be let go before the child runs: Chromium locks a profile dir
    # exclusively, so a server still holding it means a login window that never opens.
    assert released == [True]
    assert session.login_in_flight("p") is True


def test_a_spawn_failure_is_the_one_error_envelope(monkeypatch):
    """A login the user abandons is a state; a helper that cannot be started at all is an
    environment fault, and only that one gets an error envelope."""
    monkeypatch.setattr(session, "_live_session_now", lambda profile: False)
    monkeypatch.setattr(session, "shutdown", lambda: None)

    def _boom(*a, **kw):
        raise OSError("no python here")

    monkeypatch.setattr(session.subprocess, "Popen", _boom)

    out = session.start_login("p")
    assert out["error"]["kind"] == "schema_drift"
    assert out["error"]["detail"]["cause"] == "environment"
    # and the reservation is released, or the box would stay paused forever
    assert session.login_in_flight("p") is False


def test_a_login_helper_refuses_to_start_a_login(monkeypatch):
    monkeypatch.setenv("GOOGLE_MCP_LOGIN_CHILD", "1")
    monkeypatch.setattr(session, "shutdown", _must_not_run)
    out = session.start_login("p")
    assert out["error"]["kind"] == "schema_drift"


def test_the_child_command_carries_the_profile_and_the_recursion_guard():
    argv, env, _flags = session._login_child("agent", 600)
    assert argv[1:] == ["-m", "google_search_mcp.cli", "login",
                        "--profile", "agent", "--timeout", "600"]
    assert env["GOOGLE_MCP_LOGIN_CHILD"] == "1"


# ------------------------------------------------------------------------- status + envelope


def test_status_reports_a_login_in_progress_without_touching_the_browser(monkeypatch):
    """Order matters: the registry is read before the browser, because a probe during a login
    is refused by the guard and reporting signed_in=false mid-sign-in is a lie."""
    session._login_procs["p"] = (_FakeProc(), session.time.monotonic() - 3.0)
    monkeypatch.setattr(session, "_ensure_browser", _must_not_run)

    st = session.status("p")
    assert st["login"] == "login_in_progress"
    assert st["signed_in"] is False
    assert 2 <= st["started_s_ago"] <= 5


def test_auth_expired_carries_the_login_tool_as_a_branchable_field():
    """Prose in `error` is not something an agent can branch on. `detail.login_tool` is."""
    result = AuthExpired("no session").as_result()
    assert result["kind"] == "auth_expired"
    assert result["detail"]["login_tool"] == "google_initiate_login"


def test_other_kinds_carry_no_detail():
    assert "detail" not in RateLimited("slow down").as_result()


def _must_not_run(*args, **kwargs):
    raise AssertionError("this path must not be reached")
