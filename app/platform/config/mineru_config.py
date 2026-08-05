"""MinerU parser client configuration."""

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv


# 从项目根目录加载.env配置。
load_dotenv(find_dotenv())


def _get_bool(name: str, default: bool) -> bool:
    """读取布尔类型环境变量。"""

    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    """读取整数类型环境变量，格式错误时返回默认值。"""

    value = os.getenv(name)
    if not value:
        return default

    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class MineruConfig:
    """MinerU 3.x API客户端配置。"""

    base_url: str
    api_token: str
    backend: str
    effort: str
    parse_method: str
    language: str
    formula_enable: bool
    table_enable: bool
    image_analysis: bool
    return_middle_json: bool
    return_content_list: bool
    poll_interval_seconds: int
    task_timeout_seconds: int
    request_timeout_seconds: int
    download_timeout_seconds: int
    verify_ssl: bool



# 当前MinerU API支持的OCR语言参数。
SUPPORTED_MINERU_LANGUAGES = {
    "ch", "ch_server", "korean", "ta", "te", "ka",
    "th", "el", "arabic", "east_slavic", "cyrillic", "devanagari"
}


def _get_language() -> str:
    """
    读取并校验MinerU语言配置。

    当前API不支持auto，因此配置错误时直接在服务启动阶段提示。
    """

    language = (os.getenv("MINERU_LANGUAGE") or "ch_server").strip().lower()

    if language not in SUPPORTED_MINERU_LANGUAGES:
        supported = ", ".join(sorted(SUPPORTED_MINERU_LANGUAGES))
        raise ValueError(f"不支持的MINERU_LANGUAGE={language}，当前支持：{supported}")

    return language

mineru_config = MineruConfig(
    # 优先读取新配置，旧配置名保留兼容。
    base_url=(os.getenv("MINERU_API_BASE_URL") or os.getenv("MINERU_BASE_URL") or "http://127.0.0.1:8002").rstrip("/"),

    # 自部署MinerU通常不需要Token，配置为空即可。
    api_token=os.getenv("MINERU_API_TOKEN") or "",

    # 默认使用支持纯CPU运行的pipeline，存在GPU时可在.env中改成hybrid-engine。
    backend=os.getenv("MINERU_BACKEND") or "pipeline",

    # high保留完整图片和图表分析；medium速度更快。
    effort=os.getenv("MINERU_EFFORT") or "high",

    # auto自动判断使用文本提取还是OCR。
    parse_method=os.getenv("MINERU_PARSE_METHOD") or "auto",

    # 当前MinerU API不支持auto，默认使用兼容中英文混合内容的ch_server。
    language=_get_language(),
    formula_enable=_get_bool("MINERU_FORMULA_ENABLE", True),
    table_enable=_get_bool("MINERU_TABLE_ENABLE", True),
    # VLM图片分析只适用于hybrid或vlm后端，pipeline默认关闭。
    image_analysis=_get_bool("MINERU_IMAGE_ANALYSIS", False),

    # 同时保留结构化结果，后面用于升级切片。
    return_middle_json=_get_bool("MINERU_RETURN_MIDDLE_JSON", True),
    return_content_list=_get_bool("MINERU_RETURN_CONTENT_LIST", True),

    poll_interval_seconds=_get_int("MINERU_POLL_INTERVAL_SECONDS", 3),
    task_timeout_seconds=_get_int("MINERU_TASK_TIMEOUT_SECONDS", 3600),
    request_timeout_seconds=_get_int("MINERU_REQUEST_TIMEOUT_SECONDS", 60),
    download_timeout_seconds=_get_int("MINERU_DOWNLOAD_TIMEOUT_SECONDS", 600),
    verify_ssl=_get_bool("MINERU_VERIFY_SSL", True)
)
