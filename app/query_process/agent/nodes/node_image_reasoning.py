from __future__ import annotations

import base64
import mimetypes
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

from langchain.messages import HumanMessage

from app.clients.image_asset_mongo_utils import get_image_asset_tool
from app.clients.minio_utils import download_minio_object
from app.conf.image_processing_config import image_processing_config
from app.conf.lm_config import lm_config
from app.core.logger import logger
from app.lm.lm_utils import get_llm_client
from app.query_process.agent.state import QueryGraphState
from app.utils.task_utils import add_done_task, add_running_task


# 明确出现这些词时，问题通常依赖图片中的空间位置、界面文字、连接关系或图形信息。
EXPLICIT_VISUAL_PATTERN = re.compile(
    r"图中|图上|上图|下图|图片|截图|照片|界面|操作面板|控制面板|"
    r"接线图|连接图|拓扑图|流程图|示意图|结构图|原理图|图表|曲线|波形|"
    r"指示灯|图标|屏幕显示|画面显示"
)

# “按钮在哪里”“哪个接口”“端口位置”等问题没有直接说图片，但通常必须结合命中的设备图片判断。
SPATIAL_OBJECT_PATTERN = re.compile(
    r"(?:按钮|按键|开关|旋钮|接口|端口|插口|插槽|部件|零件|指示灯|传感器|接头)"
    r".{0,12}(?:在哪|哪里|位置|哪个|哪一个|怎么接|如何连接|朝向|方向)"
    r"|(?:在哪|哪里|位置|哪个|哪一个)"
    r".{0,12}(?:按钮|按键|开关|旋钮|接口|端口|插口|插槽|部件|零件|指示灯|传感器|接头)"
)

MINIO_MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\((minio://[^)]+)\)")


def is_visual_question(question: str) -> bool:
    """
    判断用户问题是否明显依赖图片信息。

    使用确定性规则而不是额外调用大模型做分类，避免普通问题先消耗一次模型请求。
    规则保持保守：只有明确涉及图片、界面、图形结构或具体部件空间位置时才触发视觉分析。
    """
    normalized_question = re.sub(r"\s+", "", str(question or "")).lower()
    if not normalized_question:
        return False
    return bool(
        EXPLICIT_VISUAL_PATTERN.search(normalized_question)
        or SPATIAL_OBJECT_PATTERN.search(normalized_question)
    )


def _normalize_string_list(value: Any) -> List[str]:
    """把Milvus动态字段统一转换为去空字符串列表。"""
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item).strip() for item in values if str(item or "").strip()]


def _collect_candidate_uris(reranked_docs: List[Dict[str, Any]]) -> List[str]:
    """
    按Reranker相关性顺序收集Chunk关联图片地址。

    新数据优先读取image_object_uris动态字段；旧数据没有该字段时，再从Markdown正文提取minio://引用。
    """
    result: List[str] = []
    seen = set()

    for document in reranked_docs or []:
        if not isinstance(document, dict) or str(document.get("source") or "local") != "local":
            continue

        object_uris = _normalize_string_list(document.get("image_object_uris"))
        if not object_uris:
            object_uris = MINIO_MARKDOWN_IMAGE_PATTERN.findall(str(document.get("text") or ""))

        for object_uri in object_uris:
            if not object_uri.startswith("minio://") or object_uri in seen:
                continue
            seen.add(object_uri)
            result.append(object_uri)

    return result


def _build_fallback_asset(object_uri: str) -> Dict[str, Any]:
    """旧数据在MongoDB中找不到图片资产时，使用Chunk中的MinIO地址构造最小候选对象。"""
    return {
        "image_id": "",
        "document_id": "",
        "document_name": "",
        "filename": Path(object_uri).name,
        "object_uri": object_uri,
        "content_type": mimetypes.guess_type(object_uri)[0] or "image/jpeg",
        "page_number": None,
        "base_description": "",
        "visual_description": "",
        "visual_status": "unknown",
    }


def _load_candidate_assets(tenant_id: str, object_uris: List[str]) -> List[Dict[str, Any]]:
    """
    从MongoDB批量读取图片资产，并按Chunk相关性顺序补齐旧数据候选。

    MongoDB查询失败时直接使用MinIO地址构造候选，保证图片问答不会因为图片资产集合异常而完全失效。
    """
    if not object_uris:
        return []

    try:
        stored_assets = get_image_asset_tool().get_assets_by_object_uris(tenant_id, object_uris)
    except Exception as exc:
        logger.error(f"查询图片资产失败，已降级使用Chunk中的MinIO地址：{exc}", exc_info=True)
        stored_assets = []

    asset_by_uri = {
        str(asset.get("object_uri") or ""): asset
        for asset in stored_assets
        if isinstance(asset, dict) and asset.get("object_uri")
    }
    return [asset_by_uri.get(object_uri) or _build_fallback_asset(object_uri) for object_uri in object_uris]


def _best_cached_description(asset: Dict[str, Any]) -> str:
    """按视觉描述、结构化图注、基础描述和原始替代文本的优先级读取已缓存图片语义。"""
    for field in ("visual_description", "structured_caption", "base_description", "alt_text"):
        description = " ".join(str(asset.get(field) or "").split()).strip()
        if description:
            return description[:500]
    return "暂无已缓存的图片说明"


def _format_asset_location(asset: Dict[str, Any], index: int) -> str:
    """生成稳定的图片编号、文档名和页码描述，供视觉模型和最终回答引用。"""
    document_name = str(asset.get("document_name") or asset.get("filename") or "未知文档").strip()
    page_number = asset.get("page_number")
    page_text = f"第{page_number}页" if isinstance(page_number, int) and page_number > 0 else "页码未知"
    return f"图片{index}，文档《{document_name}》，{page_text}"


def _build_cached_context(assets: List[Dict[str, Any]]) -> str:
    """把后台异步生成的通用图片说明整理为可直接加入回答Prompt的降级上下文。"""
    if not assets:
        return ""

    lines = ["【相关图片的已缓存说明】"]
    for index, asset in enumerate(assets, start=1):
        lines.append(f"- {_format_asset_location(asset, index)}：{_best_cached_description(asset)}")
    lines.append("以上内容是通用图片说明，不足以确认的空间位置、按钮文字或连接关系不得推测。")
    return "\n".join(lines)


def _read_image_data_url(asset: Dict[str, Any]) -> Tuple[str, str]:
    """
    从MinIO下载图片、校验体积并转换为视觉模型需要的Data URL。

    返回Data URL和临时文件路径，临时文件由调用方在finally中统一删除。
    """
    object_uri = str(asset.get("object_uri") or "").strip()
    if not object_uri.startswith("minio://"):
        raise ValueError(f"图片缺少有效MinIO地址：{object_uri}")

    declared_size = int(asset.get("file_size") or 0)
    if declared_size > image_processing_config.max_image_bytes:
        raise ValueError(
            f"图片元数据大小{declared_size}字节，超过查询视觉上限"
            f"{image_processing_config.max_image_bytes}字节"
        )

    suffix = Path(str(asset.get("filename") or object_uri)).suffix
    temp_path = download_minio_object(object_uri, suffix=suffix)
    path = Path(temp_path)
    file_size = path.stat().st_size
    if file_size <= 0:
        raise ValueError("图片文件为空")
    if file_size > image_processing_config.max_image_bytes:
        raise ValueError(
            f"图片实际大小{file_size}字节，超过查询视觉上限"
            f"{image_processing_config.max_image_bytes}字节"
        )

    mime_type = str(asset.get("content_type") or mimetypes.guess_type(path.name)[0] or "image/jpeg")
    encoded = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}", temp_path


def _extract_response_text(content: Any) -> str:
    """兼容视觉模型返回字符串或多段内容列表，并规整为普通文本。"""
    if isinstance(content, str):
        return "\n".join(line.strip() for line in content.splitlines() if line.strip())
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                texts.append(str(item["text"]))
        return "\n".join(text.strip() for text in texts if text.strip())
    return str(content or "").strip()


def _invoke_query_vision(question: str, assets: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """
    使用一次多图请求回答当前视觉问题。

    单次请求最多包含QUERY_IMAGE_TOP_K张图片，既保留跨图对比能力，也避免逐张调用造成高延迟和额外费用。
    下载失败的单张图片会被跳过；只要仍有一张可用图片，视觉分析就继续执行。
    """
    content: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "你正在根据设备技术手册图片回答一个具体问题。\n"
                f"用户问题：{question}\n\n"
                "请只依据随后提供的图片和每张图片的已缓存说明作答。重点确认界面文字、按钮或接口位置、"
                "接线关系、指示灯状态、图形走势和部件方向。无法从图片确认的内容必须明确说明无法确认，"
                "不要根据常识补造。回答控制在500字以内，并使用“图片1”“第X页”等标识说明依据。"
            ),
        }
    ]
    temp_paths: List[str] = []
    available_assets: List[Dict[str, Any]] = []

    try:
        for index, asset in enumerate(assets, start=1):
            try:
                data_url, temp_path = _read_image_data_url(asset)
                temp_paths.append(temp_path)
                available_assets.append(asset)
                content.append(
                    {
                        "type": "text",
                        "text": (
                            f"{_format_asset_location(asset, index)}。"
                            f"已缓存说明：{_best_cached_description(asset)}"
                        ),
                    }
                )
                content.append({"type": "image_url", "image_url": {"url": data_url}})
            except Exception as exc:
                logger.warning(
                    f"查询视觉分析跳过一张不可用图片：uri={asset.get('object_uri')}，原因={exc}"
                )

        if not available_assets:
            raise ValueError("候选图片均无法读取")

        vision_client = get_llm_client(
            model=lm_config.lv_model,
            timeout_seconds=image_processing_config.query_vision_timeout_seconds,
            max_retries=0,
        )
        response = vision_client.invoke([HumanMessage(content=content)])
        analysis = _extract_response_text(getattr(response, "content", ""))
        if not analysis:
            raise ValueError("视觉模型返回空分析结果")
        return analysis[:2000], available_assets
    finally:
        for temp_path in temp_paths:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(f"查询视觉临时文件清理失败：{temp_path}，原因={exc}")


def node_image_reasoning(state: QueryGraphState) -> Dict[str, Any]:
    """
    查询阶段按需执行图片视觉分析。

    处理原则：
    1. 普通文字问题直接跳过，视觉模型调用次数为零；
    2. 视觉问题只选择Reranker最相关Chunk中的前N张图片；
    3. 先加载后台缓存描述作为降级上下文，再执行一次查询相关的多图分析；
    4. MongoDB、MinIO或视觉模型异常只影响图片补充信息，不中断文本RAG回答。
    """
    logger.info("---node_image_reasoning开始处理---")
    node_name = sys._getframe().f_code.co_name
    session_id = str(state.get("session_id") or "")
    if session_id:
        add_running_task(session_id, node_name, state.get("is_stream"))

    try:
        question = str(state.get("rewritten_query") or state.get("original_query") or "").strip()
        need_visual_reasoning = is_visual_question(question)
        if not need_visual_reasoning:
            logger.info("当前问题未命中视觉意图规则，跳过查询阶段视觉模型调用")
            return {
                "need_visual_reasoning": False,
                "image_reasoning_status": "not_required",
                "image_assets": [],
                "image_analysis_context": "",
                "image_reasoning_object_uris": [],
            }

        candidate_uris = _collect_candidate_uris(state.get("reranked_docs") or [])
        selected_uris = candidate_uris[: image_processing_config.query_image_top_k]
        if not selected_uris:
            logger.info("当前问题需要图片信息，但重排后的Chunk没有关联图片")
            return {
                "need_visual_reasoning": True,
                "image_reasoning_status": "no_candidate_images",
                "image_assets": [],
                "image_analysis_context": "",
                "image_reasoning_object_uris": [],
            }

        tenant_id = str(state.get("tenant_id") or "local")
        selected_assets = _load_candidate_assets(tenant_id, selected_uris)
        cached_context = _build_cached_context(selected_assets)

        if not image_processing_config.query_vision_enabled:
            logger.info("查询阶段视觉模型已关闭，使用后台缓存图片说明继续回答")
            return {
                "need_visual_reasoning": True,
                "image_reasoning_status": "vision_disabled",
                "image_assets": selected_assets,
                "image_analysis_context": cached_context,
                "image_reasoning_object_uris": selected_uris,
            }

        try:
            visual_analysis, available_assets = _invoke_query_vision(question, selected_assets)
            analysis_context = (
                f"{cached_context}\n\n【针对当前问题的图片视觉分析】\n{visual_analysis}"
                if cached_context
                else f"【针对当前问题的图片视觉分析】\n{visual_analysis}"
            )
            logger.info(
                f"查询阶段视觉分析完成：候选图片={len(selected_assets)}，"
                f"实际分析={len(available_assets)}"
            )
            return {
                "need_visual_reasoning": True,
                "image_reasoning_status": "completed",
                "image_assets": available_assets,
                "image_analysis_context": analysis_context,
                "image_reasoning_object_uris": [
                    str(asset.get("object_uri") or "")
                    for asset in available_assets
                    if asset.get("object_uri")
                ],
            }
        except Exception as exc:
            logger.error(f"查询阶段视觉分析失败，已降级使用缓存图片说明：{exc}", exc_info=True)
            return {
                "need_visual_reasoning": True,
                "image_reasoning_status": "fallback_cached_description",
                "image_assets": selected_assets,
                "image_analysis_context": cached_context,
                "image_reasoning_object_uris": selected_uris,
                "image_reasoning_error": str(exc)[:500],
            }
    finally:
        if session_id:
            add_done_task(session_id, node_name, state.get("is_stream"))
        logger.info("---node_image_reasoning处理结束---")


if __name__ == "__main__":
    assert is_visual_question("图中这个接口是什么？")
    assert is_visual_question("启动按钮在哪里？")
    assert not is_visual_question("设备开机流程是什么？")
    logger.info("图片视觉意图规则本地测试通过")
