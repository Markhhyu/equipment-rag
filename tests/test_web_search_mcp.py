import asyncio

from app.modules.qa.graph.nodes import node_web_search_mcp


class _UnauthorizedServer:
    async def connect(self):
        raise RuntimeError("401 Unauthorized")

    async def cleanup(self):
        raise RuntimeError("401 Unauthorized")


def test_mcp_auth_failure_degrades_to_empty_results(monkeypatch):
    monkeypatch.setattr(node_web_search_mcp, "_create_mcp_server", lambda: _UnauthorizedServer())

    assert asyncio.run(node_web_search_mcp.mcp_call("LJ2268卡纸")) is None
