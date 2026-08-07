import os
import subprocess
import sys


def _load_base_url(env: dict[str, str]) -> str:
    process_env = os.environ.copy()
    process_env.pop("MINERU_API_BASE_URL", None)
    process_env.pop("MINERU_BASE_URL", None)
    process_env.update(env)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.platform.config.mineru_config import mineru_config; print(mineru_config.base_url)",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=process_env,
    )
    return result.stdout.strip()


def test_mineru_fallback_port_does_not_conflict_with_workflow_api():
    assert _load_base_url({"MINERU_API_BASE_URL": "", "MINERU_BASE_URL": ""}) == (
        "http://127.0.0.1:8003"
    )


def test_mineru_base_url_can_be_overridden():
    assert _load_base_url({"MINERU_API_BASE_URL": "http://mineru.example:9010/"}) == (
        "http://mineru.example:9010"
    )
