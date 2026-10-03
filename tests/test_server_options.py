"""Explicit isolated-server options retain the existing default behavior."""

from dcc_mcp_core.server_base import DccServerBase

from dcc_mcp_openscad.server import OpenscadMcpServer


def capture_options(monkeypatch):
    captured = []
    monkeypatch.setattr(DccServerBase, "__init__", lambda self, options: captured.append(options))
    return captured


def test_explicit_gateway_options_override_environment(monkeypatch):
    monkeypatch.setenv("DCC_MCP_GATEWAY_PORT", "12345")
    captured = capture_options(monkeypatch)
    OpenscadMcpServer(port=0, gateway_port=0, enable_gateway_failover=False)
    assert captured[0].gateway.port == 0
    assert captured[0].gateway.enable_failover is False


def test_omitted_gateway_options_preserve_defaults(monkeypatch):
    monkeypatch.setenv("DCC_MCP_GATEWAY_PORT", "12345")
    captured = capture_options(monkeypatch)
    OpenscadMcpServer(port=0)
    assert captured[0].gateway.port == 12345
    assert captured[0].gateway.enable_failover is True
