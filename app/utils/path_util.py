"""Deprecated compatibility alias for shared path discovery."""

import sys

from app.shared import paths as _implementation


sys.modules[__name__] = _implementation
