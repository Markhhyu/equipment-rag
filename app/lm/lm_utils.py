# 环境配置与依赖导入
import os
from typing import Optional

from dotenv import load_dotenv
from langchain_core.exceptions import LangChainException
from langchain_openai import ChatOpenAI

from app.conf.lm_config import lm_config
from app.core.logger import logger


load_dotenv()

# 全局缓存键包含模型、输出模式、流式用量、超时和重试次数，确保不同调用场景不会误用同一个客户端配置。
_llm_client_cache = {}


def get_llm_client(
    model: Optional[str] = None,
    json_mode: bool = False,
    timeout_seconds: Optional[float] = None,
    max_retries: Optional[int] = None,
) -> ChatOpenAI:
    """
    获取带全局缓存的 LangChain ChatOpenAI 客户端。

    该方法统一适配 OpenAI 兼容接口，并允许不同业务场景单独指定请求超时和最大重试次数：
    1. 普通文本问答不传超时参数时保持原有客户端行为；
    2. 图片视觉增强会传入较短超时，防止单张异常图片长时间占用 Worker；
    3. max_retries 为 None 时使用底层客户端默认值，传入 0 时明确关闭客户端内部重试。

    :param model: 模型名称，未传时使用项目默认大模型。
    :param json_mode: 是否要求模型返回标准 JSON 对象。
    :param timeout_seconds: 单次模型请求超时时间，单位为秒。
    :param max_retries: 底层客户端最大重试次数。
    :return: 初始化完成并缓存的 ChatOpenAI 客户端。
    """
    target_model = model or lm_config.llm_model or "qwen3-32b"
    stream_usage = os.getenv("LLM_STREAM_USAGE", "true").lower() == "true"
    normalized_timeout = float(timeout_seconds) if timeout_seconds is not None else None
    normalized_retries = int(max_retries) if max_retries is not None else None
    cache_key = (target_model, json_mode, stream_usage, normalized_timeout, normalized_retries)

    if cache_key in _llm_client_cache:
        logger.debug(
            f"[LLM客户端] 缓存命中：模型={target_model}，JSON模式={json_mode}，"
            f"超时={normalized_timeout}，重试={normalized_retries}"
        )
        return _llm_client_cache[cache_key]

    if not lm_config.api_key:
        raise ValueError("[LLM客户端] 配置缺失：请在.env中配置OPENAI_API_KEY（大模型API密钥）")
    if not lm_config.base_url:
        raise ValueError("[LLM客户端] 配置缺失：请在.env中配置OPENAI_BASE_URL（API接口基础地址）")

    logger.info(
        f"[LLM客户端] 开始初始化：模型={target_model}，JSON模式={json_mode}，"
        f"超时={normalized_timeout}，重试={normalized_retries}"
    )

    # 国产模型兼容接口通常支持 enable_thinking；项目现有模型统一关闭思考输出，减少无关内容和调用耗时。
    extra_body = {"enable_thinking": False}
    model_kwargs = {}
    if json_mode:
        model_kwargs["response_format"] = {"type": "json_object"}
        logger.debug("[LLM客户端] 已开启JSON输出模式")

    client_kwargs = {
        "model": target_model,
        "temperature": lm_config.llm_temperature or 0.1,
        "api_key": lm_config.api_key,
        "base_url": lm_config.base_url,
        "extra_body": extra_body,
        "model_kwargs": model_kwargs,
        "stream_usage": stream_usage,
    }
    if normalized_timeout is not None:
        client_kwargs["timeout"] = normalized_timeout
    if normalized_retries is not None:
        client_kwargs["max_retries"] = normalized_retries

    try:
        llm_client = ChatOpenAI(**client_kwargs)
    except LangChainException as exc:
        raise RuntimeError(f"[LLM客户端] 模型【{target_model}】初始化失败：{exc}") from exc

    _llm_client_cache[cache_key] = llm_client
    logger.info(
        f"[LLM客户端] 初始化成功并缓存：模型={target_model}，JSON模式={json_mode}，"
        f"超时={normalized_timeout}，重试={normalized_retries}"
    )
    return llm_client


if __name__ == "__main__":
    logger.info("===== 开始执行LLM客户端工具测试 =====")
    try:
        client1 = get_llm_client()
        logger.info("默认客户端创建成功")

        client2 = get_llm_client(model="qwen-vl-plus", timeout_seconds=45, max_retries=0)
        client3 = get_llm_client(model="qwen-vl-plus", timeout_seconds=45, max_retries=0)
        logger.info(f"视觉客户端缓存验证结果：{client2 is client3}")

        client4 = get_llm_client(model="qwen3-32b", json_mode=True)
        logger.info(f"JSON客户端创建成功：{client4 is not None}")
    except Exception as exc:
        logger.error(f"LLM客户端工具测试失败：{exc}", exc_info=True)
    finally:
        logger.info("===== LLM客户端工具测试结束 =====")
