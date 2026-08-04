from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from app.core.logger import logger


_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*]\([^)]*\)")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)]\([^)]*\)")
_TEXT_KEYS = ("text", "table_body")
_LIST_KEYS = ("list_items", "image_caption", "image_footnote", "table_caption", "table_footnote")
_CONTAINER_KEYS = ("blocks", "para_blocks", "children", "content", "body")


def _normalize_text(value: Any) -> str:
    """去掉Markdown标记、空白和标点，保留可用于跨格式匹配的正文字符。"""
    text = str(value or "")
    text = _MARKDOWN_IMAGE.sub("", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    return re.sub(r"[\W_]+", "", text.casefold(), flags=re.UNICODE)


def _extract_text_parts(value: Any) -> list[str]:
    """从MinerU标准版或V2节点中提取正文，不把bbox、页码等结构字段误当成文本。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_extract_text_parts(item))
        return result
    if not isinstance(value, dict):
        return []

    result = []
    for key in _TEXT_KEYS:
        if isinstance(value.get(key), str):
            result.append(value[key])
    for key in _LIST_KEYS:
        if isinstance(value.get(key), (str, list, dict)):
            result.extend(_extract_text_parts(value[key]))
    for key in _CONTAINER_KEYS:
        nested = value.get(key)
        if isinstance(nested, (list, dict)):
            result.extend(_extract_text_parts(nested))
    return result


def load_mineru_pages(content_list_path: str) -> dict[int, list[str]]:
    """读取MinerU内容列表，返回以1为起点的PDF物理页及其文本片段。"""
    path = Path(str(content_list_path or "").strip())
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"读取MinerU页码结构失败，跳过正文页码标注：{path}，原因={exc}")
        return {}

    pages: dict[int, list[str]] = defaultdict(list)
    if isinstance(payload, list) and payload and all(isinstance(page, list) for page in payload):
        # content_list_v2.json：外层数组下标就是PDF物理页。
        entries: Iterable[tuple[int, Any]] = (
            (page_index + 1, entry)
            for page_index, page in enumerate(payload)
            for entry in page
        )
    elif isinstance(payload, list):
        # content_list.json：扁平节点通过page_idx标记所在物理页。
        entries = (
            (int(entry.get("page_idx", 0)) + 1, entry)
            for entry in payload
            if isinstance(entry, dict)
        )
    else:
        logger.warning(f"MinerU页码结构不是数组，跳过正文页码标注：{path}")
        return {}

    for page_number, entry in entries:
        for part in _extract_text_parts(entry):
            normalized = _normalize_text(part)
            if len(normalized) >= 4:
                pages[page_number].append(normalized)
    return dict(pages)


def _page_match_score(chunk_text: str, page_parts: list[str]) -> int:
    """计算一个Chunk与单页正文的字符证据量；完整段落命中优先，短窗口用于OCR差异降级。"""
    if not chunk_text or not page_parts:
        return 0
    score = 0
    for part in page_parts:
        if part in chunk_text:
            score += len(part)
            continue
        if chunk_text in part and len(chunk_text) >= 8:
            score += len(chunk_text)
            continue
        if len(part) < 16:
            continue
        windows = {part[index : index + 16] for index in range(0, len(part) - 15, 12)}
        score += 8 * sum(1 for window in windows if window in chunk_text)
    return score


def infer_chunk_page_numbers(chunk_content: str, pages: dict[int, list[str]]) -> list[int]:
    """依据MinerU正文字符为单个Chunk推断PDF物理页，最多保留最相关的4页。"""
    chunk_text = _normalize_text(chunk_content)
    if len(chunk_text) < 4 or not pages:
        return []
    scores = {
        page_number: _page_match_score(chunk_text, parts)
        for page_number, parts in pages.items()
    }
    scores = {page: score for page, score in scores.items() if score > 0}
    if not scores:
        return []
    best_score = max(scores.values())
    threshold = max(8, int(best_score * 0.18))
    selected = [page for page, score in scores.items() if score >= threshold]
    if len(selected) > 4:
        selected = [page for page, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:4]]
    return sorted(selected)


def attach_pdf_page_numbers(chunks: list[dict[str, Any]], content_list_path: str) -> int:
    """给正文Chunk写入PDF物理页码，并返回成功标注的Chunk数量。Markdown资料保持无页码。"""
    pages = load_mineru_pages(content_list_path)
    if not pages:
        return 0
    attributed = 0
    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            continue
        page_numbers = infer_chunk_page_numbers(str(chunk.get("content") or ""), pages)
        chunk["page_numbers"] = page_numbers
        chunk["page_start"] = page_numbers[0] if page_numbers else None
        chunk["page_end"] = page_numbers[-1] if page_numbers else None
        if page_numbers:
            attributed += 1
    return attributed
