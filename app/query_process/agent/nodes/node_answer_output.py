import re
import sys
from typing import Any, Dict, List

from app.clients.minio_utils import resolve_object_urls
from app.clients.mongo_history_utils import save_chat_message
from app.core.load_prompt import load_prompt
from app.core.logger import logger
from app.lm.lm_utils import get_llm_client
from app.query_process.agent.state import QueryGraphState
from app.utils.sse_utils import SSEEvent, push_to_session
from app.utils.task_utils import add_done_task, add_running_task, set_task_result


MAX_CONTEXT_CHARS = 12000
MAX_HISTORY_CHARS = 3000
MAX_IMAGE_CONTEXT_CHARS = 3000


def _normalize_model_text(content: Any) -> str:
    """兼容模型返回字符串或多段内容列表，并规整为最终答案文本。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for item in content:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                texts.append(str(item["text"]))
        return "".join(texts).strip()
    return str(content or "").strip()


def step_1_check_answer(state: QueryGraphState) -> bool:
    """
    检查上游节点是否已经生成答案。

    设备名称不明确、没有对应知识库或需要用户二次选择时，node_item_name_confirm会直接写入answer，
    此时不再执行检索和模型生成，只负责把已有答案返回前端并保存历史。
    """
    answer = str(state.get("answer") or "").strip()
    if not answer:
        return False

    session_id = str(state.get("session_id") or "")
    if state.get("is_stream"):
        push_to_session(session_id, SSEEvent.DELTA, {"delta": answer})
    else:
        set_task_result(session_id, "answer", answer)
    return True


def _format_reranked_context(reranked_docs: List[Dict[str, Any]]) -> str:
    """把重排结果转换为带来源标识的参考资料，并限制总体字符数。"""
    blocks: List[str] = []
    used_chars = 0

    for index, document in enumerate(reranked_docs or [], start=1):
        if not isinstance(document, dict):
            continue

        text = str(document.get("text") or "").strip()
        if not text:
            continue

        metadata = [f"[{index}]"]
        source = str(document.get("source") or "").strip()
        chunk_id = document.get("chunk_id")
        document_id = str(document.get("document_id") or "").strip()
        title = str(document.get("title") or document.get("file_title") or "").strip()
        url = str(document.get("url") or "").strip()
        score = document.get("score")
        page_numbers = document.get("image_page_numbers") or []

        if source:
            metadata.append(f"[source={source}]")
        if chunk_id:
            metadata.append(f"[chunk_id={chunk_id}]")
        if document_id:
            metadata.append(f"[document_id={document_id}]")
        if title:
            metadata.append(f"[title={title}]")
        if url:
            metadata.append(f"[url={url}]")
        if score is not None:
            try:
                metadata.append(f"[score={float(score):.4f}]")
            except (TypeError, ValueError):
                pass
        if page_numbers:
            metadata.append(f"[image_pages={page_numbers}]")

        block = " ".join(metadata) + "\n" + text
        if used_chars + len(block) > MAX_CONTEXT_CHARS:
            remaining = MAX_CONTEXT_CHARS - used_chars
            if remaining > 200:
                blocks.append(block[:remaining])
            break

        blocks.append(block)
        used_chars += len(block) + 2

    return "\n\n".join(blocks) if blocks else "无可用参考资料"


def _format_history(history: List[Dict[str, Any]]) -> str:
    """按时间顺序格式化历史对话，并单独限制历史字符数，防止挤占本轮检索资料。"""
    lines: List[str] = []
    used_chars = 0

    for message in history or []:
        if not isinstance(message, dict):
            continue

        role = str(message.get("role") or "")
        text = str(message.get("text") or "").strip()
        if not text:
            continue

        prefix = "用户" if role == "user" else "助手" if role == "assistant" else ""
        if not prefix:
            continue

        line = f"{prefix}: {text}"
        if used_chars + len(line) > MAX_HISTORY_CHARS:
            break
        lines.append(line)
        used_chars += len(line) + 1

    return "\n".join(lines) if lines else "无历史对话"


def _build_image_reasoning_section(state: QueryGraphState) -> str:
    """
    构造最终回答使用的图片分析补充信息和约束。

    图片分析是当前问题下对候选图片的定向解释，不替代设备手册正文。若正文与图片分析冲突，
    最终回答必须明确说明冲突并要求人工核验，而不是自行选择其中一个结论。
    """
    need_visual_reasoning = bool(state.get("need_visual_reasoning"))
    image_context = str(state.get("image_analysis_context") or "").strip()
    image_status = str(state.get("image_reasoning_status") or "not_required")

    if not need_visual_reasoning:
        return ""

    if image_context:
        image_context = image_context[:MAX_IMAGE_CONTEXT_CHARS]
        return (
            "【图片分析补充信息】\n"
            f"处理状态：{image_status}\n"
            f"{image_context}\n\n"
            "【多模态回答约束】\n"
            "1. 只使用与当前重排文档关联的图片分析，不得引用未选中的图片；\n"
            "2. 图片分析仅作为设备手册正文的补充证据，不得据此补造操作步骤、参数或安全要求；\n"
            "3. 正文和图片分析一致时可以综合回答；存在冲突时必须明确指出冲突并建议核对原始手册或由设备工程师确认；\n"
            "4. 涉及按钮、接口、接线、方向和空间位置时，应说明依据的图片编号或页码。"
        )

    return (
        "【图片分析补充信息】\n"
        f"处理状态：{image_status}\n"
        "当前问题依赖图片信息，但没有检索到可用的相关图片，或图片分析未能产生有效结果。\n\n"
        "【回答约束】\n"
        "只能依据文字资料回答；对于按钮、接口、接线、方向、部件位置或图片中的状态，必须明确说明当前无法从图片确认，严禁根据常识猜测。"
    )


def step_2_construct_prompt(state: QueryGraphState) -> str:
    """组合文本检索资料、历史对话和图片推理上下文，构造最终回答Prompt。"""
    question = str(state.get("rewritten_query") or state.get("original_query") or "").strip()
    item_names = [str(value).strip() for value in (state.get("item_names") or []) if str(value).strip()]
    context = _format_reranked_context(state.get("reranked_docs") or [])
    history = _format_history(state.get("history") or [])

    prompt = load_prompt(
        "answer_out",
        context=context,
        history=history,
        item_names=", ".join(item_names) if item_names else "无指定设备",
        question=question,
    )

    image_section = _build_image_reasoning_section(state)
    if image_section:
        prompt = f"{prompt}\n\n{image_section}"

    logger.debug(f"最终回答Prompt：{prompt}")
    logger.info(
        f"回答Prompt构建完成，字符数={len(prompt)}，参考文档数={len(state.get('reranked_docs') or [])}，"
        f"需要视觉={bool(state.get('need_visual_reasoning'))}，"
        f"视觉状态={state.get('image_reasoning_status') or 'not_required'}"
    )
    return prompt


def step_3_generate_response(state: QueryGraphState, prompt: str) -> QueryGraphState:
    """调用对话模型生成最终答案，支持SSE流式输出和普通阻塞输出。"""
    session_id = str(state.get("session_id") or "")
    is_stream = bool(state.get("is_stream"))
    llm = get_llm_client()

    if is_stream:
        final_text = ""
        try:
            for chunk in llm.stream(prompt):
                delta = _normalize_model_text(getattr(chunk, "content", ""))
                if not delta:
                    continue
                final_text += delta
                push_to_session(session_id, SSEEvent.DELTA, {"delta": delta})
            state["answer"] = final_text.strip()
            logger.info(f"流式答案生成完成，字符数={len(state.get('answer') or '')}")
        except Exception as exc:
            logger.error(f"流式答案生成失败：{exc}", exc_info=True)
            push_to_session(session_id, SSEEvent.ERROR, {"error": str(exc)})
            state["answer"] = final_text.strip() or "抱歉，生成回答时出现错误。"
        return state

    try:
        response = llm.invoke(prompt)
        answer = _normalize_model_text(getattr(response, "content", ""))
        if not answer:
            raise ValueError("大模型返回空答案")
        state["answer"] = answer
        set_task_result(session_id, "answer", answer)
        logger.info(f"非流式答案生成完成，字符数={len(answer)}")
    except Exception as exc:
        logger.error(f"非流式答案生成失败：{exc}", exc_info=True)
        state["answer"] = "抱歉，生成回答时出现错误。"
        set_task_result(session_id, "answer", state["answer"])
    return state


def _unique_strings(values: List[str]) -> List[str]:
    """按原始顺序去重字符串。"""
    result: List[str] = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _selected_image_object_refs(state: QueryGraphState) -> List[str]:
    """
    获取本轮图片推理实际选择的对象地址。

    不再扫描全部reranked_docs中的Markdown图片，避免前端展示与用户问题无关的图片。
    当节点只返回image_assets而没有显式地址列表时，从资产对象中安全回退提取。
    """
    selected_refs = _unique_strings(state.get("image_reasoning_object_uris") or [])
    if selected_refs:
        return selected_refs

    asset_refs = [
        str(asset.get("object_uri") or "")
        for asset in (state.get("image_assets") or [])
        if isinstance(asset, dict) and asset.get("object_uri")
    ]
    return _unique_strings(asset_refs)


def step_4_write_history(state: QueryGraphState, image_object_refs: List[str]) -> QueryGraphState:
    """把回答、设备名称、实际使用的图片对象地址和Trace ID写入MongoDB历史记录。"""
    answer = str(state.get("answer") or "").strip()
    if not answer:
        return state

    try:
        save_chat_message(
            session_id=str(state.get("session_id") or "default"),
            role="assistant",
            text=answer,
            rewritten_query="",
            # 正常回答保存已确认设备；澄清回答保存候选设备。
            # 这样下一轮用户只回复“是的”时，型号确认节点才能读取并确认单个候选。
            item_names=state.get("item_names") or state.get("pending_item_names") or [],
            image_urls=image_object_refs,
            message_id=None,
            trace_id=str(state.get("trace_id") or ""),
        )
    except Exception as exc:
        logger.error(f"写入MongoDB历史记录失败：{exc}", exc_info=True)
    return state


def node_answer_output(state: QueryGraphState) -> QueryGraphState:
    """
    输出最终答案并完成历史记录和前端图片返回。

    图片只使用node_image_reasoning本轮实际选择的对象地址，浏览器返回时再转换为短期签名URL；
    MongoDB历史仍保存稳定的minio://引用，避免签名URL过期后历史图片无法重新打开。
    """
    logger.info("---node_answer_output开始处理---")
    session_id = str(state.get("session_id") or "")
    node_name = sys._getframe().f_code.co_name
    add_running_task(session_id, node_name, state.get("is_stream"))

    try:
        answer_exists = step_1_check_answer(state)
        if not answer_exists:
            prompt = step_2_construct_prompt(state)
            state["prompt"] = prompt
            step_3_generate_response(state, prompt)

        image_object_refs = _selected_image_object_refs(state)
        image_urls = resolve_object_urls(image_object_refs)
        state["image_object_refs"] = image_object_refs
        state["image_urls"] = image_urls

        step_4_write_history(state, image_object_refs)

        logger.info(
            f"答案节点处理完成，答案字符数={len(state.get('answer') or '')}，"
            f"返回图片数={len(image_urls)}，视觉状态={state.get('image_reasoning_status') or 'not_required'}"
        )
        return state
    finally:
        add_done_task(session_id, node_name, state.get("is_stream"))
        logger.info("---node_answer_output处理结束---")


if __name__ == "__main__":
    mock_state: QueryGraphState = {
        "session_id": "test_answer_session_001",
        "tenant_id": "local",
        "original_query": "启动按钮在哪里？",
        "rewritten_query": "HAK 180烫金机操作面板的启动按钮在哪里？",
        "item_names": ["HAK 180烫金机"],
        "history": [],
        "reranked_docs": [
            {
                "chunk_id": "local_101",
                "document_id": "doc_001",
                "source": "local",
                "title": "HAK 180烫金机操作手册",
                "score": 0.95,
                "text": "启动按钮位于操作面板右下区域，具体位置参考第38页操作面板图片。",
                "image_page_numbers": [38],
            }
        ],
        "need_visual_reasoning": True,
        "image_reasoning_status": "completed",
        "image_analysis_context": "图片1显示绿色Start按钮位于操作面板右下区域。",
        "image_reasoning_object_uris": ["minio://equipment-rag/images/panel.png"],
        "is_stream": False,
    }
    logger.info(step_2_construct_prompt(mock_state))
