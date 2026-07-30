"""Run the local quality gate with one cross-platform command."""

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
    run("uv", "run", "--frozen", "ruff", "format", "--check", "tests", "scripts")
    run("uv", "run", "--frozen", "pytest")
    run("uv", "run", "--frozen", "python", "-m", "compileall", "-q", "app")

    if shutil.which("docker"):
        run("docker", "compose", "config", "--quiet")
        run("docker", "compose", "--env-file", ".env.example", "config", "--quiet")
    else:
        print("\nDocker CLI not found; skipped Compose validation.", flush=True)

    print("\nAll local checks passed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
