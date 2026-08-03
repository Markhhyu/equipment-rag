import os
from datetime import datetime
from typing import Dict, List, Optional

from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient, ReturnDocument

from app.conf.image_processing_config import image_processing_config
from app.core.logger import logger


load_dotenv()


class ImageAssetMongoTool:
    """
    文档图片资产 MongoDB 管理工具。

    图片处理采用异步增强模式：
    1. 导入阶段只保存图片资产，不等待视觉模型返回；
    2. 后台 Worker 根据状态原子领取图片任务；
    3. 图片处理完成后更新视觉描述，失败时按配置决定重试或终止；
    4. 查询阶段可以根据文档、图片编号和处理状态获取图片资产。

    任务状态保存在 MongoDB 中，因此服务重启后可以继续处理，也支持多个 Worker 并行运行。
    """

    def __init__(self):
        mongo_url = os.getenv("MONGO_URL")
        db_name = os.getenv("MONGO_DB_NAME") or "equipment_rag"
        if not mongo_url:
            raise ValueError("未配置MONGO_URL，无法初始化图片资产存储")

        self.client = MongoClient(mongo_url)
        self.db = self.client[db_name]
        self.collection = self.db[image_processing_config.asset_collection]

        # 文档与状态索引用于后台任务扫描；image_id唯一索引用于保证导入重试不会产生重复记录。
        self.collection.create_index([("document_id", ASCENDING), ("visual_status", ASCENDING)])
        self.collection.create_index([("tenant_id", ASCENDING), ("document_id", ASCENDING)])
        self.collection.create_index([("image_id", ASCENDING)], unique=True)
        self.collection.create_index([("content_hash", ASCENDING)], sparse=True)
        self.collection.create_index([("visual_status", ASCENDING), ("lock_time", ASCENDING)])

        logger.info(f"图片资产MongoDB初始化完成，集合={image_processing_config.asset_collection}")

    def save_assets(self, assets: List[Dict]) -> int:
        """
        批量保存图片资产。

        使用 upsert 和 $setOnInsert 保留已经完成的视觉结果，避免文档任务恢复或重复执行时把状态重新覆盖为 pending。
        """
        if not assets:
            return 0

        inserted_count = 0
        for asset in assets:
            image_id = str(asset.get("image_id") or "").strip()
            if not image_id:
                logger.warning("图片资产缺少image_id，已跳过保存")
                continue

            document = dict(asset)
            document.setdefault("visual_status", "pending")
            document.setdefault("retry_count", 0)
            document.setdefault("error_message", "")
            document.setdefault("created_at", datetime.now().timestamp())
            document.setdefault("updated_at", document["created_at"])

            result = self.collection.update_one(
                {"image_id": image_id},
                {"$setOnInsert": document},
                upsert=True,
            )
            if result.upserted_id:
                inserted_count += 1

        return inserted_count

    def claim_pending_assets(self, worker_id: str, limit: int = 10) -> List[Dict]:
        """
        原子领取等待视觉增强的图片。

        每次通过 find_one_and_update 领取一条记录，只有成功把状态改为 processing 的 Worker 才能得到任务。
        processing 状态超过租约时间后允许重新领取，用于恢复进程崩溃或容器重启遗留的任务。
        """
        safe_worker_id = str(worker_id or "").strip()
        if not safe_worker_id:
            raise ValueError("worker_id不能为空")

        target_count = max(int(limit or 0), 0)
        claimed_assets: List[Dict] = []
        for _ in range(target_count):
            now = datetime.now().timestamp()
            expire_before = now - image_processing_config.enrichment_lease_seconds
            eligible_filter = {
                "retry_count": {"$lte": image_processing_config.caption_max_retries},
                "$or": [
                    {"visual_status": "pending"},
                    {"visual_status": "processing", "lock_time": {"$lt": expire_before}},
                ],
            }
            asset = self.collection.find_one_and_update(
                eligible_filter,
                {
                    "$set": {
                        "visual_status": "processing",
                        "worker_id": safe_worker_id,
                        "lock_time": now,
                        "updated_at": now,
                        "error_message": "",
                    }
                },
                sort=[("created_at", ASCENDING)],
                return_document=ReturnDocument.AFTER,
            )
            if not asset:
                break
            claimed_assets.append(asset)

        return claimed_assets

    def complete_visual_result(self, image_id: str, worker_id: str, description: str) -> bool:
        """
        完成图片视觉增强任务。

        更新条件同时校验 image_id、worker_id 和 processing 状态，防止租约过期后旧 Worker 覆盖新 Worker 已写入的结果。
        """
        now = datetime.now().timestamp()
        result = self.collection.update_one(
            {"image_id": image_id, "worker_id": worker_id, "visual_status": "processing"},
            {
                "$set": {
                    "visual_description": str(description or "").strip(),
                    "visual_status": "completed",
                    "status_reason": "视觉增强完成",
                    "error_message": "",
                    "updated_at": now,
                    "completed_at": now,
                },
                "$unset": {"worker_id": "", "lock_time": ""},
            },
        )
        return result.modified_count > 0

    def fail_visual_result(self, image_id: str, worker_id: str, error: str) -> str:
        """
        记录图片视觉增强失败结果，并根据最大重试次数决定后续状态。

        retry_count 表示已经失败的次数。未超过配置上限时重新设置为 pending，超过上限后固定为 failed，避免异常图片无限循环调用模型。
        """
        asset = self.collection.find_one(
            {"image_id": image_id, "worker_id": worker_id, "visual_status": "processing"},
            {"retry_count": 1},
        )
        if not asset:
            logger.warning(f"图片失败状态未更新，任务可能已被其他Worker重新领取：image_id={image_id}")
            return "ignored"

        retry_count = int(asset.get("retry_count") or 0) + 1
        next_status = "pending" if retry_count <= image_processing_config.caption_max_retries else "failed"
        now = datetime.now().timestamp()
        result = self.collection.update_one(
            {"image_id": image_id, "worker_id": worker_id, "visual_status": "processing"},
            {
                "$set": {
                    "visual_status": next_status,
                    "retry_count": retry_count,
                    "error_message": str(error or "")[:1000],
                    "status_reason": "等待重试" if next_status == "pending" else "超过最大重试次数",
                    "updated_at": now,
                },
                "$unset": {"worker_id": "", "lock_time": ""},
            },
        )
        return next_status if result.modified_count > 0 else "ignored"

    def get_document_progress(self, document_id: str) -> Dict:
        """统计某个文档各类图片状态，供接口和前端展示异步增强进度。"""
        match = {"document_id": document_id}
        pipeline = [
            {"$match": match},
            {"$group": {"_id": "$visual_status", "count": {"$sum": 1}}},
        ]
        counts = {str(item.get("_id") or "unknown"): int(item.get("count") or 0) for item in self.collection.aggregate(pipeline)}
        total = sum(counts.values())
        finished = counts.get("completed", 0) + counts.get("skipped", 0) + counts.get("failed", 0)
        return {
            "document_id": document_id,
            "total": total,
            "finished": finished,
            "pending": counts.get("pending", 0),
            "processing": counts.get("processing", 0),
            "completed": counts.get("completed", 0),
            "skipped": counts.get("skipped", 0),
            "failed": counts.get("failed", 0),
            "is_finished": total > 0 and finished == total,
        }

    def list_document_assets(self, tenant_id: str, document_id: str, limit: int = 200) -> List[Dict]:
        """按租户和文档查询图片资产，返回顺序优先按页码排列。"""
        return list(
            self.collection.find({"tenant_id": tenant_id, "document_id": document_id})
            .sort([("page_number", ASCENDING), ("filename", ASCENDING)])
            .limit(max(int(limit or 0), 0))
        )


_image_asset_tool: Optional[ImageAssetMongoTool] = None


def get_image_asset_tool() -> ImageAssetMongoTool:
    """获取图片资产MongoDB单例，避免重复创建数据库连接。"""
    global _image_asset_tool
    if _image_asset_tool is None:
        _image_asset_tool = ImageAssetMongoTool()
    return _image_asset_tool
