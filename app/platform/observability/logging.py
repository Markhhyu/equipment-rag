"""应用日志配置与跨异步任务的关联上下文。"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv
from loguru import logger as loguru_logger

from app.shared.paths import PROJECT_ROOT


load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


APP_SERVICE_NAME = os.getenv("APP_SERVICE_NAME", "equipment-rag").strip() or "equipment-rag"
APP_ENVIRONMENT = os.getenv("APP_ENVIRONMENT", "development").strip() or "development"

LOG_CONSOLE_ENABLE = _env_bool("LOG_CONSOLE_ENABLE", True)
LOG_CONSOLE_LEVEL = os.getenv("LOG_CONSOLE_LEVEL", "INFO").upper()
LOG_FILE_ENABLE = _env_bool("LOG_FILE_ENABLE", True)
LOG_FILE_LEVEL = os.getenv("LOG_FILE_LEVEL", "INFO").upper()
LOG_FILE_RETENTION = os.getenv("LOG_FILE_RETENTION", "7 days")
LOG_ENQUEUE = _env_bool("LOG_ENQUEUE", False)
LOG_DIAGNOSE = _env_bool("LOG_DIAGNOSE", False)
LOG_CONSOLE_ENCODING = os.getenv("LOG_CONSOLE_ENCODING", "utf-8")

_requested_format = os.getenv("LOG_FORMAT", "text").strip().lower()
LOG_FORMAT = _requested_format if _requested_format in {"json", "text"} else "text"

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding=LOG_CONSOLE_ENCODING, errors="backslashreplace")
    except (AttributeError, LookupError, ValueError):
        pass

LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE_NAME = "app_{time:YYYYMMDD}.log"
LOG_FILE_PATH = LOG_DIR / LOG_FILE_NAME
TEXT_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[service]}</cyan> | "
    "<cyan>{name}:{function}:{line}</cyan> - "
    "<level>{message}</level>"
)

_CONTEXT_KEYS = ("request_id", "trace_id", "tenant_id", "run_id")
_LOG_CONTEXT: ContextVar[dict[str, object]] = ContextVar("equipment_rag_log_context", default={})


def current_log_context() -> dict[str, object]:
    """返回当前任务的日志关联字段副本。"""

    return dict(_LOG_CONTEXT.get())


def bind_log_context(**values: object) -> Token[dict[str, object]]:
    """在当前同步或异步执行上下文中绑定关联字段，并返回可恢复令牌。"""

    context = current_log_context()
    context.update(
        {key: value for key, value in values.items() if key in _CONTEXT_KEYS and value is not None and value != ""}
    )
    return _LOG_CONTEXT.set(context)


def clear_log_context(token: Token[dict[str, object]] | None = None) -> None:
    """恢复绑定前的上下文；没有令牌时清空当前上下文。"""

    if token is None:
        _LOG_CONTEXT.set({})
        return
    _LOG_CONTEXT.reset(token)


@contextmanager
def log_context(**values: object) -> Iterator[None]:
    """临时绑定日志关联字段，退出时保证清理。"""

    token = bind_log_context(**values)
    try:
        yield
    finally:
        clear_log_context(token)


def enrich_log_record(record: dict) -> None:
    """为每条日志补齐服务信息与固定关联字段。"""

    extra = record["extra"]
    for key, value in _LOG_CONTEXT.get().items():
        extra.setdefault(key, value)
    extra.setdefault("service", APP_SERVICE_NAME)
    extra.setdefault("environment", APP_ENVIRONMENT)
    for key in _CONTEXT_KEYS:
        extra.setdefault(key, "")


def init_logger():
    """根据环境变量初始化 Loguru 控制台和可选文件输出。"""

    loguru_logger.remove()
    serialize = LOG_FORMAT == "json"

    if LOG_CONSOLE_ENABLE:
        loguru_logger.add(
            sink=sys.stdout,
            level=LOG_CONSOLE_LEVEL,
            format=TEXT_LOG_FORMAT,
            colorize=not serialize,
            serialize=serialize,
            enqueue=LOG_ENQUEUE,
            backtrace=False,
            diagnose=LOG_DIAGNOSE,
        )

    if LOG_FILE_ENABLE:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        loguru_logger.add(
            sink=LOG_FILE_PATH,
            level=LOG_FILE_LEVEL,
            format=TEXT_LOG_FORMAT,
            serialize=serialize,
            rotation="00:00",
            retention=LOG_FILE_RETENTION,
            encoding="utf-8",
            enqueue=LOG_ENQUEUE,
            backtrace=False,
            diagnose=LOG_DIAGNOSE,
        )

    return loguru_logger


base_logger = init_logger()
logger = base_logger.patch(enrich_log_record)


if __name__ == "__main__":
    with log_context(request_id="local-log-test"):
        logger.info("日志模块测试")
    print(f"日志文件输出路径：{LOG_FILE_PATH}")
