import os
from datetime import datetime
from typing import Dict, List, Optional

from pymongo import ASCENDING, MongoClient
from dotenv import load_dotenv

from app.conf.image_processing_config import image_processing_config
from app.core.logger import logger


load_dotenv()


class ImageAssetMongoTool:
    """
    文档图片资产 MongoDB 管理工具。

    图片处理采用异步增强模式：
    1. 导入阶段只保存图片资产，不等待视觉模型返回；
    2. 后台 Worker 根据状态领取图片任务；
    3. 图片处理完成后更新视觉描述；
    4. 查询阶段可以根据图片资产关联上下文。

    通过 MongoDB 保存状态，可以支持服务重启恢复和多 Worker 并行处理。
    """

    def __init__(self):
        mongo_url = os.getenv("MONGO_URL")
        db_name = os.getenv("MONGO_DB_NAME") or "equipment_rag"

        self.client = MongoClient(mongo_url)
        self.db = self.client[db_name]
        self.collection = self.db[image_processing_config.asset_collection]

        # document_id + 状态用于后台任务扫描；image_id保证图片唯一。
        self.collection.create_index([("document_id", ASCENDING), ("visual_status", ASCENDING)])
        self.collection.create_index([("image_id", ASCENDING)], unique=True)
        self.collection.create_index([("content_hash", ASCENDING)], sparse=True)

        logger.info(f"图片资产MongoDB初始化完成，集合={image_processing_config.asset_collection}")

    def save_assets(self, assets: List[Dict]) -> int:
        """
        批量保存图片资产。

        使用upsert避免导入任务重试时产生重复图片记录。
        """
        if not assets:
            return 0

        count = 0
        for asset in assets:
            image_id = asset.get("image_id")
            if not image_id:
                continue

            asset.setdefault("visual_status", "pending")
            asset.setdefault("retry_count", 0)
            asset.setdefault("created_at", datetime.now().timestamp())

            result = self.collection.update_one(
                {"image_id": image_id},
                {"$setOnInsert": asset},
                upsert=True,
            )
            if result.upserted_id:
                count += 1

        return count

    def claim_pending_assets(self, worker_id: str, limit: int = 10) -> List[Dict]:
        """
        原子领取待处理图片。

        领取过程使用processing状态和租约时间，避免多个Worker同时处理同一张图片。
        如果Worker异常退出，租约过期后图片可以重新进入处理队列。
        """
        now = datetime.now().timestamp()
        expire_before = now - image_processing_config.enrichment_lease_seconds
        result = []

        cursor = self.collection.find(
            {
                "$or": [
                    {"visual_status": "pending"},
                    {
                        "visual_status": "processing",
                        "lock_time": {"$lt": expire_before},
                    },
                ]
            }
        ).limit(limit)

        for item in cursor:
            updated = self.collection.find_one_and_update(
                {
                    "image_id": item["image_id"],
                    "visual_status": item["visual_status"],
                },
                {
                    "$set": {
                        "visual_status": "processing",
                        "worker_id": worker_id,
                        "lock_time": now,
                        "updated_at": now,
                    }
                },
                return_document=True,
            )
            if updated:
                result.append(updated)

        return result

    def update_visual_result(self, image_id: str, description: str, status: str = "completed", error: str = "") -> None:
        """
        更新图片增强结果。

        图片级失败不会影响整个知识库导入，只记录当前图片失败原因。
        """
        self.collection.update_one(
            {"image_id": image_id},
            {
                "$set": {
                    "visual_description": description or "",
                    "visual_status": status,
                    "error_message": error,
                    "updated_at": datetime.now().timestamp(),
                },
                "$inc": {"retry_count": 1} if status == "failed" else {},
            },
        )

    def get_document_progress(self, document_id: str) -> Dict:
        """统计某个文档的图片增强进度，用于前端展示异步处理状态。"""
        total = self.collection.count_documents({"document_id": document_id})
        completed = self.collection.count_documents({"document_id": document_id, "visual_status": "completed"})
        return {
            "total": total,
            "completed": completed,
            "pending": max(total - completed, 0),
        }


_image_asset_tool: Optional[ImageAssetMongoTool] = None


def get_image_asset_tool() -> ImageAssetMongoTool:
    """获取图片资产MongoDB单例，避免重复创建数据库连接。"""
    global _image_asset_tool
    if _image_asset_tool is None:
        _image_asset_tool = ImageAssetMongoTool()
    return _image_asset_tool
