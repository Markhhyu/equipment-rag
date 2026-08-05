"""Deprecated compatibility alias for platform prompt loading."""

import sys

from app.platform.ai import prompts as _implementation


sys.modules[__name__] = _implementation
