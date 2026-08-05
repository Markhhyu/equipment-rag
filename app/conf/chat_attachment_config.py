"""Deprecated compatibility alias for app.platform.config.chat_attachment_config."""

import sys

from app.platform.config import chat_attachment_config as _implementation


sys.modules[__name__] = _implementation
