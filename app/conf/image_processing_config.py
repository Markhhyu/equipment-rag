"""Deprecated compatibility alias for app.platform.config.image_processing_config."""

import sys

from app.platform.config import image_processing_config as _implementation


sys.modules[__name__] = _implementation
