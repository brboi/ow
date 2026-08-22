"""Tests for migrate-to-2.0.py."""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("migrate_2", SCRIPTS_DIR / "migrate-to-2.0.py")
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)


# --- Fixtures -----------------------------------------------------------------

def _make_bare_repo(path: Path) -> None:
    """Create a real (empty) bare repo so git commands don't fail."""
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--bare", str(path)], check=True, capture_output=True
    )


def _make_old_project(
    root: Path,
    *,
    workspaces: tuple[str, ...] = ("canary",),
    repos: tuple[str, ...] = ("community.git",),
    with_templates: bool = True,
) -> Path:
    """Create a fake 1.x project layout under *root*."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "ow.toml").write_text(
        'version = 1\n[vars]\nhttp_port = 8069\n'
        '[remotes.community]\norigin.url = "git@github.com:odoo/odoo.git"\n'
    )
    bare_dir = root / ".bare-git-repos"
    bare_dir.mkdir()
    for repo in repos:
        _make_bare_repo(bare_dir / repo)
    ws_dir = root / "workspaces"
    ws_dir.mkdir()
    for name in workspaces:
        ws = ws_dir / name
        ow = ws / ".ow"
        ow.mkdir(parents=True)
        (ow / "config").write_text(
            f'templates = ["common"]\nrepos = {{}}\n'
        )
    if with_templates:
        tmpl = root / "templates" / "common"
        tmpl.mkdir(parents=True)
        (tmpl / "odoorc.j2").write_text("# old template\n")
    return root


@pytest.fixture
def old_project(tmp_path: Path) -> Path:
    return _make_old_project(tmp_path / "old")


@pytest.fixture
def xdg_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect XDG dirs to tmp_path subdirectories."""
    config = tmp_path / "config"
    data = tmp_path / "data"
    state = tmp_path / "state"
    for d in (config, data, state):
        d.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return tmp_path


# --- Helpers ------------------------------------------------------------------

def _config_target() -> Path:
    return migrate._xdg("XDG_CONFIG_HOME", ".config") / "ow" / "config.toml"


def _repos_target() -> Path:
    return migrate._xdg("XDG_DATA_HOME", ".local/share") / "ow" / "repos"


# --- step_config --------------------------------------------------------------

class TestStepConfig:
    def test_copies_when_target_missing(self, old_project: Path, xdg_dirs: Path) -> None:
        target = _config_target()
        migrate.step_config(old_project / "ow.toml", target, execute=True)
        assert target.exists()
        assert target.read_text() == (old_project / "ow.toml").read_text()

    def test_skips_when_target_exists(self, old_project: Path, xdg_dirs: Path) -> None:
        target = _config_target()
        target.parent.mkdir(parents=True)
        target.write_text("# existing\n")
        migrate.step_config(old_project / "ow.toml", target, execute=True)
        assert target.read_text() == "# existing\n"

    def test_dry_run_does_not_copy(self, old_project: Path, xdg_dirs: Path) -> None:
        target = _config_target()
        migrate.step_config(old_project / "ow.toml", target, execute=False)
        assert not target.exists()


# --- step_bare_repos ----------------------------------------------------------

class TestStepBareRepos:
    def test_moves_all_repos(self, old_project: Path, xdg_dirs: Path) -> None:
        target = _repos_target()
        migrate.step_bare_repos(old_project, target, execute=True)
        assert (target / "community.git").is_dir()
        assert not (old_project / ".bare-git-repos" / "community.git").exists()

    def test_skips_existing_at_target(self, old_project: Path, xdg_dirs: Path) -> None:
        target = _repos_target()
        target.mkdir(parents=True)
        _make_bare_repo(target / "community.git")  # pre-existing
        migrate.step_bare_repos(old_project, target, execute=True)
        # Source still there (wasn't moved — dest existed)
        assert (old_project / ".bare-git-repos" / "community.git").exists()

    def test_merges_into_non_empty_target(self, old_project: Path, xdg_dirs: Path) -> None:
        target = _repos_target()
        target.mkdir(parents=True)
        _make_bare_repo(target / "enterprise.git")  # pre-existing
        migrate.step_bare_repos(old_project, target, execute=True)
        assert (target / "community.git").is_dir()
        assert (target / "enterprise.git").is_dir()
        assert not (old_project / ".bare-git-repos" / "community.git").exists()

    def test_dry_run_does_not_move(self, old_project: Path, xdg_dirs: Path) -> None:
        target = _repos_target()
        migrate.step_bare_repos(old_project, target, execute=False)
        assert not target.exists()
        assert (old_project / ".bare-git-repos" / "community.git").exists()

    def test_no_bare_dir_skips(self, old_project: Path, xdg_dirs: Path) -> None:
        shutil.rmtree(old_project / ".bare-git-repos")
        target = _repos_target()
        migrate.step_bare_repos(old_project, target, execute=True)
        assert not target.exists()


# --- step_repair --------------------------------------------------------------

class TestStepRepair:
    def test_repairs_each_repo(self, old_project: Path, xdg_dirs: Path) -> None:
        """Step 2 must run before step 3 for the repos to exist at target."""
        target = _repos_target()
        migrate.step_bare_repos(old_project, target, execute=True)
        migrate.step_repair(target, execute=True)
        # No crash = success (empty bare repos have no worktrees to repair)

    def test_skips_when_no_repos_dir(self, xdg_dirs: Path) -> None:
        migrate.step_repair(_repos_target(), execute=True)
        # No crash

    def test_dry_run_does_not_run_git(self, old_project: Path, xdg_dirs: Path) -> None:
        target = _repos_target()
        migrate.step_bare_repos(old_project, target, execute=True)
        # Dry-run repair should not execute git
        migrate.step_repair(target, execute=False)


# --- step_workspaces ----------------------------------------------------------

class TestStepWorkspaces:
    def test_renames_config(self, old_project: Path, xdg_dirs: Path) -> None:
        workspaces = migrate.step_workspaces(old_project, execute=True)
        assert len(workspaces) == 1
        ws = old_project / "workspaces" / "canary"
        assert (ws / ".ow" / "config.toml").exists()
        assert not (ws / ".ow" / "config").exists()

    def test_skips_already_migrated(self, old_project: Path, xdg_dirs: Path) -> None:
        ws = old_project / "workspaces" / "canary"
        (ws / ".ow" / "config").rename(ws / ".ow" / "config.toml")
        workspaces = migrate.step_workspaces(old_project, execute=True)
        assert len(workspaces) == 0

    def test_dry_run_does_not_rename(self, old_project: Path, xdg_dirs: Path) -> None:
        migrate.step_workspaces(old_project, execute=False)
        ws = old_project / "workspaces" / "canary"
        assert (ws / ".ow" / "config").exists()
        assert not (ws / ".ow" / "config.toml").exists()

    def test_multiple_workspaces(self, tmp_path: Path, xdg_dirs: Path) -> None:
        old = _make_old_project(tmp_path / "old", workspaces=("canary", "parrot"))
        workspaces = migrate.step_workspaces(old, execute=True)
        assert len(workspaces) == 2
        for name in ("canary", "parrot"):
            assert (old / "workspaces" / name / ".ow" / "config.toml").exists()

    def test_no_workspaces_dir(self, old_project: Path, xdg_dirs: Path) -> None:
        shutil.rmtree(old_project / "workspaces")
        workspaces = migrate.step_workspaces(old_project, execute=True)
        assert workspaces == []


# --- step_apply ---------------------------------------------------------------

class TestStepApply:
    def test_skips_when_ow_not_on_path(
        self, old_project: Path, xdg_dirs: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda _: None)
        ws = old_project / "workspaces" / "canary"
        migrate.step_apply([ws], execute=True)
        # No crash

    def test_skips_when_no_workspaces(self, xdg_dirs: Path) -> None:
        migrate.step_apply([], execute=True)
        # No crash


# --- step_templates -----------------------------------------------------------

class TestStepTemplates:
    def test_prints_guidance(self, old_project: Path, xdg_dirs: Path, capsys: pytest.CaptureFixture) -> None:
        migrate.step_templates(old_project)
        out = capsys.readouterr().out
        assert "Templates (manual" in out
        assert "common/odoorc.j2" in out
        assert "ow templates --take common/odoorc.j2" in out

    def test_no_templates_dir(self, old_project: Path, xdg_dirs: Path, capsys: pytest.CaptureFixture) -> None:
        shutil.rmtree(old_project / "templates")
        migrate.step_templates(old_project)
        out = capsys.readouterr().out
        assert "Templates" not in out


# --- Integration (main) -------------------------------------------------------

class TestMainIntegration:
    def test_full_migration(
        self, old_project: Path, xdg_dirs: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["migrate", str(old_project), "--yes"])
        migrate.main()
        # Global config
        assert _config_target().exists()
        # Bare repos moved
        assert (_repos_target() / "community.git").is_dir()
        # Workspace config renamed
        ws_cfg = old_project / "workspaces" / "canary" / ".ow" / "config.toml"
        assert ws_cfg.exists()

    def test_dry_run_changes_nothing(
        self, old_project: Path, xdg_dirs: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["migrate", str(old_project)])
        migrate.main()
        assert not _config_target().exists()
        assert (old_project / ".bare-git-repos" / "community.git").exists()
        assert (old_project / "workspaces" / "canary" / ".ow" / "config").exists()

    def test_idempotent(
        self, old_project: Path, xdg_dirs: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["migrate", str(old_project), "--yes"])
        migrate.main()
        # Second run — should not fail or duplicate
        monkeypatch.setattr(sys, "argv", ["migrate", str(old_project), "--yes"])
        migrate.main()
        # State unchanged
        assert _config_target().exists()
        assert (_repos_target() / "community.git").is_dir()
        ws_cfg = old_project / "workspaces" / "canary" / ".ow" / "config.toml"
        assert ws_cfg.exists()

    def test_missing_ow_toml_errors(
        self, tmp_path: Path, xdg_dirs: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = tmp_path / "empty"
        bad.mkdir()
        monkeypatch.setattr(sys, "argv", ["migrate", str(bad), "--yes"])
        with pytest.raises(SystemExit) as exc_info:
            migrate.main()
        assert exc_info.value.code == 1
