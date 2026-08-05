"""Deprecated compatibility alias for QA session attachments."""

import sys

from app.modules.qa.infrastructure import attachments as _implementation


sys.modules[__name__] = _implementation
