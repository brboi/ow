"""`ow archive` / `ow unarchive` — the round trip, against real git.

The property that matters is that a workspace survives the round trip with
working worktrees and its branches intact. Only real bare repos can show
that, so nothing here is mocked but the confirmation prompt and mise.
"""

import subprocess
from pathlib import Path

import pytest

from ow.commands.archive import (
    archived_workspaces,
    cmd_archive,
    cmd_unarchive,
)
from ow.commands.ls import cmd_ls
from ow.utils import index, paths
from ow.utils.config import (
    BranchSpec,
    Config,
    WorkspaceConfig,
    load_workspace_config,
    write_workspace_config,
)

MARKER = Path(".ow") / "config.toml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _bare_repo(tmp_path: Path, alias: str = "community") -> Path:
    src = tmp_path / "origin" / alias
    src.mkdir(parents=True)
    subprocess.run(["git", "-C", str(src), "init", "-q", "-b", "master"], check=True)
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "T")
    (src / "a.txt").write_text("a")
    # An Odoo core checkout: the round trip is checked through `odoorc`, and
    # ow only renders it for a workspace that has an Odoo to run.
    (src / "odoo-bin").write_text("#!/usr/bin/env python3\n")
    (src / "addons" / "sale").mkdir(parents=True)
    (src / "addons" / "sale" / "__manifest__.py").write_text("{}\n")
    (src / "odoo" / "addons" / "base").mkdir(parents=True)
    (src / "odoo" / "addons" / "base" / "__manifest__.py").write_text("{}\n")
    (src / "odoo" / "release.py").write_text(
        "version_info = (19, 0, 0, 'final', 0, '')\n"
        "MIN_PY_VERSION = (3, 10)\nMAX_PY_VERSION = (3, 14)\n"
    )
    (src / "odoo" / "tools").mkdir(parents=True)
    (src / "odoo" / "tools" / "config.py").write_text(
        "PARSER.add_argument('--with-demo', action='store_true')\n"
        "PARSER.add_argument('--without-demo', action='store_true')\n"
    )
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "A")

    repos = paths.repos_dir()
    repos.mkdir(parents=True, exist_ok=True)
    bare = repos / f"{alias}.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(src), str(bare)],
        capture_output=True, text=True, check=True,
    )
    _git(bare, "config", "user.email", "t@t")
    _git(bare, "config", "user.name", "T")
    _git(bare, "update-ref", "refs/remotes/origin/master", "refs/heads/master")
    return bare


def _branches(bare: Path) -> list[str]:
    out = _git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return out.splitlines() if out else []


def _registered_worktrees(bare: Path) -> list[str]:
    out = _git(bare, "worktree", "list", "--porcelain")
    return [
        line.split(" ", 1)[1]
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def _make_workspace(tmp_path: Path, name: str, bare: Path) -> Path:
    ws = tmp_path / "workspaces" / name
    ws.mkdir(parents=True)
    write_workspace_config(
        ws / MARKER,
        WorkspaceConfig(repos={"community": BranchSpec("origin/master", f"master-{name}")}),
    )
    _git(bare, "worktree", "add", "-b", f"master-{name}", str(ws / "community"), "origin/master")
    index.remember(ws)
    return ws


def _answer(monkeypatch, reply: str):
    monkeypatch.setattr("builtins.input", lambda prompt="": reply)


def _mise_ok(monkeypatch):
    monkeypatch.setattr("ow.utils.relocate.require_mise", lambda: (2026, 9, 9))


@pytest.fixture
def config(xdg) -> Config:
    return Config(remotes={})


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------

def test_archive_moves_the_workspace_and_drops_the_index_entry(tmp_path, monkeypatch, xdg):
    bare = _bare_repo(tmp_path)
    ws = _make_workspace(tmp_path, "parrot", bare)
    _answer(monkeypatch, "y")

    cmd_archive("parrot")

    archived = paths.archives_dir() / "parrot"
    assert not ws.exists()
    assert (archived / MARKER).exists()
    # Registrations follow the files.
    assert str(archived / "community") in _registered_worktrees(bare)
    # The branch is kept — that is what makes unarchive a plain move back.
    assert "master-parrot" in _branches(bare)
    # And the workspace is no longer active.
    assert index.known_workspaces() == []


def test_archive_declining_the_prompt_changes_nothing(tmp_path, monkeypatch, xdg):
    bare = _bare_repo(tmp_path)
    ws = _make_workspace(tmp_path, "parrot", bare)
    _answer(monkeypatch, "n")

    with pytest.raises(SystemExit) as exc:
        cmd_archive("parrot")

    assert exc.value.code == 2
    assert ws.exists()
    assert index.known_workspaces() == [ws]


def test_archive_rejects_an_unknown_name(tmp_path, capsys, xdg):
    with pytest.raises(SystemExit) as exc:
        cmd_archive("nope")

    assert exc.value.code == 1
    assert "ow ls" in capsys.readouterr().err


def test_archive_refuses_when_the_archive_slot_is_taken(tmp_path, monkeypatch, capsys, xdg):
    bare = _bare_repo(tmp_path)
    _make_workspace(tmp_path, "parrot", bare)
    (paths.archives_dir() / "parrot").mkdir(parents=True)

    with pytest.raises(SystemExit) as exc:
        cmd_archive("parrot")

    assert exc.value.code == 1
    assert "already exists" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# unarchive
# ---------------------------------------------------------------------------

def test_the_round_trip_restores_a_working_workspace(tmp_path, monkeypatch, config):
    bare = _bare_repo(tmp_path)
    _make_workspace(tmp_path, "parrot", bare)
    _answer(monkeypatch, "y")
    _mise_ok(monkeypatch)

    cmd_archive("parrot")
    back = tmp_path / "restored"
    back.mkdir()
    cmd_unarchive(config, "parrot", str(back))

    target = back / "parrot"
    assert (target / MARKER).exists()
    # The worktree works from the restored path.
    assert _git(target / "community", "rev-parse", "--abbrev-ref", "HEAD") == "master-parrot"
    assert str(target / "community") in _registered_worktrees(bare)
    # Active again, and odoorc names where it landed.
    assert index.known_workspaces() == [target]
    assert str(target) in (target / "odoorc").read_text()
    # The archive slot is free for the next time.
    assert not (paths.archives_dir() / "parrot").exists()


def test_unarchive_defaults_to_the_current_directory(tmp_path, monkeypatch, config):
    bare = _bare_repo(tmp_path)
    _make_workspace(tmp_path, "parrot", bare)
    _answer(monkeypatch, "y")
    _mise_ok(monkeypatch)
    cmd_archive("parrot")

    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    cmd_unarchive(config, "parrot")

    assert (here / "parrot" / MARKER).exists()


def test_unarchive_rejects_an_unknown_name(tmp_path, capsys, config):
    with pytest.raises(SystemExit) as exc:
        cmd_unarchive(config, "nope")

    assert exc.value.code == 1
    assert "ow ls --archived" in capsys.readouterr().err


def test_unarchive_refuses_an_occupied_destination(tmp_path, monkeypatch, capsys, config):
    bare = _bare_repo(tmp_path)
    _make_workspace(tmp_path, "parrot", bare)
    _answer(monkeypatch, "y")
    cmd_archive("parrot")

    taken = tmp_path / "taken"
    (taken / "parrot").mkdir(parents=True)

    with pytest.raises(SystemExit) as exc:
        cmd_unarchive(config, "parrot", str(taken))

    assert exc.value.code == 1
    assert "already exists" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Legacy (schema-1) archives — a round trip must never be the thing that
# migrates a manifest; only `ow render` does that.
# ---------------------------------------------------------------------------

LEGACY_WS_WITH_UNKNOWN_KEY = (
    'mystery = "unrecognized-value"\n'
    "\n"
    "[repos]\n"
    'community = "master..master-parrot"\n'
)


def test_archive_and_unarchive_preserve_a_legacy_workspace_byte_for_byte(
    tmp_path, monkeypatch, config,
):
    """Unknown keys, and the manifest's own schema, survive archive and
    unarchive untouched — restoring a legacy archive must not migrate it."""
    bare = _bare_repo(tmp_path)
    ws = tmp_path / "workspaces" / "parrot"
    (ws / ".ow").mkdir(parents=True)
    (ws / MARKER).write_text(LEGACY_WS_WITH_UNKNOWN_KEY)
    lock_bytes = b'# Managed by ow.\nodoorc = "deadbeef"\n'
    (ws / ".ow" / "rendered.lock.toml").write_bytes(lock_bytes)
    _git(bare, "worktree", "add", "-b", "master-parrot", str(ws / "community"), "origin/master")
    index.remember(ws)

    _answer(monkeypatch, "y")
    cmd_archive("parrot")

    archived = paths.archives_dir() / "parrot"
    assert (archived / MARKER).read_text() == LEGACY_WS_WITH_UNKNOWN_KEY
    assert (archived / ".ow" / "rendered.lock.toml").read_bytes() == lock_bytes

    back = tmp_path / "restored"
    back.mkdir()
    cmd_unarchive(config, "parrot", str(back))

    target = back / "parrot"
    assert (target / MARKER).read_text() == LEGACY_WS_WITH_UNKNOWN_KEY
    assert (target / ".ow" / "rendered.lock.toml").read_bytes() == lock_bytes
    assert not (target / "odoorc").exists()

    reloaded = load_workspace_config(target / MARKER)
    assert reloaded.version == 1
    assert reloaded.legacy is not None
    assert reloaded.legacy.issues  # the unknown key is recorded, never fatal


def test_unarchive_of_a_legacy_workspace_points_at_ow_render(tmp_path, monkeypatch, capsys, config):
    bare = _bare_repo(tmp_path)
    ws = tmp_path / "workspaces" / "parrot"
    (ws / ".ow").mkdir(parents=True)
    (ws / MARKER).write_text(LEGACY_WS_WITH_UNKNOWN_KEY)
    _git(bare, "worktree", "add", "-b", "master-parrot", str(ws / "community"), "origin/master")
    index.remember(ws)

    _answer(monkeypatch, "y")
    cmd_archive("parrot")

    back = tmp_path / "restored"
    back.mkdir()
    capsys.readouterr()
    cmd_unarchive(config, "parrot", str(back))

    target = back / "parrot"
    out = capsys.readouterr().out
    assert f"ow render -w {target}" in out


# ---------------------------------------------------------------------------
# ow ls --archived
# ---------------------------------------------------------------------------

def test_archived_workspaces_lists_only_real_ones(tmp_path, xdg):
    root = paths.archives_dir()
    (root / "parrot" / ".ow").mkdir(parents=True)
    (root / "parrot" / MARKER).write_text('version = 1\ntemplates = []\n')
    (root / "not-a-workspace").mkdir()

    assert archived_workspaces() == [root / "parrot"]


def test_archived_workspaces_is_empty_without_an_archive_dir(xdg):
    assert archived_workspaces() == []


def test_ls_archived_shows_the_archive(tmp_path, monkeypatch, capsys, xdg):
    bare = _bare_repo(tmp_path)
    _make_workspace(tmp_path, "parrot", bare)
    _answer(monkeypatch, "y")
    cmd_archive("parrot")
    capsys.readouterr()

    cmd_ls(archived=True)
    archived_out = capsys.readouterr().out

    cmd_ls()
    active_out = capsys.readouterr().out

    assert "parrot" in archived_out
    assert "community:master..master-parrot" in archived_out
    assert "No known workspaces" in active_out


def test_ls_archived_says_so_when_empty(xdg, capsys):
    cmd_ls(archived=True)
    assert "No archived workspaces." in capsys.readouterr().out
