"""Deprecated compatibility entrypoint for the platform reranker."""

from app.platform.ai.reranking.factory import get_reranker


def get_reranker_model():
    """
    兼容原有业务代码的Reranker获取方法。

    旧调用方可以继续使用该名称，正式代码应直接调用 get_reranker。
    """

    return get_reranker()
