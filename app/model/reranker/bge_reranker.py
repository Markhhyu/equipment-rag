from typing import Sequence

from FlagEmbedding import FlagReranker

from app.conf.reranker_config import RerankerConfig
from app.core.logger import logger
from app.model.reranker.base import BaseReranker, SentencePair, convert_scores_to_list


class BgeReranker(BaseReranker):
    """
    BGE Reranker适配器。

    默认使用BAAI/bge-reranker-v2-m3，继续作为当前生产基线。
    """

    def __init__(self, config: RerankerConfig):
        super().__init__("bge", config.model_name_or_path)
        self.config = config

        # CPU通常不适合FP16，因此只有CUDA环境才真正启用。
        effective_fp16 = config.use_fp16 and config.device.startswith("cuda")

        logger.info(
            f"开始初始化BGE Reranker，model={config.model_name_or_path}，"
            f"device={config.device}，fp16={effective_fp16}"
        )

        # devices是新版FlagEmbedding使用的设备参数。
        self.model = FlagReranker(
            config.model_name_or_path,
            devices=config.device,
            use_fp16=effective_fp16
        )

        logger.info("BGE Reranker初始化完成")

    def compute_score(self, sentence_pairs: Sequence[SentencePair]) -> list[float]:
        """
        使用BGE计算候选文档相关性分数。

        normalize=False时返回原始Logit分数；
        normalize=True时通过Sigmoid映射到0～1。
        """

        if not sentence_pairs:
            return []

        scores = self.model.compute_score(
            sentence_pairs,
            batch_size=self.config.batch_size,
            max_length=self.config.max_length,
            normalize=self.config.normalize_score
        )

        return convert_scores_to_list(scores)