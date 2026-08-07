import io
import json

from app.platform.observability import logging as app_logging


def _capture_json_log(emit):
    output = io.StringIO()
    sink_id = app_logging.logger.add(output, serialize=True, diagnose=False)
    try:
        emit()
    finally:
        app_logging.logger.remove(sink_id)
    return json.loads(output.getvalue())


def test_json_log_contains_service_and_correlation_fields():
    def emit():
        with app_logging.log_context(request_id="request-123", tenant_id="factory-a"):
            app_logging.logger.bind(trace_id="trace-456", run_id="run-789").info("completed")

    payload = _capture_json_log(emit)
    record = payload["record"]
    extra = record["extra"]

    assert record["message"] == "completed"
    assert record["level"]["name"] == "INFO"
    assert extra["service"] == app_logging.APP_SERVICE_NAME
    assert extra["environment"] == app_logging.APP_ENVIRONMENT
    assert extra["request_id"] == "request-123"
    assert extra["tenant_id"] == "factory-a"
    assert extra["trace_id"] == "trace-456"
    assert extra["run_id"] == "run-789"
    assert app_logging.current_log_context() == {}


def test_nested_log_context_restores_previous_values():
    outer_token = app_logging.bind_log_context(request_id="outer")
    try:
        with app_logging.log_context(request_id="inner", run_id="run-1"):
            assert app_logging.current_log_context() == {"request_id": "inner", "run_id": "run-1"}
        assert app_logging.current_log_context() == {"request_id": "outer"}
    finally:
        app_logging.clear_log_context(outer_token)

    assert app_logging.current_log_context() == {}


def test_exception_log_does_not_include_local_secret():
    def emit():
        local_password = "must-not-appear-in-log"
        try:
            raise RuntimeError("expected failure")
        except RuntimeError:
            app_logging.logger.exception("operation failed")
        assert local_password

    payload = _capture_json_log(emit)

    assert payload["record"]["exception"]["type"] == "RuntimeError"
    assert "must-not-appear-in-log" not in json.dumps(payload, ensure_ascii=False)


def test_text_format_remains_available_for_local_development():
    output = io.StringIO()
    sink_id = app_logging.logger.add(output, format=app_logging.TEXT_LOG_FORMAT, colorize=False)
    try:
        app_logging.logger.info("readable text")
    finally:
        app_logging.logger.remove(sink_id)

    rendered = output.getvalue()
    assert "readable text" in rendered
    assert app_logging.APP_SERVICE_NAME in rendered
