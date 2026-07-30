from app.model.reranker.factory import get_reranker


def get_reranker_model():
    """
    兼容原有业务代码的Reranker获取方法。

    原来的node_rerank不需要立即修改，内部已经切换到新Provider工厂。
    """

    return get_reranker()