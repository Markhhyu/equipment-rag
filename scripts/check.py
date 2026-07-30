"""用一条跨平台命令运行与 CI 一致的本地质量门禁。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run(*command: str) -> None:
    print(f"\n> {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def main() -> int:
    run("uv", "lock", "--check")
    run("uv", "pip", "check")
    run("uv", "run", "--frozen", "ruff", "check", "app", "tests", "scripts")
    run(
        "uv",
        "run",
        "--frozen",
        "ruff",
        "format",
        "--check",
        "app/evaluation",
        "app/runtime",
        "app/security",
        "tests",
        "scripts",
    )
    run("uv", "run", "--frozen", "pytest")
    run(
        "uv",
        "run",
        "--frozen",
        "python",
        "-m",
        "app.evaluation.cli",
        "replay",
        "--predictions",
        "evals/fixtures/smoke_predictions.jsonl",
        "--fail-on-threshold",
    )
    run("uv", "run", "--frozen", "python", "-m", "compileall", "-q", "app")

    if shutil.which("docker"):
        run("docker", "compose", "config", "--quiet")
        run("docker", "compose", "--env-file", ".env.example", "config", "--quiet")
    else:
        print("\n未找到 Docker CLI，已跳过 Compose 配置验证。", flush=True)

    print("\n所有本地检查均已通过。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
