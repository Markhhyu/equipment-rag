import threading
from typing import Optional

from app.conf.reranker_config import reranker_config
from app.core.logger import logger
from app.model.reranker.base import BaseReranker
from app.model.reranker.bge_reranker import BgeReranker
from app.model.reranker.qwen_reranker import QwenReranker


# 缓存已经初始化的模型，避免每次问答重复加载。
_cached_reranker: Optional[BaseReranker] = None
_cached_key: Optional[tuple] = None

# 多线程环境下保护模型初始化过程。
_reranker_lock = threading.Lock()


def _build_cache_key() -> tuple:
    """根据当前配置构造模型缓存键。"""

    return (
        reranker_config.provider,
        reranker_config.model_name_or_path,
        reranker_config.device,
        reranker_config.use_fp16,
        reranker_config.batch_size,
        reranker_config.max_length,
        reranker_config.normalize_score
    )


def _create_reranker() -> BaseReranker:
    """根据provider创建对应的Reranker实现。"""

    provider = reranker_config.provider

    if provider == "bge":
        return BgeReranker(reranker_config)

    if provider == "qwen":
        return QwenReranker(reranker_config)

    raise ValueError(f"不支持的Reranker Provider：{provider}，当前仅支持bge和qwen")


def get_reranker() -> BaseReranker:
    """
    获取Reranker单例。

    第一次调用时加载模型，后续调用直接返回缓存对象。
    """

    global _cached_reranker, _cached_key

    cache_key = _build_cache_key()

    if _cached_reranker is not None and _cached_key == cache_key:
        return _cached_reranker

    with _reranker_lock:
        # 获取锁后再次检查，避免多个线程重复加载模型。
        if _cached_reranker is not None and _cached_key == cache_key:
            return _cached_reranker

        logger.info(
            f"创建Reranker实例，provider={reranker_config.provider}，"
            f"model={reranker_config.model_name_or_path}"
        )

        _cached_reranker = _create_reranker()
        _cached_key = cache_key
        return _cached_reranker


def get_reranker_info() -> dict:
    """返回当前配置的Reranker信息，不触发模型加载。"""

    return {
        "reranker_provider": reranker_config.provider,
        "reranker_model": reranker_config.model_name_or_path,
        "reranker_device": reranker_config.device,
        "reranker_fp16": reranker_config.use_fp16,
        "reranker_batch_size": reranker_config.batch_size,
        "reranker_max_length": reranker_config.max_length,
        "reranker_normalized": reranker_config.normalize_score
    }


def clear_reranker_cache() -> None:
    """
    清空当前进程中的模型缓存。

    主要用于自动化测试，正常服务运行时不应频繁调用。
    """

    global _cached_reranker, _cached_key

    _cached_reranker = None
    _cached_key = None