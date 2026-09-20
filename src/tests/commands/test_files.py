"""`ow files` — read-only inspection and diff of the generated files."""

import subprocess
from pathlib import Path

import pytest

from ow.commands.files import cmd_files
from ow.utils import paths
from ow.utils.config import Config, WorkspaceConfig, parse_branch_spec, write_workspace_config


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


def test_files_lists_every_absent_output_and_exits_zero(xdg, tmp_path, capsys):
    config, ws_dir = _workspace(tmp_path)

    cmd_files(config, workspace=str(ws_dir))

    out = capsys.readouterr().out
    assert "mise/conf.d/00-ow.toml" in out
    assert "absent" in out


def test_files_writes_nothing(xdg, tmp_path, tree_snapshot):
    config, ws_dir = _workspace(tmp_path)
    before = tree_snapshot.capture(xdg, ws_dir)

    cmd_files(config, workspace=str(ws_dir))

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg, ws_dir)) == {}


def test_files_diff_writes_nothing(xdg, tmp_path, tree_snapshot):
    config, ws_dir = _workspace(tmp_path)
    before = tree_snapshot.capture(xdg, ws_dir)

    with pytest.raises(SystemExit):
        cmd_files(config, workspace=str(ws_dir), show_diff=True)

    assert tree_snapshot.differences(before, tree_snapshot.capture(xdg, ws_dir)) == {}


def test_files_diff_shows_dev_null_for_an_addition_and_exits_one(xdg, tmp_path, capsys):
    config, ws_dir = _workspace(tmp_path)

    with pytest.raises(SystemExit) as exc:
        cmd_files(config, workspace=str(ws_dir), show_diff=True)

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "/dev/null" in out


def test_files_diff_exits_zero_once_everything_is_up_to_date(xdg, tmp_path, capsys):
    from ow.utils.config import load_workspace_config
    from ow.utils.workspace import refresh_workspace

    config, ws_dir = _workspace(tmp_path)
    ws = load_workspace_config(ws_dir / ".ow" / "config.toml")
    result = refresh_workspace(config, ws, ws_dir, trust=False)
    assert not result.failed

    with pytest.raises(SystemExit) as exc:
        cmd_files(config, workspace=str(ws_dir), show_diff=True)

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "nothing differs" in out


def test_files_reports_a_missing_worktree_and_exits_one(xdg, tmp_path, capsys):
    config, ws_dir = _workspace(tmp_path)
    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(repos={
            "community": parse_branch_spec("master..featA"),
            "enterprise": parse_branch_spec("master..featA"),
        }),
    )

    with pytest.raises(SystemExit) as exc:
        cmd_files(config, workspace=str(ws_dir))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "ow init" in err


def test_files_plain_exits_zero_on_ordinary_yours_difference(xdg, tmp_path, capsys):
    """Plain `files` (no --diff) tolerates YOURS/OUTDATED/ABSENT — only real
    blockers (missing worktree, invalid core, path conflicts, migration
    diagnostics) fail it."""
    config, ws_dir = _workspace(tmp_path)
    (ws_dir / "mise" / "conf.d").mkdir(parents=True)
    (ws_dir / "mise" / "conf.d" / "00-ow.toml").write_text("mine\n")

    cmd_files(config, workspace=str(ws_dir))

    out = capsys.readouterr().out
    assert "yours" in out


def test_files_reports_an_unreadable_schema_1_lock_and_exits_one(xdg, tmp_path, capsys):
    """A schema-1 workspace whose lock cannot be read is a printed blocker,
    never an unhandled OSError."""
    config, ws_dir = _workspace(tmp_path)
    (ws_dir / ".ow" / "config.toml").write_text(
        'version = 1\n\n[repos]\ncommunity = "master..featA"\n'
    )
    lock = ws_dir / ".ow" / "rendered.lock.toml"
    lock.write_bytes(b"")
    lock.chmod(0o000)
    try:
        with pytest.raises(SystemExit) as exc:
            cmd_files(config, workspace=str(ws_dir))
    finally:
        lock.chmod(0o600)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert str(lock) in err
    assert "Traceback" not in err
