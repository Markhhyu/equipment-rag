from __future__ import annotations

import re
import sys
from typing import Any, Dict, List

from app.core.logger import logger
from app.import_process.agent.state import ImportGraphState
from app.utils.task_utils import add_running_task


# 只提取已经由node_md_img替换完成的MinIO图片引用，避免把普通网页链接误认为文档图片资产。
MINIO_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\((minio://[^)]+)\)")


def _unique_strings(values: List[str]) -> List[str]:
    """按原始出现顺序去重字符串，保证同一图片在Chunk中重复引用时只保存一次。"""
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def node_attach_image_metadata(state: ImportGraphState) -> ImportGraphState:
    """
    将文档编号和图片资产编号关联到每个文本Chunk。

    node_md_img已经把Markdown本地图片路径替换为稳定的minio://对象地址，并在state中保存image_assets。
    本节点通过Chunk正文中的对象地址查找对应图片资产，然后补充以下动态元数据：
    1. document_id：当前导入任务编号，用于查询阶段按文档回查图片；
    2. image_ids：当前Chunk实际引用的图片资产编号；
    3. image_object_uris：当前Chunk中的稳定MinIO对象引用；
    4. image_page_numbers：图片在原PDF中的页码，便于后续展示和问题排查。

    Milvus集合已开启动态字段，因此不需要修改固定Schema。没有图片的Chunk仍会保存空列表，查询阶段可以直接判断。
    """
    task_id = str(state.get("task_id") or "").strip()
    if task_id:
        add_running_task(task_id, sys._getframe().f_code.co_name)

    chunks = state.get("chunks") or []
    image_assets = state.get("image_assets") or []
    tenant_id = str(state.get("tenant_id") or "local")
    revision_id = str(state.get("revision_id") or task_id or "").strip()
    document_id = str(state.get("document_id") or revision_id or state.get("file_title") or "未命名文档")
    version_label = str(state.get("version_label") or "legacy-v1")
    trust_level = str(state.get("trust_level") or "manufacturer_manual")
    applicability = {
        "device_model": str(state.get("device_model") or ""),
        "software_version": str(state.get("software_version") or ""),
        "firmware_version": str(state.get("firmware_version") or ""),
        "hardware_revision": str(state.get("hardware_revision") or ""),
        "site_id": str(state.get("site_id") or ""),
        "asset_ids": [str(value) for value in (state.get("asset_ids") or []) if str(value).strip()],
    }

    asset_by_uri: Dict[str, Dict[str, Any]] = {}
    for asset in image_assets:
        if not isinstance(asset, dict):
            continue
        object_uri = str(asset.get("object_uri") or "").strip()
        if object_uri:
            asset_by_uri[object_uri] = asset

    linked_chunk_count = 0
    linked_image_count = 0
    missing_asset_uris = set()

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue

        chunk["tenant_id"] = tenant_id
        chunk["document_id"] = document_id
        chunk["revision_id"] = revision_id
        chunk["version_label"] = version_label
        chunk["trust_level"] = trust_level
        chunk.update(applicability)
        chunk["governance_managed"] = True

        content = str(chunk.get("content") or "")
        object_uris = _unique_strings(MINIO_IMAGE_PATTERN.findall(content))
        image_ids: List[str] = []
        page_numbers: List[int] = []

        for object_uri in object_uris:
            asset = asset_by_uri.get(object_uri)
            if not asset:
                missing_asset_uris.add(object_uri)
                continue

            image_id = str(asset.get("image_id") or "").strip()
            if image_id:
                image_ids.append(image_id)

            page_number = asset.get("page_number")
            if isinstance(page_number, int) and page_number > 0:
                page_numbers.append(page_number)

        chunk["image_ids"] = _unique_strings(image_ids)
        chunk["image_object_uris"] = object_uris
        chunk["image_page_numbers"] = list(dict.fromkeys(page_numbers))
        chunk["has_images"] = bool(object_uris)

        if object_uris:
            linked_chunk_count += 1
            linked_image_count += len(object_uris)

    state["chunks"] = chunks
    state["image_chunk_link_summary"] = {
        "document_id": document_id,
        "revision_id": revision_id,
        "version_label": version_label,
        "chunk_count": len(chunks),
        "linked_chunk_count": linked_chunk_count,
        "linked_image_reference_count": linked_image_count,
        "unmatched_image_reference_count": len(missing_asset_uris),
    }

    if missing_asset_uris:
        logger.warning(
            f"Chunk图片关联完成，但有{len(missing_asset_uris)}个MinIO引用未找到图片资产，"
            f"可能是旧Markdown数据或图片资产写入失败"
        )

    logger.info(
        f"Chunk图片关联完成：文档编号={document_id}，Chunk总数={len(chunks)}，"
        f"含图片Chunk={linked_chunk_count}，图片引用数={linked_image_count}"
    )
    return state
