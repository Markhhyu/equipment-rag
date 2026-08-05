"""Deprecated compatibility alias for knowledge image assets."""

import sys

from app.modules.knowledge.infrastructure import image_assets as _implementation


sys.modules[__name__] = _implementation
