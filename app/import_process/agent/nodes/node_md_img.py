from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from minio import Minio

from app.clients.image_asset_mongo_utils import get_image_asset_tool
from app.clients.minio_utils import get_minio_client, minio_object_uri
from app.conf.image_processing_config import image_processing_config
from app.conf.minio_config import minio_config
from app.core.logger import logger
from app.import_process.agent.state import ImportGraphState
from app.security.tenancy import tenant_object_prefix
from app.utils.task_utils import add_running_task


# MinerU 常见图片格式。SVG 暂不纳入处理，避免视觉模型和前端渲染兼容性差异。
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)]+)\)")


@dataclass(frozen=True)
class MarkdownImageReference:
    """Markdown 中一处图片引用及其附近文本。"""

    filename: str
    raw_target: str
    alt_text: str
    context_before: str
    context_after: str


def _normalize_text(value: Any, max_chars: int = 240) -> str:
    """清理 Markdown 标记和连续空白，并限制长度，避免图片资产记录保存过多无关正文。"""
    text = str(value or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"[`#>*_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _safe_path_segment(value: str) -> str:
    """生成适合 MinIO 对象路径的单级目录名，保留中文并移除路径分隔符和控制字符。"""
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", str(value or "").strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._")
    return cleaned or "未命名文档"


def _extract_target_path(raw_target: str) -> str:
    """
    从 Markdown 图片目标中提取实际路径。

    MinerU 通常输出 images/xxx.png；同时兼容 <images/a b.png> 和带标题的写法。
    """
    target = (raw_target or "").strip()
    if target.startswith("<") and ">" in target:
        return target[1:target.index(">")].strip()
    quoted_title = re.match(r"^(.*?)(?:\s+[\"'].*[\"'])$", target)
    return (quoted_title.group(1) if quoted_title else target).strip()


def _read_md_content(state: ImportGraphState) -> Tuple[str, Path, Path]:
    """读取 Markdown 正文，并返回 Markdown 文件和同级 images 目录。"""
    md_path = Path(str(state.get("md_path") or ""))
    if not str(md_path) or not md_path.exists():
        raise FileNotFoundError(f"Markdown文件不存在：{state.get('md_path')}")

    md_content = str(state.get("md_content") or "")
    if not md_content:
        md_content = md_path.read_text(encoding="utf-8")
        logger.info(f"已从文件读取Markdown正文，字符数：{len(md_content)}")

    return md_content, md_path, md_path.parent / "images"


def _scan_markdown_references(md_content: str) -> Dict[str, MarkdownImageReference]:
    """
    扫描 Markdown 图片引用，并为每张图片保存第一处有效上下文。

    同一图片可能在目录、正文或附录中重复引用；图片资产只保存一份，Markdown 中的全部引用仍会统一替换。
    """
    references: Dict[str, MarkdownImageReference] = {}
    context_chars = image_processing_config.context_chars

    for match in MARKDOWN_IMAGE_PATTERN.finditer(md_content):
        target_path = _extract_target_path(match.group("target"))
        filename = Path(target_path.replace("\\", "/")).name
        if not filename or Path(filename).suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        references.setdefault(
            filename,
            MarkdownImageReference(
                filename=filename,
                raw_target=target_path,
                alt_text=_normalize_text(match.group("alt"), 160),
                context_before=_normalize_text(md_content[max(0, match.start() - context_chars):match.start()], context_chars),
                context_after=_normalize_text(md_content[match.end():match.end() + context_chars], context_chars),
            ),
        )

    logger.info(f"Markdown图片引用扫描完成，共发现{len(references)}张唯一图片")
    return references


def _walk_json(value: Any) -> Iterable[Dict[str, Any]]:
    """递归遍历 MinerU JSON，返回其中所有字典节点，兼容不同版本的外层结构。"""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _first_value(data: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    """按候选字段顺序读取首个非空值，用于兼容 MinerU 不同版本字段名。"""
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _load_structured_metadata(state: ImportGraphState) -> Dict[str, Dict[str, Any]]:
    """
    从 MinerU content_list 或 middle.json 提取图片页码、坐标和图注。

    结构化文件缺失或字段变化时自动降级到 Markdown 上下文，不影响文档主流程。
    """
    candidate_paths = [
        state.get("mineru_content_list_path"),
        state.get("mineru_content_list_v2_path"),
        state.get("mineru_middle_json_path"),
    ]
    result: Dict[str, Dict[str, Any]] = {}

    for raw_path in candidate_paths:
        path = Path(str(raw_path or ""))
        if not str(path) or not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            for item in _walk_json(payload):
                image_path = _first_value(item, ("img_path", "image_path", "image_url", "image"))
                if not isinstance(image_path, str):
                    continue
                filename = Path(image_path.replace("\\", "/")).name
                if Path(filename).suffix.lower() not in IMAGE_EXTENSIONS:
                    continue

                page_value = _first_value(item, ("page_number", "page_no", "page", "page_idx", "page_index"))
                page_number = None
                try:
                    page_number = int(page_value)
                    if "page_idx" in item or "page_index" in item:
                        page_number += 1
                except (TypeError, ValueError):
                    pass

                result.setdefault(
                    filename,
                    {
                        "page_number": page_number,
                        "bbox": _first_value(item, ("bbox", "box", "position")),
                        "structured_caption": _normalize_text(
                            _first_value(item, ("img_caption", "image_caption", "caption", "title")),
                            240,
                        ),
                    },
                )

            if result:
                logger.info(f"已从MinerU结构化结果提取{len(result)}张图片的页码或图注信息")
                break
        except Exception as exc:
            logger.warning(f"读取MinerU结构化图片信息失败，已降级使用Markdown上下文：{path}，原因：{exc}")

    return result


def _calculate_file_hash(path: Path) -> str:
    """分块计算图片SHA-256，用于去重、追踪和后续视觉结果复用。"""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _build_base_description(reference: MarkdownImageReference, metadata: Dict[str, Any]) -> str:
    """
    优先使用结构化图注和 Markdown 原始描述；两者都缺失时使用图片附近正文。

    该描述会立即写入 Markdown 和图片资产，即使视觉增强尚未完成，文本检索仍能获得基本图片语义。
    """
    candidates = [
        metadata.get("structured_caption"),
        reference.alt_text,
        f"{reference.context_before} {reference.context_after}",
    ]
    for candidate in candidates:
        description = _normalize_text(candidate, 240)
        if description:
            return description
    return "文档相关图片"


def _upload_image(minio_client: Minio, image_path: Path, object_name: str) -> str:
    """上传单张图片并返回稳定的 minio:// 对象引用；失败时抛出异常交由单图降级处理。"""
    content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    minio_client.fput_object(
        bucket_name=minio_config.bucket_name,
        object_name=object_name,
        file_path=str(image_path),
        content_type=content_type,
    )
    return minio_object_uri(minio_config.bucket_name, object_name)


def _decide_visual_status(base_description: str, file_size: int, pending_count: int) -> Tuple[str, str]:
    """根据模式、图片大小、已有语义和单文档额度判断是否进入异步视觉增强队列。"""
    config = image_processing_config
    if config.process_mode == "off":
        return "skipped", "已关闭视觉增强"
    if file_size < config.min_image_bytes:
        return "skipped", "图片文件过小，按图标或装饰图片处理"
    if config.process_mode == "smart" and len(base_description) >= config.strong_caption_min_chars:
        return "skipped", "现有图注或上下文已能说明图片内容"
    if pending_count >= config.caption_max_per_document:
        return "skipped", "已达到单文档视觉增强数量上限"
    return "pending", "等待后台视觉增强"


def _replace_markdown_images(md_content: str, replacements: Dict[str, Tuple[str, str]]) -> str:
    """把本地图片路径替换成 MinIO 对象引用，同时保留可检索的图片描述。"""
    def replace(match: re.Match[str]) -> str:
        target_path = _extract_target_path(match.group("target"))
        filename = Path(target_path.replace("\\", "/")).name
        replacement = replacements.get(filename)
        if not replacement:
            return match.group(0)
        description, object_uri = replacement
        return f"![{description}]({object_uri})"

    return MARKDOWN_IMAGE_PATTERN.sub(replace, md_content)


def _save_processed_markdown(md_path: Path, content: str) -> Path:
    """把替换图片地址后的正文保存为新文件，保留 MinerU 原始 Markdown 便于问题排查。"""
    new_path = md_path.with_name(f"{md_path.stem}_new.md")
    new_path.write_text(content, encoding="utf-8")
    return new_path


def node_md_img(state: ImportGraphState) -> ImportGraphState:
    """
    将 MinerU 提取的图片资产化，但不在导入主链路同步调用视觉模型。

    处理步骤：
    1. 扫描 Markdown 图片引用，并读取 MinerU 页码、坐标和图注；
    2. 按租户、文档和任务编号上传到独立 MinIO 目录，避免同名文档相互覆盖；
    3. 将图片元数据和异步处理状态写入 MongoDB；
    4. 把 Markdown 本地路径替换为稳定对象引用后立即进入切片和向量化。

    单张图片上传或资产保存失败只记录日志，不阻塞正文知识库入库。
    """
    add_running_task(state["task_id"], sys._getframe().f_code.co_name)

    md_content, md_path, images_dir = _read_md_content(state)
    state["md_content"] = md_content
    if not images_dir.exists() or not images_dir.is_dir():
        logger.info(f"MinerU结果中不存在图片目录，跳过图片资产处理：{images_dir}")
        state["image_assets"] = []
        state["image_enrichment_summary"] = {"total": 0, "pending": 0, "skipped": 0, "failed": 0}
        return state

    references = _scan_markdown_references(md_content)
    if not references:
        logger.info("Markdown中没有本地图片引用，跳过图片资产处理")
        state["image_assets"] = []
        state["image_enrichment_summary"] = {"total": 0, "pending": 0, "skipped": 0, "failed": 0}
        return state

    minio_client = get_minio_client()
    if minio_client is None:
        logger.warning("MinIO客户端初始化失败，保留原Markdown图片路径并继续正文入库")
        return state

    tenant_id = str(state.get("tenant_id") or "local")
    revision_id = str(state.get("revision_id") or state.get("task_id") or md_path.stem)
    document_id = str(state.get("document_id") or revision_id)
    version_label = str(state.get("version_label") or "legacy-v1")
    document_name = _safe_path_segment(str(state.get("file_title") or md_path.stem))
    structured_metadata = _load_structured_metadata(state)
    replacements: Dict[str, Tuple[str, str]] = {}
    assets: List[Dict[str, Any]] = []
    pending_count = 0
    failed_count = 0

    for filename, reference in references.items():
        image_path = images_dir / filename
        if not image_path.exists() or not image_path.is_file():
            logger.warning(f"Markdown引用的图片文件不存在，已跳过：{image_path}")
            failed_count += 1
            continue

        try:
            content_hash = _calculate_file_hash(image_path)
            metadata = structured_metadata.get(filename, {})
            base_description = _build_base_description(reference, metadata)
            object_name = tenant_object_prefix(
                tenant_id,
                minio_config.minio_img_dir,
                document_name,
                document_id,
                revision_id,
                filename,
            )
            object_uri = _upload_image(minio_client, image_path, object_name)
            visual_status, status_reason = _decide_visual_status(base_description, image_path.stat().st_size, pending_count)
            if visual_status == "pending":
                pending_count += 1

            image_id = hashlib.sha256(
                f"{tenant_id}|{document_id}|{revision_id}|{filename}|{content_hash}".encode("utf-8")
            ).hexdigest()
            asset = {
                "image_id": image_id,
                "tenant_id": tenant_id,
                "document_id": document_id,
                "revision_id": revision_id,
                "version_label": version_label,
                "trust_level": str(state.get("trust_level") or "manufacturer_manual"),
                "device_model": str(state.get("device_model") or ""),
                "equipment_version": str(state.get("equipment_version") or ""),
                "software_version": str(state.get("software_version") or ""),
                "firmware_version": str(state.get("firmware_version") or ""),
                "hardware_revision": str(state.get("hardware_revision") or ""),
                "site_id": str(state.get("site_id") or ""),
                "asset_ids": [str(value) for value in (state.get("asset_ids") or []) if str(value).strip()],
                "document_name": document_name,
                "filename": filename,
                "content_hash": content_hash,
                "content_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                "file_size": image_path.stat().st_size,
                "object_uri": object_uri,
                "page_number": metadata.get("page_number"),
                "bbox": metadata.get("bbox"),
                "alt_text": reference.alt_text,
                "structured_caption": metadata.get("structured_caption") or "",
                "context_before": reference.context_before,
                "context_after": reference.context_after,
                "base_description": base_description,
                "visual_description": base_description if visual_status == "skipped" else "",
                "visual_status": visual_status,
                "status_reason": status_reason,
                "retry_count": 0,
            }
            assets.append(asset)
            replacements[filename] = (base_description, object_uri)
        except Exception as exc:
            failed_count += 1
            logger.error(f"图片资产处理失败，已跳过当前图片并继续导入：{image_path}，原因：{exc}", exc_info=True)

    if assets:
        try:
            saved_count = get_image_asset_tool().save_assets(assets)
            logger.info(f"图片资产保存完成，本次生成{len(assets)}条，MongoDB新增{saved_count}条")
        except Exception as exc:
            logger.error(f"图片资产写入MongoDB失败，正文仍继续入库：{exc}", exc_info=True)

    new_md_content = _replace_markdown_images(md_content, replacements)
    new_md_path = _save_processed_markdown(md_path, new_md_content)
    state["md_content"] = new_md_content
    state["md_path"] = str(new_md_path)
    state["image_assets"] = assets
    state["image_enrichment_summary"] = {
        "total": len(assets),
        "pending": pending_count,
        "skipped": sum(1 for item in assets if item.get("visual_status") == "skipped"),
        "failed": failed_count,
    }

    logger.info(
        f"Markdown图片资产处理完成：总数={len(assets)}，待增强={pending_count}，"
        f"无需增强={state['image_enrichment_summary']['skipped']}，失败={failed_count}，新文件={new_md_path}"
    )
    return state
