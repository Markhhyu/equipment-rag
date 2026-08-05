import os
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from langchain_core.messages import SystemMessage, HumanMessage

from app.platform.ai.prompts import load_prompt
from app.platform.config.rag_tuning_config import rag_tuning_config
from app.modules.qa.graph.state import QueryGraphState
from app.platform.runtime.task_progress import add_done_task, add_running_task
from app.modules.qa.infrastructure.history import get_recent_messages, save_chat_message
from app.platform.ai.chat import get_llm_client
from app.platform.ai.embeddings import generate_embeddings
from app.platform.vector_store.milvus import create_hybrid_search_requests, get_milvus_client, hybrid_search
from dotenv import load_dotenv, find_dotenv
from app.platform.observability.logging import logger
from app.platform.security.tenancy import tenant_filter

load_dotenv(find_dotenv())


# 设备型号通常由英文字母和数字组成，并可能包含空格、短横线或下划线。
# 这里故意不匹配纯中文商品名：中文名称仍交给LLM理解，明确的型号代码则由程序确定性处理，
# 避免历史对话中的旧设备名称覆盖用户本轮已经写清楚的新型号。
_MODEL_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{1,12}(?:[\s._-]*\d[A-Za-z0-9._-]*))(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# 只有非常明确、且本身不包含新问题内容的肯定回复才会触发“确认上轮单个候选”。
# 例如“是的”“就是这个”可以确认；“是的，但是我问的是PT770”不会被本规则截断，
# 后者会继续由当前问题中的明确型号规则处理。
_AFFIRMATIVE_REPLY_PATTERN = re.compile(
    r"^(?:是|是的|对|对的|就是|就是这个|就是它|就是这款|确认|没错|可以|嗯|好的?)$"
)


def _unique_strings(values: List[str]) -> List[str]:
    """按出现顺序去重，避免使用set导致候选型号顺序随机变化。"""
    result: List[str] = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _normalize_item_name(value: str) -> str:
    """移除空格和标点并统一小写，用于商品名的稳定词法比较。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def _extract_model_tokens(value: str) -> List[str]:
    """
    从文本中提取标准化设备型号。

    示例：
    - ``LJ2268/LJ2268W激光打印机`` -> ``["LJ2268", "LJ2268W"]``
    - ``HAK 180烫金机`` -> ``["HAK180"]``
    - ``RS-12`` -> ``["RS-12"]``（比较时会统一移除分隔符）
    """
    tokens: List[str] = []
    for match in _MODEL_TOKEN_PATTERN.finditer(str(value or "")):
        normalized = re.sub(r"[^A-Za-z0-9]+", "", match.group(1)).upper()
        if normalized and normalized not in tokens:
            tokens.append(normalized)
    return tokens


def _extract_explicit_item_names(query: str) -> List[str]:
    """
    提取当前问题里明确写出的型号，作为优先于LLM和历史记录的设备线索。

    返回原始可读形式是为了让向量检索仍能使用用户输入；真正比较时会再标准化。
    """
    names = [re.sub(r"\s+", " ", match.group(1)).strip() for match in _MODEL_TOKEN_PATTERN.finditer(query or "")]
    return _unique_strings(names)


def _is_lexical_alias(extracted_name: str, candidate_name: str) -> bool:
    """
    判断检索候选是否是用户型号的词法别名。

    型号token相交是最可靠的依据，例如LJ2268可以命中
    “LJ2268/LJ2268W激光打印机”。没有型号时，只允许完整名称相等或较长名称包含关系。
    """
    extracted_tokens = set(_extract_model_tokens(extracted_name))
    candidate_tokens = set(_extract_model_tokens(candidate_name))
    if extracted_tokens and candidate_tokens:
        return bool(extracted_tokens & candidate_tokens)

    extracted_normalized = _normalize_item_name(extracted_name)
    candidate_normalized = _normalize_item_name(candidate_name)
    if not extracted_normalized or not candidate_normalized:
        return False
    if extracted_normalized == candidate_normalized:
        return True
    return len(extracted_normalized) >= 4 and (
        extracted_normalized in candidate_normalized or candidate_normalized in extracted_normalized
    )


def _resolve_pending_confirmation(query: str, history: List[Dict]) -> Tuple[Optional[str], str]:
    """
    解析“是的/就是这个”对上轮单候选澄清的确认。

    候选保存在助手历史消息的item_names字段中。只有上轮确实是澄清问题且只有一个候选时
    才自动确认；多个候选时不能猜测用户选择了哪一个。第二个返回值是澄清前的用户问题，
    用于把“是的”恢复成可检索的完整问题。
    """
    compact_query = re.sub(r"[\s，。！？、,.!?]+", "", str(query or ""))
    if not _AFFIRMATIVE_REPLY_PATTERN.fullmatch(compact_query):
        return None, ""

    for index in range(len(history or []) - 1, -1, -1):
        message = history[index]
        if message.get("role") != "assistant":
            continue
        answer = str(message.get("text") or "")
        candidates = _unique_strings(message.get("item_names") or [])
        if "您是想问以下哪个产品" not in answer:
            return None, ""
        if len(candidates) != 1:
            return None, ""

        previous_query = ""
        for previous in reversed(history[:index]):
            if previous.get("role") == "user":
                previous_query = str(previous.get("rewritten_query") or previous.get("text") or "").strip()
                break
        return candidates[0], previous_query

    return None, ""


def _rewrite_confirmed_question(item_name: str, previous_query: str) -> str:
    """把简单肯定回复恢复成包含已确认型号的独立问题。"""
    if previous_query:
        if _is_lexical_alias(previous_query, item_name):
            return previous_query
        return f"关于{item_name}，{previous_query}"
    return f"关于{item_name}的使用和维护问题"


def _normalize_version_selection(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def _resolve_pending_version_selection(
    query: str,
    history: List[Dict],
    selected_scope_id: str = "",
) -> Tuple[Optional[Dict[str, Any]], str, List[str]]:
    """Match a user's version choice against the latest structured clarification message."""
    normalized_query = _normalize_version_selection(query)
    requested_scope_id = str(selected_scope_id or "").strip().casefold()
    if not normalized_query and not requested_scope_id:
        return None, "", []

    for message in reversed(history or []):
        if message.get("role") != "assistant":
            continue
        groups = message.get("version_scope_options") or []
        if not groups:
            return None, "", []

        choices = [
            choice
            for group in groups
            if isinstance(group, dict)
            for choice in (group.get("choices") or [])
            if isinstance(choice, dict) and choice.get("scope_id")
        ]
        matches = []
        for choice in choices:
            scope_id = str(choice.get("scope_id") or "").casefold()
            label = _normalize_version_selection(str(choice.get("label") or ""))
            values = {
                _normalize_version_selection(str(choice.get(field) or ""))
                for field in (
                    "equipment_version",
                    "software_version",
                    "firmware_version",
                    "hardware_revision",
                    "site_id",
                    "asset_ids",
                )
                if choice.get(field)
            }
            if requested_scope_id == scope_id or scope_id in str(query or "").casefold() or label in normalized_query or any(
                value and value in normalized_query for value in values
            ):
                matches.append(choice)

        unique_matches = {str(choice["scope_id"]): choice for choice in matches}
        if len(unique_matches) != 1:
            return None, "", []
        selected = next(iter(unique_matches.values()))
        previous_query = str(message.get("version_scope_question") or "").strip()
        item_names = _unique_strings(message.get("item_names") or [])
        return selected, previous_query, item_names

    return None, "", []


def _rewrite_version_question(choice: Dict[str, Any], previous_query: str) -> str:
    scope_id = str(choice.get("scope_id") or "").strip()
    label = str(choice.get("label") or "已选择版本").strip()
    base = previous_query or "请说明该设备的使用和维护要求"
    return f"{base}；已确认适用范围：{label} [version_scope:{scope_id}]"


def step_3_extract_info(query: str, history: List[Dict]) -> Dict:
    """
    利用LLM从当前问题以及历史会话中提取出主要询问的商品名称item_names（可多个，JSON列表形式）
    若商品名不够明确则返回空列表，同时根据上下文重新改写问题，保证问题独立完整
    :param query: 字符串 - 用户当前原始查询问题（如："这个多少钱？"）
    :param history: 列表[字典] - 近期会话历史
    :return: 字典 - 提取结果，格式：{"item_names": [], "rewritten_query": ""}
    """
    logger.info("Step 3: 开始提取信息 (LLM)")
    
    # 1. 初始化准备
    client = get_llm_client(json_mode=True)
    
    # 构造历史对话文本
    history_text = ""
    for msg in history:
        history_text += f"{msg.get('role', 'unknown')}: {msg.get('text', '')}\n"
    
    logger.info(f"Step 3: 历史上下文构建完成，长度: {len(history_text)} 字符")

    # 2. 加载提示词
    try:
        # 使用关键字参数传递，避免参数位置错误
        prompt = load_prompt("rewritten_query_and_itemnames", history_text=history_text, query=query)
        logger.debug(f"Step 3: 提示词加载成功，Prompt长度: {len(prompt)}")
    except Exception as e:
        logger.error(f"Step 3: 加载提示词失败: {e}")
        return {"item_names": [], "rewritten_query": query}

    messages = [
        SystemMessage(content=load_prompt("query_rewrite_system")),
        HumanMessage(content=prompt)
    ]

    try:
        logger.info("Step 3: 正在调用 LLM 进行提取...")
        response = client.invoke(messages)
        content = response.content
        logger.debug(f"Step 3: LLM 原始响应: {content}")

        # 清理 Markdown 代码块
        if content.startswith("```json"):
            content = content.replace("```json", "").replace("```", "")
        
        result = json.loads(content)
        
        # 健壮性检查：JSON字段类型不正确时回退，避免模型偶尔返回字符串导致后续逐字符检索。
        if not isinstance(result.get("item_names"), list):
            result["item_names"] = []
        result["item_names"] = _unique_strings(result["item_names"])
        if not isinstance(result.get("rewritten_query"), str) or not result["rewritten_query"].strip():
            result["rewritten_query"] = query
            
        logger.info(f"Step 3: 提取结果解析成功 - 商品名: {result['item_names']}, 重写问题: {result['rewritten_query']}")
        return result

    except Exception as e:
        logger.error(f"Step 3: LLM 提取或解析失败: {e}")
        return {"item_names": [], "rewritten_query": query}


def step_4_vectorize_and_query(item_names: List[str], tenant_id: str = "local") -> List[Dict]:
    """
    对提取的 item_names 进行向量化并在 Milvus 中进行混合搜索
    """
    logger.info(f"Step 4: 开始向量化检索，目标商品: {item_names}")
    results = []
    
    client = get_milvus_client()
    if not client:
        logger.error("Step 4: 无法连接到 Milvus")
        return results

    collection_name = os.environ.get("ITEM_NAME_COLLECTION")
    if not collection_name:
        logger.error("Step 4: 环境变量中未找到 ITEM_NAME_COLLECTION")
        return results

    try:
        logger.info("Step 4: 正在生成 Embedding (Dense + Sparse)...")
        embeddings = generate_embeddings(item_names)
        logger.info(f"Step 4: 向量生成完成，开始 Milvus 搜索 (Collection: {collection_name})")

        for i, name in enumerate(item_names):
            try:
                dense_vector = embeddings.get("dense")[i]
                sparse_vector = embeddings.get("sparse")[i]

                # 构造混合搜索请求
                reqs = create_hybrid_search_requests(
                    dense_vector=dense_vector,
                    sparse_vector=sparse_vector,
                    expr=tenant_filter(tenant_id),
                    limit=5
                )

                # 执行混合搜索
                # 权重调整为 0.8 (Dense) / 0.2 (Sparse) 以优化评分
                search_res = hybrid_search(
                    client=client,
                    collection_name=collection_name,
                    reqs=reqs,
                    ranker_weights=(0.8, 0.2), 
                    limit=5,
                    norm_score=True,
                    output_fields=["item_name"]
                )

                matches = []
                if search_res and len(search_res) > 0:
                    for hit in search_res[0]:
                        entity = hit.get("entity") or {}
                        item_name = entity.get("item_name")
                        score = hit.get("distance")
                        
                        if item_name:
                            matches.append({
                                "item_name": item_name,
                                "score": score
                            })
                            logger.debug(f"Step 4: '{name}' 匹配项: {item_name} (Score: {score:.4f})")

                results.append({
                    "extracted_name": name,
                    "matches": matches
                })
                logger.info(f"Step 4: 商品 '{name}' 检索完成，找到 {len(matches)} 个匹配项")

            except Exception as inner_e:
                logger.error(f"Step 4: 处理商品 '{name}' 时出错: {inner_e}")
                results.append({"extracted_name": name, "matches": []})

    except Exception as e:
        logger.error(f"Step 4: 向量化或搜索过程发生全局错误: {e}")

    return results


def step_5_align_item_names(query_results: List[Dict]) -> Dict:
    """
    综合型号词法关系、向量分数和Top1/Top2差距，对齐商品名。

    规则优先级：
    1. 用户输入的型号与候选型号token一致时直接确认；
    2. 用户明确输入了型号，但所有候选型号都不同，则拒绝纯向量误匹配；
    3. 没有明确型号时，只有Top1高分且明显领先Top2才自动确认；
    4. 其余达到候选阈值的结果要求用户澄清。
    """
    logger.info("Step 5: 开始对齐商品名 (Score Analysis)")
    
    confirmed_item_names = []
    options = []

    for res in query_results:
        extracted_name = res.get("extracted_name", "").strip()
        matches = res.get("matches", []) or []
        
        if not matches:
            logger.info(f"Step 5: '{extracted_name}' 无匹配结果")
            continue

        # 按分数降序
        matches.sort(key=lambda x: x.get("score", 0), reverse=True)
        
        # 打印详细评分日志辅助调试
        top_matches_log = ", ".join([f"{m['item_name']}({m['score']:.3f})" for m in matches[:3]])
        logger.info(f"Step 5: '{extracted_name}' Top匹配: {top_matches_log}")

        # 规则A：词法别名不依赖向量绝对分数。LJ2268与“LJ2268/LJ2268W激光打印机”
        # 即使只有0.68分，也比没有共同型号token的0.95分候选更可信。
        lexical_matches = [
            match
            for match in matches
            if _is_lexical_alias(extracted_name, str(match.get("item_name") or ""))
        ]
        if lexical_matches:
            confirmed_name = str(lexical_matches[0].get("item_name") or "").strip()
            if confirmed_name:
                confirmed_item_names.append(confirmed_name)
                logger.info(f"Step 5: 规则A命中（型号/名称词法匹配）-> 确认: {confirmed_name}")
            continue

        # 规则B：用户已经明确输入型号时，候选必须拥有相同型号token。
        # 这条规则会拒绝已复现的错误：PT770被0.682分错误映射到LJ2268打印机。
        if _extract_model_tokens(extracted_name):
            logger.warning(f"Step 5: 规则B命中（明确型号不一致）-> 拒绝全部语义候选: {extracted_name}")
            continue

        # 规则C：对于没有型号代码的普通名称，自动确认阈值更严格，并要求第一名明显领先第二名。
        top_score = float(matches[0].get("score") or 0)
        second_score = float(matches[1].get("score") or 0) if len(matches) > 1 else 0.0
        score_margin = top_score - second_score
        if (
            top_score >= rag_tuning_config.item_name_auto_confirm_score
            and score_margin >= rag_tuning_config.item_name_auto_confirm_margin
        ):
            confirmed_name = str(matches[0].get("item_name") or "").strip()
            if confirmed_name:
                confirmed_item_names.append(confirmed_name)
                logger.info(
                    f"Step 5: 规则C命中（高分且领先）-> 确认: {confirmed_name}, "
                    f"Top1={top_score:.3f}, Margin={score_margin:.3f}"
                )
            continue

        # 规则D：达到候选阈值但不够自动确认的结果只用于澄清，不直接作为用户最终设备。
        candidate_matches = [
            match
            for match in matches
            if float(match.get("score") or 0) >= rag_tuning_config.item_name_candidate_score
        ]
        if candidate_matches:
            current_options = [str(match.get("item_name") or "").strip() for match in candidate_matches[:5]]
            current_options = [value for value in current_options if value]
            options.extend(current_options)
            logger.info(f"Step 5: 规则D命中（需要澄清）-> 添加候选: {current_options}")
            continue

        logger.info("Step 5: 规则E命中（低置信度）-> 无匹配")

    result = {
        "confirmed_item_names": _unique_strings(confirmed_item_names),
        "options": _unique_strings(options)
    }
    logger.info(f"Step 5: 对齐结果: {result}")
    return result


def step_6_check_confirmation(state: Dict, align_result: Dict, rewritten_query: str) -> Dict:
    """
    检查对齐结果，更新 State
    """
    logger.info("Step 6: 检查确认状态并更新 State")
    
    # 健壮性处理
    if align_result is None:
        align_result = {}

    confirmed = align_result.get("confirmed_item_names", [])
    options = align_result.get("options", [])

    # 分支 A: 有确认商品名
    if confirmed:
        logger.info(f"Step 6: [分支A] 存在确认商品名: {confirmed}")

        state["item_names"] = confirmed
        state["pending_item_names"] = []
        state["rewritten_query"] = rewritten_query
        if "answer" in state:
            del state["answer"]
        return state

    # 分支 B: 有候选商品名
    if options:
        logger.info(f"Step 6: [分支B] 存在候选商品名: {options}")
        options_str = "、".join(options[:3])
        answer = f"您是想问以下哪个产品：{options_str}？请明确一下型号。"
        state["answer"] = answer
        state["item_names"] = []
        # 候选写入本轮助手消息，下一轮用户只回复“是的”时才能安全恢复单个候选。
        state["pending_item_names"] = options
        state["rewritten_query"] = rewritten_query
        return state

    # 分支 C: 无结果
    logger.info("Step 6: [分支C] 无确认也无候选")
    state["answer"] = "抱歉，未找到相关产品，请提供准确型号以便我为您查询。"
    state["item_names"] = []
    state["pending_item_names"] = []
    state["rewritten_query"] = rewritten_query
    return state


def step_7_write_history(state: Dict, session_id: str, rewritten_query: str, message_id: str) -> Dict:
    """
    写入最终历史记录
    """
    logger.info("Step 7: 写入会话历史")
    
    # 这里只更新本轮用户消息。助手回答统一由node_answer_output写入一次，
    # 否则澄清/未找到产品这两类回答会在MongoDB中重复保存两份。
    logger.info(f"Step 7: 更新用户消息 (ID: {message_id})")
    save_chat_message(
        session_id=session_id,
        role="user",
        text=state["original_query"],
        rewritten_query=rewritten_query,
        item_names=state.get("item_names", []),
        image_urls=state.get("user_image_refs", []),
        message_id=message_id
    )

    return state


def node_item_name_confirm(state: QueryGraphState) -> QueryGraphState:
    """
    主节点函数：商品名称确认流程
    """
    logger.info(">>> node_item_name_confirm: 开始处理")
    
    session_id = state["session_id"]
    original_query = state.get("original_query", "")
    is_stream = state.get("is_stream", False)

    # 标记任务开始
    add_running_task(session_id, "node_item_name_confirm", is_stream)

    try:
        # 1. 获取最近历史。get_recent_messages已保证“先取最新N条，再按旧到新排列”。
        history = get_recent_messages(session_id, limit=10)
        logger.info(f"Node: 获取到 {len(history)} 条历史消息")

        # 2. 先保存用户原始消息，流程结束时只补充重写问题和最终设备名，不改变创建时间。
        message_id = save_chat_message(
            session_id,
            "user",
            original_query,
            "",
            state.get("item_names", []),
            state.get("user_image_refs", []),
        )
        logger.debug(f"Node: 用户消息已初始保存, ID: {message_id}")

        # 3. 信息提取分三层，确定性越强的规则优先级越高。
        selected_version, previous_version_query, version_item_names = _resolve_pending_version_selection(
            original_query,
            history,
            str(state.get("selected_version_scope_id") or ""),
        )

        # 第一层：当前问题明确写了型号，直接采用当前型号，绝不允许历史设备覆盖。
        explicit_item_names = _extract_explicit_item_names(original_query)

        # 第二层：当前问题只是“是的”，尝试确认上一轮保存的单个候选。
        pending_item_name, previous_query = _resolve_pending_confirmation(original_query, history)

        if selected_version and version_item_names:
            item_names = version_item_names
            rewritten_query = _rewrite_version_question(selected_version, previous_version_query)
            state["selected_version_context"] = [selected_version]
            logger.info(
                f"Node: 用户确认上轮适用版本: {selected_version.get('label')}，"
                f"scope_id={selected_version.get('scope_id')}"
            )
        elif explicit_item_names:
            item_names = explicit_item_names
            rewritten_query = original_query
            logger.info(f"Node: 当前问题检测到明确型号，跳过LLM型号提取: {item_names}")
        elif pending_item_name:
            item_names = [pending_item_name]
            rewritten_query = _rewrite_confirmed_question(pending_item_name, previous_query)
            logger.info(f"Node: 用户确认上轮单个候选: {pending_item_name}")
        else:
            # 第三层：没有明确型号时，再让LLM结合历史处理中文名称、代词和省略问题。
            extract_res = step_3_extract_info(original_query, history)
            item_names = extract_res.get("item_names", [])
            rewritten_query = extract_res.get("rewritten_query", original_query)

        state["rewritten_query"] = rewritten_query
        align_result: Dict[str, List[str]] = {}

        # 已确认的上轮候选来自真实Milvus结果，不需要再次用向量分数证明自己。
        if selected_version and version_item_names:
            align_result = {"confirmed_item_names": version_item_names, "options": []}
        elif pending_item_name:
            align_result = {"confirmed_item_names": [pending_item_name], "options": []}
        elif item_names:
            query_results = step_4_vectorize_and_query(item_names, str(state.get("tenant_id") or "local"))
            align_result = step_5_align_item_names(query_results)
        else:
            logger.info("Node: 未提取到商品名，跳过向量检索")

        # 4. 根据确认/候选/无结果三个分支更新状态。
        state = step_6_check_confirmation(state, align_result, rewritten_query)

        # 5. 仅更新本轮用户消息；助手消息统一由最终输出节点保存。
        final_state = step_7_write_history(state, session_id, rewritten_query, message_id)

        # 历史不包含本轮刚保存的用户消息，避免最终回答Prompt重复出现当前问题。
        final_state["history"] = [
            {
                "role": message.get("role", ""),
                "text": message.get("text", ""),
                "item_names": message.get("item_names", []),
                "selected_version_context": message.get("selected_version_context", []),
                "version_scope_options": message.get("version_scope_options", []),
            }
            for message in history
        ]

        logger.info(f"Node: 处理结束, Final State Item Names: {final_state.get('item_names')}")
        return final_state
    finally:
        # 即使节点抛出异常也移除“进行中”标记，真正的失败状态由query_service统一设置。
        add_done_task(session_id, "node_item_name_confirm", is_stream)


if __name__ == "__main__":
    # 测试代码块
    print("\n" + "="*50)
    print(">>> 启动 node_item_name_confirm 本地测试")
    print("="*50)
    
    # 模拟输入状态
    mock_state = {
        "session_id": "test_debug_session_001",
        "original_query": "HAK 180 烫金机多少钱？",  # 针对用户提到的具体 case
        "is_stream": False,
        "item_names": []
    }

    try:
        # 运行节点
        result = node_item_name_confirm(mock_state)
        
        print("\n" + "="*50)
        print(">>> 测试结果摘要:")
        print(f"Rewritten Query: {result.get('rewritten_query')}")
        print(f"Item Names: {result.get('item_names')}")
        print(f"Answer: {result.get('answer')}")
        print("="*50)

    except Exception as e:
        logger.exception(f"测试运行期间发生未捕获异常: {e}")
