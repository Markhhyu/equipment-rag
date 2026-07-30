from types import SimpleNamespace

import pytest
from agents.mcp import MCPServerSse, MCPServerStreamableHttp

from app.query_process.agent.nodes import node_web_search_mcp


def _config(**overrides):
    values = {
        "mcp_base_url": "https://example.test/mcp",
        "api_key": "test-secret",
        "transport": "streamable_http",
        "tool_name": "bailian_web_search",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_create_streamable_http_mcp_server(monkeypatch):
    monkeypatch.setattr(node_web_search_mcp, "mcp_config", _config())

    server = node_web_search_mcp._create_mcp_server()

    assert isinstance(server, MCPServerStreamableHttp)
    assert server.params["url"] == "https://example.test/mcp"
    assert server.params["headers"]["Authorization"] == "Bearer test-secret"


def test_create_legacy_sse_mcp_server(monkeypatch):
    monkeypatch.setattr(node_web_search_mcp, "mcp_config", _config(transport="sse"))

    server = node_web_search_mcp._create_mcp_server()

    assert isinstance(server, MCPServerSse)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"mcp_base_url": ""}, "MCP_DASHSCOPE_BASE_URL"),
        ({"api_key": ""}, "MCP_DASHSCOPE_API_KEY"),
        ({"transport": "websocket"}, "MCP_DASHSCOPE_TRANSPORT"),
    ],
)
def test_create_mcp_server_rejects_invalid_config(monkeypatch, overrides, message):
    monkeypatch.setattr(node_web_search_mcp, "mcp_config", _config(**overrides))

    with pytest.raises(ValueError, match=message):
        node_web_search_mcp._create_mcp_server()
