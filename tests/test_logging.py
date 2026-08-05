from types import SimpleNamespace

import app.platform.observability.logging as app_logging


def test_log_position_skips_current_logging_module(monkeypatch):
    monkeypatch.setattr(
        app_logging.inspect,
        "stack",
        lambda: [
            SimpleNamespace(filename=str(app_logging._LOGGING_MODULE_PATH), function="fix_log_position", lineno=100),
            SimpleNamespace(filename=r"D:\project\business_service.py", function="handle", lineno=42),
        ],
    )
    record = {}

    app_logging.fix_log_position(record)

    assert record == {"name": "business_service.py", "function": "handle", "line": 42}
