from pathlib import Path

from app.shared.paths import PROJECT_ROOT, get_project_root


def test_project_root_uses_repository_marker_without_dotenv():
    assert PROJECT_ROOT == get_project_root()
    assert (PROJECT_ROOT / "pyproject.toml").is_file()


def test_project_root_environment_override(monkeypatch):
    override = Path(__file__).parent
    monkeypatch.setenv("PROJECT_ROOT", str(override))

    assert get_project_root() == override.absolute()
