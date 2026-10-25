from __future__ import annotations

from mcp.server.transport_security import TransportSecurityMiddleware

from google_search_mcp import mcp_server


def test_loopback_bind_accepts_docker_host():
    mw = TransportSecurityMiddleware(mcp_server._transport_security("127.0.0.1"))
    assert mw._validate_host("host.docker.internal:8766")
    assert mw._validate_host("127.0.0.1:8766")
    assert not mw._validate_host("evil.example:8766")


def test_non_loopback_bind_keeps_framework_default():
    assert mcp_server._transport_security("0.0.0.0") is None
