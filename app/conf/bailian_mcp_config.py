"""Deprecated compatibility alias for app.platform.config.bailian_mcp_config."""

import sys

from app.platform.config import bailian_mcp_config as _implementation


sys.modules[__name__] = _implementation
