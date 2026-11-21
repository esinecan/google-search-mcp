from __future__ import annotations

import asyncio
import sys

import pytest

from google_search_mcp import mcp_server


class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append(scope)


def _run(guard, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(guard(scope, receive, send))
    return sent


def _scope(method, headers=(), path="/mcp"):
    return {"type": "http", "method": method, "path": path, "headers": list(headers)}


@pytest.mark.parametrize("method", ["GET", "DELETE"])
@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_sessionless_probe_is_answered_without_creating_a_session(method, path):
    inner = _Recorder()
    sent = _run(mcp_server.SessionlessRequestGuard(inner), _scope(method, path=path))
    assert inner.calls == []
    assert sent[0]["status"] == 400


@pytest.mark.parametrize("scope", [
    _scope("POST"),
    _scope("GET", headers=[(b"mcp-session-id", b"abc")]),
    _scope("GET", path="/other"),
    {"type": "lifespan"},
])
def test_everything_else_reaches_the_mcp_app(scope):
    inner = _Recorder()
    sent = _run(mcp_server.SessionlessRequestGuard(inner), scope)
    assert inner.calls == [scope]
    assert sent == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows priority classes")
def test_background_priority_is_raised_to_normal():
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetPriorityClass.argtypes = [ctypes.c_void_p]
    kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    me = kernel32.GetCurrentProcess()
    before = kernel32.GetPriorityClass(me)
    try:
        assert kernel32.SetPriorityClass(me, mcp_server.BELOW_NORMAL_PRIORITY_CLASS)
        assert mcp_server._leave_background_priority() is True
        assert kernel32.GetPriorityClass(me) == mcp_server.NORMAL_PRIORITY_CLASS
        assert mcp_server._leave_background_priority() is False
    finally:
        kernel32.SetPriorityClass(me, before)
