from typing import Sequence

import torch
from sentence_transformers import CrossEncoder

from app.conf.reranker_config import RerankerConfig
from app.core.logger import logger
from app.model.reranker.base import BaseReranker, SentencePair, convert_scores_to_list


class QwenReranker(BaseReranker):
    """
    Qwen3 Reranker适配器。

    当前仅作为候选模型，不直接替换BGE生产基线。
    """

    def __init__(self, config: RerankerConfig):
        super().__init__("qwen", config.model_name_or_path)
        self.config = config

        # CUDA且开启FP16时使用float16；其他情况使用float32。
        torch_dtype = torch.float16 if config.use_fp16 and config.device.startswith("cuda") else torch.float32

        logger.info(
            f"开始初始化Qwen3 Reranker，model={config.model_name_or_path}，"
            f"device={config.device}，dtype={torch_dtype}"
        )

        # Qwen官方支持通过Sentence Transformers的CrossEncoder加载。
        self.model = CrossEncoder(
            config.model_name_or_path,
            device=config.device,
            max_length=config.max_length,
            model_kwargs={"torch_dtype": torch_dtype}
        )

        logger.info("Qwen3 Reranker初始化完成")

    def compute_score(self, sentence_pairs: Sequence[SentencePair]) -> list[float]:
        """
        使用Qwen3计算候选文档相关性分数。

        为保持与BGE当前逻辑一致，默认返回原始分数。
        开启normalize_score时才使用Sigmoid转换为0～1。
        """

        if not sentence_pairs:
            return []

        activation_fn = torch.nn.Sigmoid() if self.config.normalize_score else torch.nn.Identity()

        scores = self.model.predict(
            sentence_pairs,
            batch_size=self.config.batch_size,
            show_progress_bar=False,
            activation_fn=activation_fn,
            convert_to_numpy=True
        )

        return convert_scores_to_list(scores)