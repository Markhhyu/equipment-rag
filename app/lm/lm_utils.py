"""Deprecated compatibility alias for app.platform.ai.chat."""

import sys

from app.platform.ai import chat as _implementation


sys.modules[__name__] = _implementation
