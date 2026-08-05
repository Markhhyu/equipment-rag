"""Embedding model configuration."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


# 加载 .env 后再创建全局配置对象，确保所有工作流节点读取同一份值。
load_dotenv()


@dataclass
class EmbeddingConfig:
    """BGE-M3 文本向量模型配置。"""

    bge_m3_path: str | None  # 已下载模型的绝对路径；为空时按仓库 ID 联网下载。
    bge_m3: str | None  # Hugging Face/ModelScope 可识别的模型仓库 ID。
    bge_device: str | None  # cpu 兼容性最好；cuda:0 表示第一张 NVIDIA GPU。
    bge_fp16: bool  # 半精度只建议在 CUDA 上启用，CPU 应保持 false。


# 全局对象只保存配置，不会在导入本模块时立即下载模型。
embedding_config = EmbeddingConfig(
    bge_m3_path=os.getenv("BGE_M3_PATH"),
    bge_m3=os.getenv("BGE_M3"),
    bge_device=os.getenv("BGE_DEVICE"),
    # 同时兼容 1、True 和 true，避免不同部署系统的布尔格式差异。
    bge_fp16=os.getenv("BGE_FP16") in ("1", "True", "true", 1),
)
