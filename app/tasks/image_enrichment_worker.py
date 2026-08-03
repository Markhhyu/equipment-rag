from __future__ import annotations

import base64
import mimetypes
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from app.clients.image_asset_mongo_utils import get_image_asset_tool
from app.conf.image_processing_config import image_processing_config
from app.core.logger import logger
from app.lm.lm_utils import get_llm_client


_worker_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _read_image_base64(image_path: str) -> str:
    """读取本地图片并转换为Base64，供视觉模型识别。"""
    with open(image_path, "rb") as file:
        return base64.b64encode(file.read()).decode("utf-8")


def _build_image_prompt(asset: dict) -> str:
    """
    构造图片理解提示词。

    提示模型重点关注设备手册中的有效信息，例如：
    设备结构、操作界面字段、接口位置、拓扑关系和异常提示。
    """
    return f"""
你正在分析设备技术手册中的一张图片。
请根据图片内容生成简洁、准确的中文描述。
重点说明：
1. 图片展示的设备、界面、拓扑或结构；
2. 关键按钮、字段、接口、参数或连接关系；
3. 对设备维护人员有帮助的信息。

已有上下文：
{asset.get('context_before', '')}
{asset.get('context_after', '')}

不要编造图片中不存在的信息。
"""


def _generate_visual_description(asset: dict) -> str:
    """
    调用视觉大模型生成图片描述。

    单张图片失败不会影响其他图片任务，异常由Worker捕获并写回MongoDB。
    """
    object_uri = str(asset.get("object_uri") or "")
    if not object_uri.startswith("file://") and not asset.get("local_path"):
        raise ValueError("当前图片资产没有可供视觉模型读取的本地路径")

    image_path = str(asset.get("local_path"))
    image_base64 = _read_image_base64(image_path)
    mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"

    message = {
        "type": "image_url",
        "image_url": {
            "url": f"data:{mime_type};base64,{image_base64}"
        },
    }

    llm = get_llm_client(model=os.getenv("VL_MODEL"))
    response = llm.invoke([
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": _build_image_prompt(asset),
                },
                message,
            ],
        }
    ])
    return str(getattr(response, "content", "") or "").strip()


def process_image_enrichment_once(worker_id: str | None = None) -> int:
    """
    执行一次图片增强扫描。

    返回本次完成处理的图片数量。
    定时任务和服务启动任务都可以复用该方法。
    """
    worker_id = worker_id or f"image-worker-{uuid.uuid4().hex[:8]}"
    tool = get_image_asset_tool()
    assets = tool.claim_pending_assets(worker_id, limit=10)
    if not assets:
        return 0

    completed = 0
    for asset in assets:
        image_id = asset.get("image_id")
        try:
            description = _generate_visual_description(asset)
            if tool.complete_visual_result(image_id, worker_id, description):
                completed += 1
                logger.info(f"图片视觉增强完成：image_id={image_id}")
        except Exception as exc:
            status = tool.fail_visual_result(image_id, worker_id, str(exc))
            logger.error(f"图片视觉增强失败：image_id={image_id}，后续状态={status}，原因={exc}")

    return completed


def _worker_loop():
    """
    后台常驻Worker循环。

    没有待处理图片时主动休眠，避免持续查询MongoDB。
    服务关闭时通过stop_event安全退出。
    """
    worker_id = f"image-worker-{uuid.uuid4().hex[:8]}"
    logger.info(f"图片视觉增强Worker启动：{worker_id}")

    while not _stop_event.is_set():
        try:
            processed = process_image_enrichment_once(worker_id)
            if processed == 0:
                _stop_event.wait(image_processing_config.enrichment_poll_seconds)
        except Exception as exc:
            logger.error(f"图片视觉增强Worker异常：{exc}", exc_info=True)
            _stop_event.wait(image_processing_config.enrichment_poll_seconds)

    logger.info(f"图片视觉增强Worker停止：{worker_id}")


def start_image_enrichment_worker() -> None:
    """启动图片视觉增强后台线程，重复调用不会创建多个Worker。"""
    global _worker_thread
    if not image_processing_config.enrichment_async:
        logger.info("图片异步增强已关闭，不启动Worker")
        return
    if _worker_thread and _worker_thread.is_alive():
        return

    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop,
        name="image-enrichment-worker",
        daemon=True,
    )
    _worker_thread.start()


def stop_image_enrichment_worker() -> None:
    """服务退出前通知Worker停止。"""
    _stop_event.set()
