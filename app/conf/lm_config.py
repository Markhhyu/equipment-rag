"""Deprecated compatibility alias for app.platform.config.lm_config."""

import sys

from app.platform.config import lm_config as _implementation


sys.modules[__name__] = _implementation
