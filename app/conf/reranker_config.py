import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv


# 加载项目根目录中的.env配置。
load_dotenv(find_dotenv())


def _get_bool(name: str, default: bool = False) -> bool:
    """
    从环境变量读取布尔值。

    支持：
    true、false、1、0、yes、no、on、off。
    """

    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int) -> int:
    """
    从环境变量读取整数。

    配置不存在或格式错误时返回默认值，避免服务启动失败。
    """

    value = os.getenv(name)

    if not value:
        return default

    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class RerankerConfig:
    """
    Reranker统一配置。

    provider决定使用哪个实现：
    1. bge：BAAI的BGE Reranker；
    2. qwen：Qwen3 Reranker。
    """

    provider: str
    model_name_or_path: str
    device: str
    use_fp16: bool
    batch_size: int
    max_length: int
    normalize_score: bool

    # 以下三个属性用于兼容旧代码，后续确认没有旧调用后再删除。
    @property
    def bge_reranker_large(self) -> str:
        return self.model_name_or_path

    @property
    def bge_reranker_device(self) -> str:
        return self.device

    @property
    def bge_reranker_fp16(self) -> bool:
        return self.use_fp16


# 默认继续使用BGE，确保本次重构不会直接改变生产效果。
_provider = (os.getenv("RERANKER_PROVIDER") or "bge").strip().lower()

# 优先读取新的通用配置，未配置时兼容原来的BGE配置。
_default_model = "Qwen/Qwen3-Reranker-0.6B" if _provider == "qwen" else "BAAI/bge-reranker-v2-m3"
_model_name = os.getenv("RERANKER_MODEL") or os.getenv("BGE_RERANKER_LARGE") or _default_model
_device = os.getenv("RERANKER_DEVICE") or os.getenv("BGE_RERANKER_DEVICE") or "cpu"

# 新配置优先，旧配置作为兼容兜底。
if os.getenv("RERANKER_USE_FP16") is not None:
    _use_fp16 = _get_bool("RERANKER_USE_FP16")
else:
    _use_fp16 = _get_bool("BGE_RERANKER_FP16")

reranker_config = RerankerConfig(
    provider=_provider,
    model_name_or_path=_model_name,
    device=_device,
    use_fp16=_use_fp16,
    batch_size=_get_int("RERANKER_BATCH_SIZE", 8),
    max_length=_get_int("RERANKER_MAX_LENGTH", 512),

    # 当前动态TopK使用原始分数计算分数断崖，因此暂时保持原始分数。
    normalize_score=_get_bool("RERANKER_NORMALIZE_SCORE", False)
)