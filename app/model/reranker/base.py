from abc import ABC, abstractmethod
from typing import Any, Sequence


# 一个句对由“问题”和“候选文档”组成。
SentencePair = Sequence[str]


def convert_scores_to_list(scores: Any) -> list[float]:
    """
    将不同模型返回的分数统一转换为list[float]。

    不同框架可能返回：
    1. Python浮点数；
    2. list；
    3. NumPy数组；
    4. Torch Tensor。
    """

    # NumPy数组和Torch Tensor通常都支持tolist()。
    if hasattr(scores, "tolist"):
        scores = scores.tolist()

    # 单个句对可能只返回一个浮点数。
    if isinstance(scores, (int, float)):
        return [float(scores)]

    return [float(score) for score in scores]


class BaseReranker(ABC):
    """
    所有Reranker实现必须遵循的统一接口。

    上层业务只调用compute_score，不需要知道底层使用BGE还是Qwen。
    """

    def __init__(self, provider: str, model_name: str):
        self.provider = provider
        self.model_name = model_name

    @abstractmethod
    def compute_score(self, sentence_pairs: Sequence[SentencePair]) -> list[float]:
        """
        计算问题与候选文档之间的相关性分数。

        :param sentence_pairs: [[query, document], ...]
        :return: 与输入顺序一一对应的分数列表
        """

        raise NotImplementedError

    def get_info(self) -> dict:
        """返回当前Reranker的基础信息，供日志和Langfuse记录。"""

        return {
            "provider": self.provider,
            "model": self.model_name
        }