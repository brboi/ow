"""`ow render` — migrate if needed, then write every generated file."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ow.commands.render import cmd_render
from ow.utils import index, paths
from ow.utils.config import (
    Config,
    WorkspaceConfig,
    parse_branch_spec,
    write_workspace_config,
)
from ow.utils.render import RENDERED_LOCK, RenderResult


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _bare_repo(tmp_path: Path, alias: str = "community") -> Path:
    src = tmp_path / "origin" / alias
    src.mkdir(parents=True)
    subprocess.run(["git", "-C", str(src), "init", "-q", "-b", "master"], check=True)
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "T")
    (src / "a.txt").write_text("a")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "A")

    repos = paths.repos_dir()
    repos.mkdir(parents=True, exist_ok=True)
    bare = repos / f"{alias}.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)],
        capture_output=True, text=True, check=True,
    )
    _git(bare, "update-ref", "refs/remotes/origin/master", "refs/heads/master")
    return bare


def _workspace(tmp_path: Path, *, spec: str = "master..featA") -> tuple[Config, Path]:
    """A real workspace: one bare repo, one linked worktree, schema-2 config."""
    bare = _bare_repo(tmp_path)
    ws_dir = tmp_path / "workspaces" / "test"
    worktree = ws_dir / "community"
    ws_dir.mkdir(parents=True)
    _git(bare, "worktree", "add", "-q", str(worktree), "-b", "featA", "master")

    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(repos={"community": parse_branch_spec(spec)}),
    )
    return Config(remotes={}), ws_dir


def test_render_writes_the_generated_files(xdg, tmp_path, capsys):
    config, ws_dir = _workspace(tmp_path)

    with patch("ow.commands.render.require_mise", return_value=(2026, 9, 9)), \
         patch("ow.utils.workspace.trust_fragment") as mock_trust:
        cmd_render(config, workspace=str(ws_dir))

    mock_trust.assert_called_once_with(ws_dir / "mise" / "conf.d" / "00-ow.toml")
    assert (ws_dir / "mise" / "conf.d" / "00-ow.toml").exists()
    assert (ws_dir / ".vscode" / "settings.json").exists()
    out = capsys.readouterr().out
    assert "rendered" in out


def test_render_remembers_the_workspace_on_success(xdg, tmp_path):
    config, ws_dir = _workspace(tmp_path)

    with patch("ow.commands.render.require_mise", return_value=(2026, 9, 9)), \
         patch("ow.utils.workspace.trust_fragment"):
        cmd_render(config, workspace=str(ws_dir))

    assert ws_dir.resolve() in index.known_workspaces()


def test_render_never_creates_a_missing_repo(xdg, tmp_path, capsys):
    """render inspects and writes; it never materializes a worktree."""
    config, ws_dir = _workspace(tmp_path)
    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(repos={
            "community": parse_branch_spec("master..featA"),
            "enterprise": parse_branch_spec("master..featA"),
        }),
    )

    with patch("ow.commands.render.require_mise", return_value=(2026, 9, 9)):
        with pytest.raises(SystemExit) as exc:
            cmd_render(config, workspace=str(ws_dir))

    assert exc.value.code == 1
    assert not (ws_dir / "enterprise").exists()
    err = capsys.readouterr().err
    assert "ow init" in err


def test_render_stops_before_any_write_when_mise_is_too_old(xdg, tmp_path):
    config, ws_dir = _workspace(tmp_path)

    with patch("ow.commands.render.require_mise", side_effect=ValueError("mise 2026.8.13 or newer is required; found 2026.1.1")):
        with pytest.raises(SystemExit) as exc:
            cmd_render(config, workspace=str(ws_dir))

    assert exc.value.code == 1
    assert not (ws_dir / "mise").exists()
    assert not (ws_dir / ".ow" / RENDERED_LOCK.name).exists()


def test_render_migrates_a_schema_1_workspace_then_renders(xdg, tmp_path):
    config, ws_dir = _workspace(tmp_path)
    legacy_source = ws_dir / ".ow" / "config.toml"
    legacy_source.write_text('version = 1\n\n[repos]\ncommunity = "master..featA"\n')

    with patch("ow.commands.render.require_mise", return_value=(2026, 9, 9)), \
         patch("ow.utils.workspace.trust_fragment"):
        cmd_render(config, workspace=str(ws_dir))

    migrated = legacy_source.read_text()
    assert "version = 2" in migrated
    assert (ws_dir / "mise" / "conf.d" / "00-ow.toml").exists()


def test_render_reports_failure_without_a_success_banner_when_trust_fails(xdg, tmp_path, capsys):
    config, ws_dir = _workspace(tmp_path)

    with patch("ow.commands.render.require_mise", return_value=(2026, 9, 9)), \
         patch("ow.utils.workspace.trust_fragment", side_effect=subprocess.CalledProcessError(1, ["mise", "trust"])):
        with pytest.raises(SystemExit) as exc:
            cmd_render(config, workspace=str(ws_dir))

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "not fully rendered" in out
    assert f"'{ws_dir.name}' rendered.\n" not in out
    # The write itself still landed — only trust failed.
    assert (ws_dir / "mise" / "conf.d" / "00-ow.toml").exists()


def test_render_leaves_safe_state_when_the_plan_has_errors(xdg, tmp_path, capsys):
    """A busy git operation blocks the whole plan; render must not half-write."""
    config, ws_dir = _workspace(tmp_path)
    worktree = ws_dir / "community"
    raw = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--git-dir"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    git_dir = Path(raw)
    if not git_dir.is_absolute():
        git_dir = worktree / git_dir
    (git_dir / "MERGE_HEAD").write_text("deadbeef\n")

    with patch("ow.commands.render.require_mise", return_value=(2026, 9, 9)):
        with pytest.raises(SystemExit) as exc:
            cmd_render(config, workspace=str(ws_dir))

    assert exc.value.code == 1
    assert not (ws_dir / "mise").exists()
