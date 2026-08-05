from typing import Sequence

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from app.platform.config.reranker_config import RerankerConfig
from app.platform.observability.logging import logger
from app.platform.ai.reranking.base import BaseReranker, SentencePair


class BgeReranker(BaseReranker):
    """基于Transformers原生接口的BGE Reranker实现。"""

    def __init__(self, config: RerankerConfig):
        super().__init__("bge", config.model_name_or_path)
        self.config = config
        self.device = torch.device(config.device)
        self.use_fp16 = config.use_fp16 and config.device.startswith("cuda")

        logger.info(
            f"开始初始化BGE Reranker，model={config.model_name_or_path}，"
            f"device={config.device}，fp16={self.use_fp16}"
        )

        self.tokenizer = AutoTokenizer.from_pretrained(config.model_name_or_path)
        self.model = AutoModelForSequenceClassification.from_pretrained(config.model_name_or_path)

        if self.use_fp16:
            self.model.half()

        self.model.to(self.device)
        self.model.eval()

        logger.info("BGE Reranker初始化完成")

    def compute_score(self, sentence_pairs: Sequence[SentencePair]) -> list[float]:
        if not sentence_pairs:
            return []

        pairs = [[str(pair[0]), str(pair[1])] for pair in sentence_pairs]
        scores: list[float] = []

        for start in range(0, len(pairs), self.config.batch_size):
            batch = pairs[start:start + self.config.batch_size]

            inputs = self.tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=self.config.max_length,
                return_tensors="pt",
            )
            inputs = {name: value.to(self.device) for name, value in inputs.items()}

            with torch.inference_mode():
                logits = self.model(
                    **inputs,
                    return_dict=True,
                ).logits.view(-1).float()

                if self.config.normalize_score:
                    logits = torch.sigmoid(logits)

            scores.extend(logits.cpu().tolist())

        return scores
