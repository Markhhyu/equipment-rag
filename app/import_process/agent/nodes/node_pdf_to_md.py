import os
import sys
from pathlib import Path

from app.clients.mineru_client import get_mineru_client
from app.core.logger import logger
from app.import_process.agent.state import ImportGraphState, create_default_state
from app.utils.format_utils import format_state
from app.utils.task_utils import add_done_task, add_running_task


def _validate_paths(state: ImportGraphState) -> tuple[Path, Path]:
    """校验原始PDF和输出目录。"""

    pdf_path = Path((state.get("pdf_path") or "").strip())
    output_dir = Path((state.get("local_dir") or "").strip())

    if not str(pdf_path):
        raise ValueError("工作流状态缺少pdf_path")

    if not str(output_dir):
        raise ValueError("工作流状态缺少local_dir")

    if not pdf_path.exists() or not pdf_path.is_file():
        raise FileNotFoundError(f"PDF文件不存在：{pdf_path.absolute()}")

    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"当前节点只支持PDF文件：{pdf_path.name}")

    output_dir.mkdir(parents=True, exist_ok=True)
    return pdf_path.resolve(), output_dir.resolve()


def node_pdf_to_md(state: ImportGraphState) -> ImportGraphState:
    """
    使用MinerU 3.x将PDF解析为Markdown和结构化JSON。

    输出内容：
    1. Markdown；
    2. 图片；
    3. content_list.json；
    4. content_list_v2.json；
    5. middle.json。
    """

    node_name = sys._getframe().f_code.co_name
    logger.debug(f"【{node_name}】开始执行，state={format_state(state)}")
    add_running_task(state["task_id"], node_name)

    try:
        pdf_path, output_dir = _validate_paths(state)
        result = get_mineru_client().parse_file(str(pdf_path), str(output_dir))

        # 将MinerU解析结果写入LangGraph状态。
        state["md_path"] = result.md_path
        state["mineru_task_id"] = result.task_id
        state["mineru_output_dir"] = result.output_dir
        state["mineru_content_list_path"] = result.content_list_path
        state["mineru_content_list_v2_path"] = result.content_list_v2_path
        state["mineru_middle_json_path"] = result.middle_json_path
        state["mineru_version"] = result.mineru_version
        state["mineru_api_protocol_version"] = result.api_protocol_version

        with open(result.md_path, "r", encoding="utf-8") as file:
            state["md_content"] = file.read()

        logger.info(
            f"【{node_name}】MinerU解析完成，task_id={result.task_id}，"
            f"version={result.mineru_version}，md_length={len(state['md_content'])}"
        )

    except Exception as e:
        logger.exception(f"【{node_name}】MinerU解析失败：{e}")
        raise
    finally:
        add_done_task(state["task_id"], node_name)
        logger.debug(f"【{node_name}】执行结束，state={format_state(state)}")

    return state


if __name__ == "__main__":
    """本地测试MinerU解析节点。"""

    from app.utils.path_util import PROJECT_ROOT

    test_pdf_path = os.path.join(PROJECT_ROOT, "doc", "hak180产品安全手册.pdf")

    test_state = create_default_state(
        task_id="test_mineru_3_task_001",
        pdf_path=test_pdf_path,
        local_dir=os.path.join(PROJECT_ROOT, "output")
    )

    result_state = node_pdf_to_md(test_state)
    logger.info(f"测试完成，MD路径：{result_state['md_path']}")