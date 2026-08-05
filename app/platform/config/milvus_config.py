"""Milvus connection and collection configuration."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


# 本地开发从 .env 读取；Compose 运行时由容器环境变量覆盖。
load_dotenv()


@dataclass
class MilvusConfig:
    """Milvus 连接地址和三个业务集合名称。"""

    milvus_url: str | None  # Docker 内使用 milvus:19530；宿主机直连使用 127.0.0.1:19530。
    chunks_collection: str | None  # 文档切片及 Dense/Sparse 向量集合。
    entity_name_collection: str | None  # 从文档识别出的通用实体名称集合。
    item_name_collection: str | None  # 设备/物料名称集合，用于问题中的设备确认。


milvus_config = MilvusConfig(
    milvus_url=os.getenv("MILVUS_URL"),
    chunks_collection=os.getenv("CHUNKS_COLLECTION"),
    entity_name_collection=os.getenv("ENTITY_NAME_COLLECTION"),
    item_name_collection=os.getenv("ITEM_NAME_COLLECTION"),
)
