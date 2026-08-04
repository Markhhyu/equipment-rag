import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


def _get_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _get_csv(name: str, default: str) -> frozenset[str]:
    raw = os.getenv(name, default)
    return frozenset(value.strip().lower() for value in raw.split(",") if value.strip())


@dataclass(frozen=True)
class ChatAttachmentConfig:
    """当前会话图片附件配置；附件不会进入文档切片或向量知识库。"""

    max_files: int
    max_bytes: int
    allowed_extensions: frozenset[str]
    allowed_content_types: frozenset[str]


chat_attachment_config = ChatAttachmentConfig(
    max_files=_get_int("CHAT_ATTACHMENT_MAX_FILES", 3),
    max_bytes=_get_int("CHAT_ATTACHMENT_MAX_BYTES", 10 * 1024 * 1024, minimum=1024),
    allowed_extensions=_get_csv("CHAT_ATTACHMENT_ALLOWED_EXTENSIONS", ".jpg,.jpeg,.png,.webp"),
    allowed_content_types=_get_csv("CHAT_ATTACHMENT_ALLOWED_CONTENT_TYPES", "image/jpeg,image/png,image/webp"),
)
