from pathlib import Path

ENV_PATH = Path(".env.example")
COMPOSE_PATH = Path("compose.yaml")

ENV_MARKER = """# =============================================================================
# Embedding 向量模型：把文本转换为 Milvus 可检索的向量
# =============================================================================
"""

ENV_BLOCK = """# =============================================================================
# 图片资产化、异步视觉增强与查询阶段视觉推理
# =============================================================================
# 图片处理模式：off 只保存图片；smart 仅增强缺少有效图注的图片；all 增强所有符合条件的图片。
# 本地首次验证建议使用 smart，可保留图片语义，同时避免对已有清晰图注重复调用视觉模型。
IMAGE_PROCESS_MODE=smart
# MongoDB 中保存图片资产、处理状态和视觉说明的集合名称；已有数据后不要随意修改。
IMAGE_ASSET_COLLECTION=document_image_assets

# true：文档导入完成后由后台线程继续生成图片说明，不阻塞文本切片和向量入库。
IMAGE_ENRICHMENT_ASYNC=true
# 单个导入服务进程启动的图片增强线程数；调大可提升吞吐，但会增加模型并发和内存占用。
IMAGE_ENRICHMENT_WORKERS=2
# 没有待处理图片时的轮询间隔，单位秒；过小会增加 MongoDB 空轮询负载。
IMAGE_ENRICHMENT_POLL_SECONDS=3
# Worker 领取图片后的租约时间，单位秒；应大于正常单张处理时间，进程异常后过期任务可重新领取。
IMAGE_ENRICHMENT_LEASE_SECONDS=180

# 单个文档最多进入视觉增强队列的图片数，用于限制大 PDF 的模型费用和处理时长。
IMAGE_CAPTION_MAX_PER_DOCUMENT=30
# 后台生成单张图片说明的模型请求超时，单位秒；网络较慢或图片复杂时可适当增大。
IMAGE_CAPTION_TIMEOUT_SECONDS=45
# 单张图片失败后的最大重试次数；超过后标记 failed，避免异常图片无限调用模型。
IMAGE_CAPTION_MAX_RETRIES=1
# 当前进程内所有图片 Worker 合计每分钟最多发起的视觉请求数，应按模型服务限流调整。
IMAGE_CAPTION_REQUESTS_PER_MINUTE=30

# 小于该字节数的图片通常是图标、Logo 或装饰元素，默认不进入视觉增强。
IMAGE_MIN_BYTES=8192
# 单张图片允许的最大字节数；图片会读入内存并转为 Base64，限制体积可避免异常内存占用。
IMAGE_MAX_BYTES=20971520
# smart 模式下，已有图注或上下文达到该字符数时认为语义足够，不再调用视觉模型。
IMAGE_STRONG_CAPTION_MIN_CHARS=12
# 保存图片前后文的最大字符数；调大可增加语义，也会增加 MongoDB 记录和提示词长度。
IMAGE_CONTEXT_CHARS=240

# 查询阶段是否允许根据图片、界面、按钮位置等问题按需调用视觉模型。
QUERY_IMAGE_VISION_ENABLED=true
# 每次查询最多选择的相关图片数；调大可能提升覆盖，也会增加延迟和图片输入成本。
QUERY_IMAGE_TOP_K=3
# 查询阶段视觉模型请求超时，单位秒；超时后降级使用缓存图片说明，不中断文本回答。
QUERY_IMAGE_VISION_TIMEOUT_SECONDS=45

"""

COMPOSE_MARKER = "  # Embedding 和 Reranker 模型。\n"
COMPOSE_BLOCK = """  # 图片资产化、异步视觉增强和查询阶段视觉推理；值来自根目录 .env。
  IMAGE_PROCESS_MODE: ${IMAGE_PROCESS_MODE:-smart}
  IMAGE_ASSET_COLLECTION: ${IMAGE_ASSET_COLLECTION:-document_image_assets}
  IMAGE_ENRICHMENT_ASYNC: ${IMAGE_ENRICHMENT_ASYNC:-true}
  IMAGE_ENRICHMENT_WORKERS: ${IMAGE_ENRICHMENT_WORKERS:-2}
  IMAGE_ENRICHMENT_POLL_SECONDS: ${IMAGE_ENRICHMENT_POLL_SECONDS:-3}
  IMAGE_ENRICHMENT_LEASE_SECONDS: ${IMAGE_ENRICHMENT_LEASE_SECONDS:-180}
  IMAGE_CAPTION_MAX_PER_DOCUMENT: ${IMAGE_CAPTION_MAX_PER_DOCUMENT:-30}
  IMAGE_CAPTION_TIMEOUT_SECONDS: ${IMAGE_CAPTION_TIMEOUT_SECONDS:-45}
  IMAGE_CAPTION_MAX_RETRIES: ${IMAGE_CAPTION_MAX_RETRIES:-1}
  IMAGE_CAPTION_REQUESTS_PER_MINUTE: ${IMAGE_CAPTION_REQUESTS_PER_MINUTE:-30}
  IMAGE_MIN_BYTES: ${IMAGE_MIN_BYTES:-8192}
  IMAGE_MAX_BYTES: ${IMAGE_MAX_BYTES:-20971520}
  IMAGE_STRONG_CAPTION_MIN_CHARS: ${IMAGE_STRONG_CAPTION_MIN_CHARS:-12}
  IMAGE_CONTEXT_CHARS: ${IMAGE_CONTEXT_CHARS:-240}
  QUERY_IMAGE_VISION_ENABLED: ${QUERY_IMAGE_VISION_ENABLED:-true}
  QUERY_IMAGE_TOP_K: ${QUERY_IMAGE_TOP_K:-3}
  QUERY_IMAGE_VISION_TIMEOUT_SECONDS: ${QUERY_IMAGE_VISION_TIMEOUT_SECONDS:-45}
"""

KEYS = [
    "IMAGE_PROCESS_MODE", "IMAGE_ASSET_COLLECTION", "IMAGE_ENRICHMENT_ASYNC",
    "IMAGE_ENRICHMENT_WORKERS", "IMAGE_ENRICHMENT_POLL_SECONDS", "IMAGE_ENRICHMENT_LEASE_SECONDS",
    "IMAGE_CAPTION_MAX_PER_DOCUMENT", "IMAGE_CAPTION_TIMEOUT_SECONDS", "IMAGE_CAPTION_MAX_RETRIES",
    "IMAGE_CAPTION_REQUESTS_PER_MINUTE", "IMAGE_MIN_BYTES", "IMAGE_MAX_BYTES",
    "IMAGE_STRONG_CAPTION_MIN_CHARS", "IMAGE_CONTEXT_CHARS", "QUERY_IMAGE_VISION_ENABLED",
    "QUERY_IMAGE_TOP_K", "QUERY_IMAGE_VISION_TIMEOUT_SECONDS",
]


def insert_once(content: str, marker: str, block: str, existing_key: str, file_name: str) -> str:
    if existing_key in content:
        return content
    if marker not in content:
        raise RuntimeError(f"{file_name} 未找到配置插入位置")
    return content.replace(marker, block + marker, 1)


def main() -> None:
    env_content = ENV_PATH.read_text(encoding="utf-8-sig")
    compose_content = COMPOSE_PATH.read_text(encoding="utf-8-sig")
    env_content = insert_once(env_content, ENV_MARKER, ENV_BLOCK, "IMAGE_PROCESS_MODE=", str(ENV_PATH))
    compose_content = insert_once(compose_content, COMPOSE_MARKER, COMPOSE_BLOCK, "IMAGE_PROCESS_MODE:", str(COMPOSE_PATH))

    for key in KEYS:
        if env_content.count(f"{key}=") != 1:
            raise RuntimeError(f"{ENV_PATH} 中 {key} 数量不正确")
        if compose_content.count(f"{key}:") != 1:
            raise RuntimeError(f"{COMPOSE_PATH} 中 {key} 数量不正确")

    ENV_PATH.write_text(env_content, encoding="utf-8", newline="\n")
    COMPOSE_PATH.write_text(compose_content, encoding="utf-8", newline="\n")
    print(f"已同步 {len(KEYS)} 个图片处理环境变量")


if __name__ == "__main__":
    main()
