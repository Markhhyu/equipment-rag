import sys

from app.clients.document_registry_utils import get_document_registry
from app.core.logger import logger
from app.query_process.agent.nodes.node_answer_output import _version_scope_clarification
from app.query_process.agent.state import QueryGraphState
from app.query_process.version_context import latest_pinned_version_context, resolve_version_context
from app.utils.task_utils import add_done_task, add_running_task


def node_version_context(state: QueryGraphState) -> QueryGraphState:
    """Resolve the governed document revision set before any vector candidate is retrieved."""
    session_id = str(state.get("session_id") or "")
    node_name = sys._getframe().f_code.co_name
    add_running_task(session_id, node_name, state.get("is_stream"))
    try:
        item_names = state.get("item_names") or []
        if not item_names:
            return {"query_revision_ids": [], "selected_version_context": []}
        try:
            active_versions = get_document_registry().list_active_versions(
                str(state.get("tenant_id") or "local"),
                item_names,
            )
        except Exception as exc:
            logger.warning(f"读取生效版本上下文失败，降级为生命周期过滤：{exc}")
            return {"query_revision_ids": [], "selected_version_context": []}

        selected_context = [] if state.get("reset_version_context") else state.get("selected_version_context") or []
        if not selected_context and not state.get("reset_version_context"):
            selected_context = latest_pinned_version_context(state.get("history") or [])
        result = resolve_version_context(
            str(state.get("rewritten_query") or state.get("original_query") or ""),
            active_versions,
            selected_scope_id=str(state.get("selected_version_scope_id") or ""),
            pinned_context=selected_context,
        )
        updates: QueryGraphState = {
            "query_revision_ids": result["revision_ids"],
            "selected_version_context": result["selected_scopes"],
            "version_scope_options": result["version_scope_options"],
        }
        if result["status"] == "ambiguous":
            updates["answer"] = _version_scope_clarification(result["version_scope_options"])
        logger.info(
            f"版本上下文解析完成：status={result['status']}，"
            f"revision_count={len(result['revision_ids'])}，option_groups={len(result['version_scope_options'])}"
        )
        return updates
    finally:
        add_done_task(session_id, node_name, state.get("is_stream"))
