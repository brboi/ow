"""Standalone tests for migrate-to-1.0.0.py."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add scripts/ to path so we can import the migration script standalone
SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import importlib.util

_spec = importlib.util.spec_from_file_location("migrate", SCRIPTS_DIR / "migrate-to-1.0.0.py")
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)


def _write_toml(path: Path, content: str) -> None:
    path.write_text(content)


def _read_toml(path: Path) -> str:
    return path.read_text()


# ---------------------------------------------------------------------------
# merge_repos
# ---------------------------------------------------------------------------

def test_merge_repos_basic():
    """Merge global + workspace-specific repos."""
    global_repos = {"community": "https://github.com/odoo/odoo.git"}
    ws_repos = {"enterprise": "https://github.com/odoo/enterprise.git"}
    result = migrate.merge_repos(global_repos, ws_repos)
    assert result == {
        "community": "https://github.com/odoo/odoo.git",
        "enterprise": "https://github.com/odoo/enterprise.git",
    }


def test_merge_repos_ws_overrides_global():
    """Workspace-specific repo URL overrides global default."""
    global_repos = {"community": "https://github.com/odoo/odoo.git"}
    ws_repos = {"community": "https://github.com/myfork/odoo.git"}
    result = migrate.merge_repos(global_repos, ws_repos)
    assert result == {"community": "https://github.com/myfork/odoo.git"}


# ---------------------------------------------------------------------------
# backup_toml
# ---------------------------------------------------------------------------

def test_backup_toml_creates_bak(tmp_path: Path):
    """Creates ow.toml.bak on first backup."""
    toml = tmp_path / "ow.toml"
    toml.write_text("[vars]\nfoo = 'bar'\n")
    backup_path = migrate.backup_toml(toml)
    assert backup_path == tmp_path / "ow.toml.bak"
    assert backup_path.exists()
    assert backup_path.read_text() == "[vars]\nfoo = 'bar'\n"


def test_backup_toml_incremental(tmp_path: Path):
    """If .bak exists, creates .bak.1, .bak.2, etc."""
    toml = tmp_path / "ow.toml"
    toml.write_text("[vars]\n")

    # First backup → .bak
    b1 = migrate.backup_toml(toml)
    assert b1 == tmp_path / "ow.toml.bak"

    # Second backup → .bak.1
    b2 = migrate.backup_toml(toml)
    assert b2 == tmp_path / "ow.toml.bak.1"

    # Third backup → .bak.2
    b3 = migrate.backup_toml(toml)
    assert b3 == tmp_path / "ow.toml.bak.2"


# ---------------------------------------------------------------------------
# write_workspace_config
# ---------------------------------------------------------------------------

def test_write_workspace_config_creates_file(tmp_path: Path):
    """Creates .ow/config with correct content."""
    ws_dir = tmp_path / "workspaces" / "myws"
    ws_dir.mkdir(parents=True)

    migrate.write_workspace_config(
        ws_dir, "myws",
        templates=["common", "vscode"],
        repos={"community": "https://example.com/odoo.git"},
        vars={"odoo_version": "17.0"},
    )

    config = ws_dir / ".ow" / "config"
    assert config.exists()

    import tomllib
    with open(config, "rb") as f:
        data = tomllib.load(f)
    assert data["templates"] == ["common", "vscode"]
    assert data["repos"] == {"community": "https://example.com/odoo.git"}
    assert data["vars"] == {"odoo_version": "17.0"}


def test_write_workspace_config_skips_existing(tmp_path: Path, capsys):
    """Does not rewrite .ow/config if it already exists."""
    ws_dir = tmp_path / "workspaces" / "myws"
    ws_dir.mkdir(parents=True)
    (ws_dir / ".ow").mkdir()
    existing = ws_dir / ".ow" / "config"
    existing.write_text("# existing\n")

    migrate.write_workspace_config(
        ws_dir, "myws",
        templates=["common"],
        repos={},
        vars={},
    )

    assert _read_toml(existing) == "# existing\n"
    captured = capsys.readouterr()
    assert "Skipping" in captured.out


# ---------------------------------------------------------------------------
# remove_workspace_sections
# ---------------------------------------------------------------------------

def test_remove_workspace_sections_removes_all(tmp_path: Path):
    """Removes all [[workspace]] sections."""
    toml = tmp_path / "ow.toml"
    _write_toml(toml, """\
[vars]
odoo_version = "17.0"

[[workspace]]
name = "ws1"

[[workspace]]
name = "ws2"
""")
    migrate.remove_workspace_sections(toml)
    content = _read_toml(toml)
    assert "[[workspace]]" not in content
    assert "ws1" not in content
    assert "ws2" not in content


def test_remove_workspace_sections_preserves_vars(tmp_path: Path):
    """Keeps [vars] and [remotes] sections."""
    toml = tmp_path / "ow.toml"
    _write_toml(toml, """\
[vars]
odoo_version = "17.0"

[remotes.origin]
url = "https://github.com/odoo/odoo.git"

[[workspace]]
name = "ws1"
""")
    migrate.remove_workspace_sections(toml)
    content = _read_toml(toml)
    assert "[vars]" in content
    assert "odoo_version" in content
    assert "[remotes.origin]" in content
    assert "[[workspace]]" not in content


def test_remove_workspace_sections_preserves_comments(tmp_path: Path):
    """Keeps comments that are outside workspace sections."""
    toml = tmp_path / "ow.toml"
    _write_toml(toml, """\
# Global variables
[vars]
odoo_version = "17.0"

[[workspace]]
name = "ws1"
# This comment is inside workspace — will be removed
repo.community = "https://example.com"

# Remotes section
[remotes.origin]
url = "https://github.com/odoo/odoo.git"

# Trailing comment
""")
    migrate.remove_workspace_sections(toml)
    content = _read_toml(toml)
    assert "# Global variables" in content
    assert "# Trailing comment" in content
    assert "[[workspace]]" not in content
    assert "[remotes.origin]" in content
    # Comments inside workspace blocks are removed along with the block
    assert "# This comment is inside workspace" not in content


# ---------------------------------------------------------------------------
# main() integration tests
# ---------------------------------------------------------------------------

def _setup_ow_toml(root: Path, content: str) -> None:
    (root / "ow.toml").write_text(content)


def _setup_workspace(root: Path, name: str) -> Path:
    ws_dir = root / "workspaces" / name
    ws_dir.mkdir(parents=True, exist_ok=True)
    return ws_dir


def test_main_no_workspace_sections(tmp_path: Path, capsys, monkeypatch):
    """Prints 'Nothing to migrate' and exits cleanly."""
    _setup_ow_toml(tmp_path, "[vars]\nodoo_version = '17.0'\n")
    _setup_workspace(tmp_path, "dummy")

    monkeypatch.chdir(tmp_path)
    migrate.main()

    captured = capsys.readouterr()
    assert "Nothing to migrate" in captured.out


def test_main_missing_ow_toml(tmp_path: Path, monkeypatch):
    """Exits with error when ow.toml is missing."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        migrate.main()
    assert exc.value.code == 1


def test_main_missing_workspaces_dir(tmp_path: Path, monkeypatch):
    """Exits with error when workspaces/ directory is missing."""
    _setup_ow_toml(tmp_path, "[vars]\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        migrate.main()
    assert exc.value.code == 1


def test_main_migrates_single_workspace(tmp_path: Path, capsys, monkeypatch):
    """Full migration of a single workspace."""
    _setup_ow_toml(tmp_path, """\
[vars]
odoo_version = "17.0"

[repo]
community = "https://github.com/odoo/odoo.git"

[[workspace]]
name = "my-project"
templates = ["common", "vscode"]
repo.community = "https://github.com/odoo/odoo.git"
""")
    _setup_workspace(tmp_path, "my-project")

    monkeypatch.chdir(tmp_path)
    migrate.main()

    config = tmp_path / "workspaces" / "my-project" / ".ow" / "config"
    assert config.exists()

    import tomllib
    with open(config, "rb") as f:
        data = tomllib.load(f)
    assert data["templates"] == ["common", "vscode"]
    assert data["repos"]["community"] == "https://github.com/odoo/odoo.git"

    # ow.toml should no longer contain [[workspace]]
    toml_content = _read_toml(tmp_path / "ow.toml")
    assert "[[workspace]]" not in toml_content


def test_main_migrates_multiple_workspaces(tmp_path: Path, capsys, monkeypatch):
    """Migration of multiple workspaces."""
    _setup_ow_toml(tmp_path, """\
[vars]
odoo_version = "17.0"

[repo]
community = "https://github.com/odoo/odoo.git"

[[workspace]]
name = "project-a"
templates = ["common"]

[[workspace]]
name = "project-b"
templates = ["common", "vscode"]
""")
    _setup_workspace(tmp_path, "project-a")
    _setup_workspace(tmp_path, "project-b")

    monkeypatch.chdir(tmp_path)
    migrate.main()

    config_a = tmp_path / "workspaces" / "project-a" / ".ow" / "config"
    config_b = tmp_path / "workspaces" / "project-b" / ".ow" / "config"
    assert config_a.exists()
    assert config_b.exists()

    import tomllib
    with open(config_a, "rb") as f:
        data_a = tomllib.load(f)
    with open(config_b, "rb") as f:
        data_b = tomllib.load(f)

    assert data_a["templates"] == ["common"]
    assert data_b["templates"] == ["common", "vscode"]


def test_main_preserves_workspace_vars(tmp_path: Path, capsys, monkeypatch):
    """Vars from workspace are migrated into .ow/config."""
    _setup_ow_toml(tmp_path, """\
[vars]
odoo_version = "17.0"

[repo]
community = "https://github.com/odoo/odoo.git"

[[workspace]]
name = "my-project"
templates = ["common"]

[workspace.vars]
custom_key = "custom_value"
odoo_version = "16.0"
""")
    _setup_workspace(tmp_path, "my-project")

    monkeypatch.chdir(tmp_path)
    migrate.main()

    config = tmp_path / "workspaces" / "my-project" / ".ow" / "config"
    import tomllib
    with open(config, "rb") as f:
        data = tomllib.load(f)
    assert "vars" in data
    assert data["vars"]["custom_key"] == "custom_value"
    assert data["vars"]["odoo_version"] == "16.0"


def test_main_global_repo_defaults(tmp_path: Path, capsys, monkeypatch):
    """Global [repo] entries are merged into workspace configs."""
    _setup_ow_toml(tmp_path, """\
[repo]
community = "https://github.com/odoo/odoo.git"
enterprise = "https://github.com/odoo/enterprise.git"

[[workspace]]
name = "my-project"
templates = ["common"]

[workspace.repo]
community = "https://github.com/myfork/odoo.git"
""")
    _setup_workspace(tmp_path, "my-project")

    monkeypatch.chdir(tmp_path)
    migrate.main()

    config = tmp_path / "workspaces" / "my-project" / ".ow" / "config"
    import tomllib
    with open(config, "rb") as f:
        data = tomllib.load(f)
    assert data["repos"]["community"] == "https://github.com/myfork/odoo.git"
    assert data["repos"]["enterprise"] == "https://github.com/odoo/enterprise.git"
