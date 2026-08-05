"""Background enrichment of document images with model-generated metadata."""

from __future__ import annotations

import base64
import mimetypes
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

from langchain.messages import HumanMessage

from app.modules.knowledge.application.image_assets import get_image_asset_tool
from app.platform.config.image_processing_config import image_processing_config
from app.platform.storage.minio import download_minio_object
from app.platform.observability.logging import logger
from app.platform.ai.chat import get_llm_client
from app.platform.ai.prompts import load_prompt
from app.platform.observability.langfuse_monitor import trace_image_enrichment
from app.platform.observability.rag_observability import start_rag_observation, summarize_image_assets


_worker_threads: list[threading.Thread] = []
_worker_lock = threading.Lock()
_stop_event = threading.Event()
_request_times: deque[float] = deque()
_rate_limit_lock = threading.Lock()


def _read_image_base64(image_path: str) -> str:
    """读取临时图片并在Base64转换前重新校验文件存在性和体积。"""
    path = Path(image_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"视觉增强临时图片不存在：{image_path}")

    file_size = path.stat().st_size
    if file_size <= 0:
        raise ValueError(f"视觉增强图片为空文件：{image_path}")
    if file_size > image_processing_config.max_image_bytes:
        raise ValueError(
            f"图片大小{file_size}字节，超过视觉增强上限{image_processing_config.max_image_bytes}字节"
        )

    with path.open("rb") as file:
        return base64.b64encode(file.read()).decode("utf-8")


def _build_image_prompt(asset: dict) -> str:
    """构造设备技术手册图片理解提示词，限制模型只提取图片中能够确认的客观信息。"""
    document_name = str(asset.get("document_name") or "未命名文档")
    page_number = asset.get("page_number")
    page_text = f"第{page_number}页" if page_number else "页码未知"
    base_description = str(asset.get("base_description") or "")
    context_before = str(asset.get("context_before") or "")
    context_after = str(asset.get("context_after") or "")

    return load_prompt(
        "image_enrichment",
        document_name=document_name,
        page_text=page_text,
        base_description=base_description,
        context_before=context_before,
        context_after=context_after,
    ).strip()


def _extract_response_text(content) -> str:
    """兼容字符串和多段内容列表两类模型返回格式，并合并为一段简洁文本。"""
    if isinstance(content, str):
        return " ".join(content.split()).strip()
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                texts.append(str(item["text"]))
        return " ".join(" ".join(texts).split()).strip()
    return " ".join(str(content or "").split()).strip()


def _wait_for_rate_limit() -> None:
    """使用进程内共享滑动窗口限制所有图片Worker的视觉模型请求速度。"""
    max_requests = image_processing_config.caption_requests_per_minute
    window_seconds = 60.0

    while not _stop_event.is_set():
        wait_seconds = 0.0
        with _rate_limit_lock:
            now = time.monotonic()
            while _request_times and now - _request_times[0] >= window_seconds:
                _request_times.popleft()
            if len(_request_times) < max_requests:
                _request_times.append(now)
                return
            wait_seconds = max(window_seconds - (now - _request_times[0]), 0.05)

        logger.debug(f"视觉模型请求达到速率上限，等待{wait_seconds:.2f}秒后继续")
        _stop_event.wait(wait_seconds)

    raise RuntimeError("服务正在停止，取消新的图片视觉请求")


def _generate_visual_description(asset: dict, image_path: str) -> str:
    """调用视觉模型生成图片描述，并记录模型、耗时、结果长度和错误等级。"""
    image_base64 = _read_image_base64(image_path)
    mime_type = str(asset.get("content_type") or mimetypes.guess_type(image_path)[0] or "image/jpeg")
    prompt = _build_image_prompt(asset)
    _wait_for_rate_limit()

    model_name = os.getenv("VL_MODEL") or "未配置视觉模型"
    llm = get_llm_client(
        model=os.getenv("VL_MODEL"),
        timeout_seconds=image_processing_config.caption_timeout_seconds,
        max_retries=0,
    )
    started = time.perf_counter()
    with start_rag_observation(
        as_type="generation",
        name="image-enrichment-generation",
        input_data={
            "image": (summarize_image_assets([asset]) or [{}])[0],
            "prompt_length": len(prompt),
        },
        metadata={
            "pipeline": "image_enrichment",
            "timeout_seconds": image_processing_config.caption_timeout_seconds,
            "max_retries": 0,
            "rate_limit_per_minute": image_processing_config.caption_requests_per_minute,
        },
        model=model_name,
    ) as generation_observation:
        try:
            response = llm.invoke(
                [
                    HumanMessage(
                        content=[
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
                            },
                        ]
                    )
                ]
            )
            description = _extract_response_text(getattr(response, "content", ""))
            if not description:
                raise ValueError("视觉模型返回了空图片描述")
            description = description[:500]
            if generation_observation is not None:
                generation_observation.update(
                    output={
                        "status": "completed",
                        "description_length": len(description),
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    }
                )
            return description
        except Exception as exc:
            safe_error = str(exc)[:500]
            if generation_observation is not None:
                generation_observation.update(
                    output={
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                    level="ERROR",
                    status_message=safe_error,
                )
            raise


def _process_single_asset(asset: dict, worker_id: str) -> bool:
    """下载、分析并写回一张已领取图片，结束后始终清理临时文件。"""
    image_id = str(asset.get("image_id") or "")
    object_uri = str(asset.get("object_uri") or "")
    if not image_id:
        raise ValueError("图片资产缺少image_id")
    if not object_uri.startswith("minio://"):
        raise ValueError(f"图片资产缺少有效的MinIO对象地址：{object_uri}")

    declared_size = int(asset.get("file_size") or 0)
    if declared_size > image_processing_config.max_image_bytes:
        raise ValueError(
            f"图片元数据大小{declared_size}字节，超过视觉增强上限{image_processing_config.max_image_bytes}字节"
        )

    temp_path: Optional[str] = None
    try:
        temp_path = download_minio_object(object_uri, suffix=Path(str(asset.get("filename") or "")).suffix)
        description = _generate_visual_description(asset, temp_path)
        updated = get_image_asset_tool().complete_visual_result(image_id, worker_id, description)
        if not updated:
            logger.warning(f"图片视觉结果未写入，任务租约可能已经过期：image_id={image_id}，worker_id={worker_id}")
            return False

        logger.info(
            f"图片视觉增强完成：文档={asset.get('document_name')}，页码={asset.get('page_number')}，"
            f"文件={asset.get('filename')}，image_id={image_id}"
        )
        return True
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
                logger.debug(f"图片视觉增强临时文件已清理：{temp_path}")
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(f"图片视觉增强临时文件清理失败：{temp_path}，原因：{exc}")


def _safe_document_progress(tool, document_id: str) -> dict:
    """读取文档级图片增强进度；统计失败不能反向影响图片任务结果。"""
    if not document_id:
        return {}
    try:
        return tool.get_document_progress(document_id)
    except Exception as exc:
        logger.warning(f"读取图片增强文档进度失败：document_id={document_id}，原因={exc}")
        return {}


def process_image_enrichment_once(worker_id: str | None = None) -> int:
    """
    领取并处理一张待增强图片。

    无任务轮询不创建Trace；领取任务后创建独立根Trace，并按document_id归入同一Langfuse会话。
    根Trace输出同时包含单张任务结果和当前文档整体进度快照。
    """
    safe_worker_id = worker_id or f"图片增强任务-{uuid.uuid4().hex[:8]}"
    tool = get_image_asset_tool()
    assets = tool.claim_pending_assets(safe_worker_id, limit=1)
    if not assets:
        return 0

    asset = assets[0]
    image_id = str(asset.get("image_id") or "")
    document_id = str(asset.get("document_id") or "")
    tenant_id = str(asset.get("tenant_id") or "local")
    retry_count = int(asset.get("retry_count") or 0)
    started = time.perf_counter()

    with trace_image_enrichment(
        worker_id=safe_worker_id,
        tenant_id=tenant_id,
        image_id=image_id,
        document_id=document_id,
        document_name=str(asset.get("document_name") or ""),
        page_number=asset.get("page_number"),
        retry_count=retry_count,
    ) as root_observation:
        with start_rag_observation(
            as_type="span",
            name="image-enrichment-task",
            input_data={"image": (summarize_image_assets([asset]) or [{}])[0]},
            metadata={
                "pipeline": "image_enrichment",
                "retry_count": retry_count,
                "lease_seconds": image_processing_config.enrichment_lease_seconds,
            },
        ) as task_observation:
            try:
                updated = _process_single_asset(asset, safe_worker_id)
                status = "completed" if updated else "lease_lost"
                progress = _safe_document_progress(tool, document_id)
                task_output = {
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "document_progress": progress,
                }
                if task_observation is not None:
                    task_observation.update(
                        output=task_output,
                        level="DEFAULT" if updated else "WARNING",
                        status_message=None if updated else "任务租约可能已过期，视觉结果未写入",
                    )
                if root_observation is not None:
                    root_observation.update(
                        output=task_output,
                        level="DEFAULT" if updated else "WARNING",
                        status_message=None if updated else "任务租约可能已过期，视觉结果未写入",
                    )
            except Exception as exc:
                safe_error = str(exc)[:500]
                next_status = tool.fail_visual_result(image_id, safe_worker_id, safe_error)
                progress = _safe_document_progress(tool, document_id)
                failed_output = {
                    "status": "failed",
                    "next_status": next_status,
                    "error_type": type(exc).__name__,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "document_progress": progress,
                }
                if task_observation is not None:
                    task_observation.update(
                        output=failed_output,
                        level="ERROR",
                        status_message=safe_error,
                    )
                if root_observation is not None:
                    root_observation.update(
                        output=failed_output,
                        level="ERROR",
                        status_message=safe_error,
                    )
                logger.error(
                    f"图片视觉增强失败：文档={asset.get('document_name')}，文件={asset.get('filename')}，"
                    f"image_id={image_id}，后续状态={next_status}，原因={exc}",
                    exc_info=True,
                )
    return 1


def _worker_loop(worker_index: int) -> None:
    """运行单个后台Worker循环，无任务时按配置休眠，收到停止信号后安全退出。"""
    worker_id = f"图片增强任务-{worker_index}-{uuid.uuid4().hex[:8]}"
    logger.info(f"图片视觉增强后台任务启动：{worker_id}")

    while not _stop_event.is_set():
        try:
            processed = process_image_enrichment_once(worker_id)
            if processed == 0:
                _stop_event.wait(image_processing_config.enrichment_poll_seconds)
        except Exception as exc:
            logger.error(f"图片视觉增强后台任务循环异常：worker_id={worker_id}，原因={exc}", exc_info=True)
            _stop_event.wait(image_processing_config.enrichment_poll_seconds)

    logger.info(f"图片视觉增强后台任务停止：{worker_id}")


def start_image_enrichment_worker() -> None:
    """按配置幂等启动图片视觉增强后台线程。"""
    if not image_processing_config.enrichment_async:
        logger.info("图片异步增强已关闭，不启动后台任务")
        return
    if image_processing_config.process_mode == "off":
        logger.info("图片处理模式为off，不启动视觉增强后台任务")
        return

    with _worker_lock:
        global _worker_threads
        _worker_threads = [thread for thread in _worker_threads if thread.is_alive()]
        missing_count = max(image_processing_config.enrichment_workers - len(_worker_threads), 0)
        if missing_count == 0:
            return

        _stop_event.clear()
        start_index = len(_worker_threads) + 1
        for offset in range(missing_count):
            worker_index = start_index + offset
            thread = threading.Thread(
                target=_worker_loop,
                args=(worker_index,),
                name=f"图片视觉增强后台任务-{worker_index}",
                daemon=True,
            )
            thread.start()
            _worker_threads.append(thread)

        logger.info(
            f"图片视觉增强后台任务启动完成，本进程线程数={len(_worker_threads)}，"
            f"每分钟请求上限={image_processing_config.caption_requests_per_minute}"
        )


def stop_image_enrichment_worker(join_timeout_seconds: float = 10.0) -> None:
    """通知所有图片增强线程停止，并在限定时间内等待退出。"""
    _stop_event.set()
    with _worker_lock:
        global _worker_threads
        threads = list(_worker_threads)

    for thread in threads:
        thread.join(timeout=max(join_timeout_seconds, 0.0))

    with _worker_lock:
        _worker_threads = [thread for thread in _worker_threads if thread.is_alive()]
        alive_names = [thread.name for thread in _worker_threads]

    if alive_names:
        logger.warning(f"部分图片增强线程尚未退出，将由任务租约恢复：{alive_names}")
    else:
        logger.info("图片视觉增强后台任务已全部停止")
