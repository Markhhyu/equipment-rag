import os
from dataclasses import dataclass

from dotenv import load_dotenv


# 统一从项目根目录加载环境变量。Docker 注入的系统环境变量优先级更高，不会被 .env 覆盖。
load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    """读取布尔配置，支持 true/false、1/0、yes/no、on/off 等常见写法。"""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int, minimum: int = 0) -> int:
    """读取整数配置；值为空、格式错误或小于下限时使用默认值。"""
    value = os.getenv(name)
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed >= minimum else default


def _get_mode() -> str:
    """
    读取图片处理模式。

    off：只保存和关联图片，不执行视觉模型增强；
    smart：优先使用图注和上下文，仅把缺少有效说明的图片交给视觉模型；
    all：所有满足基础条件的图片都进入视觉增强队列，主要用于效果对比和小文档测试。
    """
    mode = (os.getenv("IMAGE_PROCESS_MODE") or "smart").strip().lower()
    if mode not in {"off", "smart", "all"}:
        raise ValueError(f"不支持的 IMAGE_PROCESS_MODE={mode}，可选值：off、smart、all")
    return mode


@dataclass(frozen=True)
class ImageProcessingConfig:
    """文档图片资产化、异步视觉增强和查询时视觉分析的统一配置。"""

    process_mode: str
    asset_collection: str
    enrichment_async: bool
    enrichment_workers: int
    caption_max_per_document: int
    caption_timeout_seconds: int
    caption_max_retries: int
    caption_requests_per_minute: int
    min_image_bytes: int
    strong_caption_min_chars: int
    query_vision_enabled: bool
    query_image_top_k: int
    query_vision_timeout_seconds: int


image_processing_config = ImageProcessingConfig(
    process_mode=_get_mode(),
    asset_collection=(os.getenv("IMAGE_ASSET_COLLECTION") or "document_image_assets").strip(),
    enrichment_async=_get_bool("IMAGE_ENRICHMENT_ASYNC", True),
    enrichment_workers=_get_int("IMAGE_ENRICHMENT_WORKERS", 2, minimum=1),
    caption_max_per_document=_get_int("IMAGE_CAPTION_MAX_PER_DOCUMENT", 30, minimum=1),
    caption_timeout_seconds=_get_int("IMAGE_CAPTION_TIMEOUT_SECONDS", 45, minimum=5),
    caption_max_retries=_get_int("IMAGE_CAPTION_MAX_RETRIES", 1, minimum=0),
    caption_requests_per_minute=_get_int("IMAGE_CAPTION_REQUESTS_PER_MINUTE", 30, minimum=1),
    # 图片文件过小时大概率是图标、Logo、页眉或装饰元素，默认不消耗视觉模型额度。
    min_image_bytes=_get_int("IMAGE_MIN_BYTES", 8192, minimum=0),
    # 图注或上下文达到一定长度后，smart 模式认为已有足够语义，不再重复调用视觉模型。
    strong_caption_min_chars=_get_int("IMAGE_STRONG_CAPTION_MIN_CHARS", 12, minimum=1),
    query_vision_enabled=_get_bool("QUERY_IMAGE_VISION_ENABLED", True),
    query_image_top_k=_get_int("QUERY_IMAGE_TOP_K", 3, minimum=1),
    query_vision_timeout_seconds=_get_int("QUERY_IMAGE_VISION_TIMEOUT_SECONDS", 45, minimum=5),
)
