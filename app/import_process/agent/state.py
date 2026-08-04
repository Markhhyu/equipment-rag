from typing import TypedDict
import copy
from app.core.logger import logger


class ImportGraphState(TypedDict):
    """
    图的状态定义，包含所有节点产生和消费的数据字段。
    TypedDict 让我们在代码中能有自动补全和类型检查。
    使用字典式访问（如state["session_id"]、state.get("embedding_chunks")）
    """
    task_id: str  # 任务唯一ID，用于追踪日志
    tenant_id: str  # 调用方租户标识，用于存储与检索隔离
    document_id: str  # 跨版本稳定的知识文档编号
    revision_id: str  # 本次导入生成的不可变版本编号
    version_label: str  # 人工可读业务版本，例如V2.1或2026版
    trust_level: str  # enterprise_sop/manufacturer_manual/internal_reference
    device_model: str  # 适用设备型号，例如LJ2268
    equipment_version: str  # 同型号设备的版本或代次，例如A版、第二代
    software_version: str  # 适用上位机/设备软件版本
    firmware_version: str  # 适用固件版本
    hardware_revision: str  # 适用硬件修订版
    site_id: str  # 可选厂区/站点范围
    asset_ids: list[str]  # 可选设备实例编号列表

    # --- 流程控制标记 ---
    is_md_read_enabled: bool  # 是否启用 Markdown 读取路径
    is_pdf_read_enabled: bool  # 是否启用 PDF 读取路径

    # --- 切块相关 ---
    is_normal_split_enabled: bool
    is_silicon_flow_api_enabled: bool
    is_advanced_split_enabled: bool
    is_vllm_enabled: bool

    # --- 路径相关 ---
    local_dir: str  # 当前工作目录或输出目录
    local_file_path: str  # 原始输入文件路径
    file_title: str  # 文件标题（文件名去后缀）
    pdf_path: str  # PDF 文件路径 (如果输入是PDF)
    md_path: str  # Markdown 文件路径 (转换后或直接输入的)
    split_path: str  # 分块后的文件路径
    embeddings_path: str  # 向量数据库文件路径

    # --- MinerU解析结果 ---
    mineru_task_id: str  # MinerU异步任务ID
    mineru_output_dir: str  # 本次解析结果目录
    mineru_content_list_path: str  # 稳定版结构化内容列表
    mineru_content_list_v2_path: str  # V2结构化内容列表，暂不作为正式数据源
    mineru_middle_json_path: str  # 完整中间结构数据
    mineru_version: str  # 实际解析使用的MinerU版本
    mineru_api_protocol_version: str  # MinerU API协议版本

    # --- 内容数据 ---
    md_content: str  # Markdown 的全文内容
    chunks: list  # 切片后的文本列表，包含 metadata
    item_name: str  # 识别出的主体名称 (如: "万用表")，用于增强检索

    # --- 数据库相关 ---
    embeddings_content: list  # 包含向量数据的列表，准备写入 Milvus


# 建议定一个初始化对象，方便后续使用
# 定义图状态的默认初始值
graph_default_state: ImportGraphState = {
    "task_id": "",
    "tenant_id": "local",
    "document_id": "",
    "revision_id": "",
    "version_label": "legacy-v1",
    "trust_level": "manufacturer_manual",
    "device_model": "",
    "equipment_version": "",
    "software_version": "",
    "firmware_version": "",
    "hardware_revision": "",
    "site_id": "",
    "asset_ids": [],
    "is_pdf_read_enabled": False,
    "is_md_read_enabled": False,
    "is_normal_split_enabled": True,
    "is_silicon_flow_api_enabled": True,
    "is_advanced_split_enabled": False,
    "is_vllm_enabled": False,
    "local_dir": "",
    "local_file_path": "",
    "pdf_path": "",
    "md_path": "",
    "file_title": "",
    "split_path": "",
    "embeddings_path": "",
    "md_content": "",
    "chunks": [],
    "item_name": "",
    "embeddings_content": [],
    "mineru_task_id": "",
    "mineru_output_dir": "",
    "mineru_content_list_path": "",
    "mineru_content_list_v2_path": "",
    "mineru_middle_json_path": "",
    "mineru_version": "",
    "mineru_api_protocol_version": ""
}


def create_default_state(**overrides) -> ImportGraphState:
    """
    创建默认状态，支持覆盖

    参数：
        **overrides: 要覆盖的字段（关键字参数解包）

    返回：
        新的状态实例

    示例：
        state = create_default_state(task_id="task_001", local_file_path="doc.pdf")
    """

    # 默认状态
    state = copy.deepcopy(graph_default_state)
    # 用 overrides 覆盖默认值
    state.update(overrides)
    # 返回创建好的状态字典实例
    return state


def get_default_state() -> ImportGraphState:
    """
    返回一个新的状态实例，避免全局变量污染
    """
    return copy.deepcopy(graph_default_state)


if __name__ == "__main__":
    """
    测试
    """
    # 创建默认状态
    state = create_default_state(local_file_path="万用表RS-12的使用.pdf")
    logger.info(state)
