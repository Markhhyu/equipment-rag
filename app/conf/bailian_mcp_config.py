import os
from dataclasses import dataclass

from dotenv import load_dotenv


# 必须先加载项目根目录的 .env，后面的配置对象才能读取到用户填写的值。
load_dotenv()


@dataclass(frozen=True)
class McpConfig:
    """百炼 WebSearch MCP 连接配置。"""

    mcp_base_url: str
    api_key: str
    transport: str
    tool_name: str


mcp_config = McpConfig(
    # 当前百炼官方端点以 /mcp 结尾，使用 Streamable HTTP 协议。
    mcp_base_url=os.getenv("MCP_DASHSCOPE_BASE_URL") or "",
    # 新配置使用独立百炼 Key；旧项目未配置时仍回退到 OPENAI_API_KEY。
    api_key=os.getenv("MCP_DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY") or "",
    # sse 只用于仍提供旧版 /sse 地址的私有或历史服务。
    transport=(os.getenv("MCP_DASHSCOPE_TRANSPORT") or "streamable_http").strip().lower(),
    # 不同 MCP 服务暴露的工具名可能不同，百炼联网搜索默认为 bailian_web_search。
    tool_name=os.getenv("MCP_DASHSCOPE_TOOL_NAME") or "bailian_web_search",
)
