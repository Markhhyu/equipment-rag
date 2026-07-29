import json
import os

from app.core.logger import logger
from app.import_process.agent.main_graph import kb_import_app
from app.import_process.agent.state import create_default_state
from app.utils.path_util import PROJECT_ROOT

logger.info("===== 开始测试 =====")

pdf_path = os.path.join(PROJECT_ROOT, "doc", "万用表RS-12的使用.pdf")
local_dir = os.path.join(PROJECT_ROOT, "temp-files")

if not os.path.isfile(pdf_path):
    raise FileNotFoundError(f"测试文件不存在：{pdf_path}")

initial_state = create_default_state(
    task_id="test_graph_flow_001",
    local_file_path=pdf_path,
    pdf_path=pdf_path,
    local_dir=local_dir
)

logger.info(f"测试PDF路径：{pdf_path}")
logger.info(f"解析结果根目录：{local_dir}")

final_state = None

for event in kb_import_app.stream(initial_state):
    for key, value in event.items():
        logger.info(f"节点：{key}")
        final_state = value

logger.info(f"最终状态：\n{json.dumps(final_state, indent=4, ensure_ascii=False, default=str)}")

logger.info("图结构：")
kb_import_app.get_graph().print_ascii()

logger.info("===== 测试结束 =====")