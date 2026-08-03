import os
from datetime import datetime
from typing import Dict, List, Optional

from pymongo import ASCENDING, MongoClient
from dotenv import load_dotenv

from app.conf.image_processing_config import image_processing_config
from app.core.logger import logger


# 加载环境变量，保证独立运行该工具时也可以正常读取 MongoDB 配置。
load_dotenv()


class ImageAssetMongoTool:
    """
    文档图片资产 MongoDB 管理工具。

    设计目的：
    1. 图片本身存放 MinIO，MongoDB 只保存图片元数据和处理状态；
    2. 导入阶段先记录图片资产，不等待视觉模型；
    3. 后台任务根据 visual_status=pending 异步补充图片理解结果；
    4. 查询阶段可以通过 document_id、chunk 关联找到需要展示或分析的图片。
    """

    def __init__(self):
        mongo_url = os.getenv("MONGO_URL")
        db_name = os.getenv("MONGO_DB_NAME") or "equipment_rag"

        self.client = MongoClient(mongo_url)
        self.db = self.client[db_name]
        self.collection = self.db[image_processing_config.asset_collection]

        # 文档维度查询用于后台扫描待处理图片；图片哈希索引用于避免重复处理相同图片。
        self.collection.create_index([("document_id", ASCENDING), ("visual_status", ASCENDING)])
        self.collection.create_index([("content_hash", ASCENDING)], sparse=True)

        logger.info(f"图片资产MongoDB初始化完成，集合={image_processing_config.asset_collection}")

    def save_assets(self, assets: List[Dict]) -> int:
        """
        批量保存图片资产。

        使用 upsert 而不是直接 insert，避免任务重试时生成大量重复图片记录。
        """
        if not assets:
            return 0

        count = 0
        for asset in assets:
            if not asset.get("image_id"):
                continue

            result = self.collection.update_one(
                {"image_id": asset["image_id"]},
                {
                    "$set": asset,
                    "$setOnInsert": {
                        "created_at": datetime.now().timestamp(),
                    },
                },
                upsert=True,
            )
            if result.upserted_id or result.modified_count:
                count += 1

        return count

    def list_pending_assets(self, limit: int = 30) -> List[Dict]:
        """
        查询等待视觉增强的图片。

        后台 worker 不直接扫描文件系统，而是依赖该状态表实现可恢复处理。
        """
        return list(
            self.collection.find({"visual_status": "pending"})
            .sort("created_at", ASCENDING)
            .limit(limit)
        )

    def update_visual_result(self, image_id: str, description: str, status: str = "completed") -> None:
        """
        更新图片视觉理解结果。

        即使视觉模型调用失败，也只更新当前图片状态，不影响整个文档知识库。
        """
        self.collection.update_one(
            {"image_id": image_id},
            {
                "$set": {
                    "visual_description": description or "",
                    "visual_status": status,
                    "updated_at": datetime.now().timestamp(),
                }
            },
        )


_image_asset_tool: Optional[ImageAssetMongoTool] = None


def get_image_asset_tool() -> ImageAssetMongoTool:
    """获取图片资产 MongoDB 单例，避免每次处理图片重新创建数据库连接。"""
    global _image_asset_tool
    if _image_asset_tool is None:
        _image_asset_tool = ImageAssetMongoTool()
    return _image_asset_tool
