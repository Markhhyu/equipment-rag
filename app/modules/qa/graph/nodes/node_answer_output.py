import re
import sys
from typing import Any, Dict, List

from app.modules.qa.infrastructure.history import save_chat_message
from app.platform.storage.minio import resolve_object_urls
from app.platform.ai.prompts import load_prompt
from app.platform.observability.logging import logger
from app.platform.ai.chat import get_llm_client
from app.modules.knowledge.domain.trust import assess_answer_policy, trust_metadata
from app.modules.qa.graph.state import QueryGraphState
from app.platform.runtime.sse import SSEEvent, push_to_session
from app.platform.runtime.task_progress import add_done_task, add_running_task, set_task_result


MAX_CONTEXT_CHARS = 12000
MAX_HISTORY_CHARS = 3000
MAX_IMAGE_CONTEXT_CHARS = 3000
_IMAGE_BLOCK_PATTERN = re.compile(
    r"(?:\n|^)\s*【图片】\s*(?:\n\s*(?:<https?://[^>]+>|https?://\S+)\s*)+",
    flags=re.IGNORECASE,
)
_PLACEHOLDER_URL_PATTERN = re.compile(r"<?https?://(?:www\.)?example\.com/[^\s>]*>?", flags=re.IGNORECASE)


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


def _sanitize_generated_answer(answer: str) -> str:
    """移除模型越权生成的图片区块和占位链接；真实图片只允许走结构化image_urls。"""
    text = _IMAGE_BLOCK_PATTERN.sub("\n", str(answer or ""))
    text = _PLACEHOLDER_URL_PATTERN.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _version_scope_clarification(options: List[Dict[str, Any]]) -> str:
    """同型号存在多个并行配置时要求用户确认，避免把不同软件/固件版本的步骤混在一起。"""
    lines = ["当前知识库中存在多个同时生效的设备配置版本，我不能在未确认版本时混合回答。请提供以下任一信息："]
    seen = set()
    for item in options or []:
        for label in item.get("options") or []:
            text = str(label or "").strip()
            if text and text not in seen:
                seen.add(text)
                lines.append(f"- {text}")
    lines.append("你也可以直接提供设备编号；后续接入 OA/设备台账后，系统可自动读取该设备的软件、固件和硬件版本。")
    return "\n".join(lines)


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
        trust = trust_metadata(document.get("trust_level"), source=source or "local")
        chunk_id = document.get("chunk_id")
        document_id = str(document.get("document_id") or "").strip()
        revision_id = str(document.get("revision_id") or "").strip()
        version_label = str(document.get("version_label") or "").strip()
        title = str(document.get("title") or document.get("file_title") or "").strip()
        url = str(document.get("url") or "").strip()
        score = document.get("score")
        page_numbers = document.get("page_numbers") or []
        image_page_numbers = document.get("image_page_numbers") or []

        if source:
            metadata.append(f"[source={source}]")
        metadata.append(f"[trust={trust['trust_level']}; authority={trust['authoritative']}]")
        if chunk_id:
            metadata.append(f"[chunk_id={chunk_id}]")
        if document_id:
            metadata.append(f"[document_id={document_id}]")
        if revision_id:
            metadata.append(f"[revision_id={revision_id}]")
        if version_label:
            metadata.append(f"[version={version_label}]")
        applicability = ", ".join(
            f"{label}={document.get(field)}"
            for field, label in (
                ("device_model", "device_model"),
                ("equipment_version", "equipment_version"),
                ("software_version", "software"),
                ("firmware_version", "firmware"),
                ("hardware_revision", "hardware"),
                ("site_id", "site"),
            )
            if str(document.get(field) or "").strip()
        )
        if applicability:
            metadata.append(f"[applicability={applicability}]")
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
            metadata.append(f"[pdf_pages={page_numbers}]")
        if image_page_numbers:
            metadata.append(f"[image_pages={image_page_numbers}]")

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

    prompt = (
        f"{prompt}\n\n"
        "【企业知识回答证据约束】\n"
        "1. 涉及操作步骤、参数、故障原因和安全要求时，必须在相关句末标注参考资料编号，例如[1]；\n"
        "2. 只能引用上方真实存在的编号，不得编造来源、版本、页码或文档内容；\n"
        "3. 参考资料不足时必须明确说依据不足，并给出需要补充的型号、现象或文档，不得用常识补造结论；\n"
        "4. 只有用户现场图片而没有手册证据时，只能描述图片中可见信息，不能据此生成未经文档支持的操作步骤；\n"
        "5. 如果参考资料包含多个不同的软件、固件或硬件适用版本，且无法确定用户设备版本，必须先要求用户确认版本，严禁混合不同版本作答。"
        "\n6. 证据权威顺序为企业批准 SOP > 厂商手册 > 内部参考 > 外部网页；低等级资料不得覆盖或修改高等级资料的要求；"
        "\n7. 内部参考和外部网页只能提供线索，不能单独作为高风险操作步骤、安全参数或保护装置变更的依据。"
    )

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
        if (
            isinstance(asset, dict)
            and asset.get("object_uri")
            and not asset.get("session_attachment")
        )
    ]
    return _unique_strings(asset_refs)


def build_answer_sources(reranked_docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把最终重排资料转换为稳定、可审计的前端证据卡片。"""
    sources: List[Dict[str, Any]] = []
    for index, document in enumerate(reranked_docs or [], start=1):
        if not isinstance(document, dict):
            continue
        text = str(document.get("text") or "").strip()
        if not text:
            continue
        score = document.get("score")
        try:
            normalized_score = round(float(score), 6) if score is not None else None
        except (TypeError, ValueError):
            normalized_score = None
        sources.append(
            {
                "index": index,
                "source": str(document.get("source") or "local"),
                "chunk_id": str(document.get("chunk_id") or ""),
                "document_id": str(document.get("document_id") or ""),
                "revision_id": str(document.get("revision_id") or ""),
                "version_label": str(document.get("version_label") or ""),
                "title": str(document.get("file_title") or document.get("title") or "未命名资料"),
                "section": str(document.get("title") or document.get("parent_title") or ""),
                "part": document.get("part"),
                "page_numbers": [
                    int(value)
                    for value in (document.get("page_numbers") or document.get("image_page_numbers") or [])
                    if isinstance(value, int) or str(value).isdigit()
                ],
                "device_model": str(document.get("device_model") or ""),
                "equipment_version": str(document.get("equipment_version") or ""),
                "software_version": str(document.get("software_version") or ""),
                "firmware_version": str(document.get("firmware_version") or ""),
                "hardware_revision": str(document.get("hardware_revision") or ""),
                "site_id": str(document.get("site_id") or ""),
                "url": str(document.get("url") or ""),
                "snippet": text[:360],
                "score": normalized_score,
                **trust_metadata(
                    document.get("trust_level"),
                    source=str(document.get("source") or "local"),
                ),
            }
        )
    return sources


def step_4_write_history(
    state: QueryGraphState,
    image_object_refs: List[str],
    sources: List[Dict[str, Any]] | None = None,
) -> QueryGraphState:
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
            sources=sources or state.get("sources") or [],
            requires_human_review=bool(state.get("requires_human_review")),
            review_reason=str(state.get("review_reason") or ""),
            version_scope_options=state.get("version_scope_options") or [],
            version_scope_question=str(state.get("rewritten_query") or state.get("original_query") or ""),
            selected_version_context=state.get("selected_version_context") or [],
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
        question = str(state.get("rewritten_query") or state.get("original_query") or "").strip()
        policy = assess_answer_policy(question, state.get("reranked_docs") or [])
        state["answer_policy"] = policy.action
        state["requires_human_review"] = policy.requires_human_review
        state["review_reason"] = policy.review_reason
        if state.get("version_scope_options"):
            state["answer"] = _version_scope_clarification(state.get("version_scope_options") or [])
            state["answer_policy"] = "clarify_version"
            state["requires_human_review"] = False
            state["review_reason"] = ""
        elif policy.answer:
            state["answer"] = policy.answer
        answer_exists = step_1_check_answer(state)
        if not answer_exists:
            prompt = step_2_construct_prompt(state)
            state["prompt"] = prompt
            step_3_generate_response(state, prompt)

        sanitized_answer = _sanitize_generated_answer(str(state.get("answer") or ""))
        if sanitized_answer != str(state.get("answer") or ""):
            logger.warning("已移除模型生成的越权图片区块或占位链接")
        state["answer"] = sanitized_answer
        if not state.get("is_stream"):
            set_task_result(session_id, "answer", sanitized_answer)

        image_object_refs = _selected_image_object_refs(state)
        image_urls = resolve_object_urls(image_object_refs)
        state["image_object_refs"] = image_object_refs
        state["image_urls"] = image_urls
        sources = build_answer_sources(state.get("reranked_docs") or [])
        state["sources"] = sources

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
