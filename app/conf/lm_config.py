import os
from dataclasses import dataclass

from dotenv import load_dotenv


# 提前加载项目根目录的 .env；Docker 注入的系统环境变量优先级高于文件值。
load_dotenv()


@dataclass
class LLMConfig:
    """OpenAI 兼容大模型连接配置。

    base_url 只填写服务根地址，不包含 /chat/completions；
    api_key 只保存原始密钥，SDK 会负责生成 Authorization 请求头。
    """

    base_url: str
    api_key: str
    lv_model: str  # 支持图片输入的视觉模型 ID。
    llm_model: str  # 用于改写、HyDE 和回答生成的对话模型 ID。
    llm_temperature: float  # 生成随机性，设备知识问答建议保持较低值。


lm_config = LLMConfig(
    base_url=os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1",
    api_key=os.getenv("OPENAI_API_KEY") or "",
    lv_model=os.getenv("VL_MODEL") or "gpt-4.1-mini",
    llm_model=os.getenv("LLM_DEFAULT_MODEL") or "gpt-4.1-mini",
    llm_temperature=float(os.getenv("LLM_DEFAULT_TEMPERATURE") or "0.1"),
)
