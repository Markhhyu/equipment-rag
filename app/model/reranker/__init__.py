"""
Reranker统一入口。

业务代码应通过factory获取模型，不直接初始化具体实现。
"""

from app.model.reranker.factory import get_reranker, get_reranker_info

__all__ = ["get_reranker", "get_reranker_info"]