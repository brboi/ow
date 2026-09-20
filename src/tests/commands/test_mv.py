"""`ow mv` — end to end, against real bare repos and real worktrees.

Mocks cannot represent the thing that actually breaks when a workspace
moves: the bare repo's `worktrees/<id>/gitdir` pointer. Every test here
drives real git.
"""

import configparser
import subprocess
from pathlib import Path

import pytest

from ow.commands.mv import cmd_mv, _resolve_dest
from ow.utils import index, paths
from ow.utils.config import (
    BranchSpec,
    Config,
    WorkspaceConfig,
    load_workspace_config,
    write_workspace_config,
)
from ow.utils.options import MiseOverrides, OdooOverrides
from ow.utils.resolver import resolve_workspace
from ow.utils.workspace import refresh_workspace

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
    # An Odoo core checkout: `odoorc` is this test file's witness that the
    # absolute paths were rewritten, and ow only renders it for a workspace
    # that has an Odoo to run.
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


def _registered_worktrees(bare: Path) -> list[str]:
    out = _git(bare, "worktree", "list", "--porcelain")
    return [
        line.split(" ", 1)[1]
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def _make_workspace(
    tmp_path: Path, name: str, aliases: dict[str, Path], **ws_kwargs,
) -> Path:
    """A workspace with real worktrees, a config, and an index entry."""
    ws = tmp_path / "workspaces" / name
    ws.mkdir(parents=True)
    repos = {alias: BranchSpec("origin/master", f"master-{name}") for alias in aliases}
    write_workspace_config(ws / MARKER, WorkspaceConfig(repos=repos, **ws_kwargs))
    for alias, bare in aliases.items():
        _git(bare, "worktree", "add", "-b", f"master-{name}", str(ws / alias), "origin/master")
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
# Destination resolution — mv(1) semantics
# ---------------------------------------------------------------------------

def test_an_existing_directory_means_move_into_it(tmp_path):
    ws = tmp_path / "workspaces" / "parrot"
    ws.mkdir(parents=True)
    into = tmp_path / "elsewhere"
    into.mkdir()

    assert _resolve_dest(ws, str(into)) == into / "parrot"


def test_a_nonexistent_path_is_the_new_path(tmp_path):
    ws = tmp_path / "workspaces" / "parrot"
    ws.mkdir(parents=True)
    target = tmp_path / "elsewhere" / "renamed"
    target.parent.mkdir()

    assert _resolve_dest(ws, str(target)) == target


# ---------------------------------------------------------------------------
# The move itself
# ---------------------------------------------------------------------------

def test_mv_relocates_worktrees_index_and_odoorc(tmp_path, monkeypatch, config):
    bare = _bare_repo(tmp_path)
    ws = _make_workspace(tmp_path, "parrot", {"community": bare})
    target = tmp_path / "moved"
    _answer(monkeypatch, "y")
    _mise_ok(monkeypatch)

    cmd_mv(config, source=str(ws), dest=str(target))

    # The directory moved.
    assert not ws.exists()
    assert (target / MARKER).exists()

    # The worktree still works from its new path.
    assert _git(target / "community", "rev-parse", "--abbrev-ref", "HEAD") == "master-parrot"

    # The bare repo knows where it went.
    assert str(target / "community") in _registered_worktrees(bare)

    # The index names only the new path.
    assert index.known_workspaces() == [target]

    # odoorc's absolute data_dir was regenerated for the new location.
    odoorc = (target / "odoorc").read_text()
    assert str(target) in odoorc
    assert str(ws) not in odoorc


def test_mv_declining_the_prompt_changes_nothing(tmp_path, monkeypatch, config):
    bare = _bare_repo(tmp_path)
    ws = _make_workspace(tmp_path, "parrot", {"community": bare})
    target = tmp_path / "moved"
    _answer(monkeypatch, "n")

    with pytest.raises(SystemExit) as exc:
        cmd_mv(config, source=str(ws), dest=str(target))

    assert exc.value.code == 2
    assert ws.exists()
    assert not target.exists()
    assert index.known_workspaces() == [ws]


def test_mv_refuses_an_existing_target(tmp_path, monkeypatch, capsys, config):
    bare = _bare_repo(tmp_path)
    ws = _make_workspace(tmp_path, "parrot", {"community": bare})
    # `taken` is a directory, so mv(1) semantics resolve to taken/parrot —
    # which is already occupied.
    (tmp_path / "taken" / "parrot").mkdir(parents=True)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": pytest.fail("must not prompt after a validation error"),
    )

    with pytest.raises(SystemExit) as exc:
        cmd_mv(config, source=str(ws), dest=str(tmp_path / "taken"))

    assert exc.value.code == 1
    assert "already exists" in capsys.readouterr().err
    assert ws.exists()


def test_mv_refuses_to_move_a_workspace_into_itself(tmp_path, monkeypatch, capsys, config):
    bare = _bare_repo(tmp_path)
    ws = _make_workspace(tmp_path, "parrot", {"community": bare})

    with pytest.raises(SystemExit) as exc:
        cmd_mv(config, source=str(ws), dest=str(ws / "inner"))

    assert exc.value.code == 1
    assert "is inside" in capsys.readouterr().err


def test_mv_accepts_a_workspace_name(tmp_path, monkeypatch, config):
    """The index makes `ow mv parrot ...` work from anywhere."""
    bare = _bare_repo(tmp_path)
    ws = _make_workspace(tmp_path, "parrot", {"community": bare})
    target = tmp_path / "moved"
    _answer(monkeypatch, "y")
    _mise_ok(monkeypatch)

    cmd_mv(config, source="parrot", dest=str(target))

    assert (target / MARKER).exists()
    assert index.known_workspaces() == [target]


def test_mv_warns_that_a_rename_changes_the_database(tmp_path, monkeypatch, capsys, config):
    bare = _bare_repo(tmp_path)
    ws = _make_workspace(tmp_path, "parrot", {"community": bare})
    target = tmp_path / "quattromori"
    _answer(monkeypatch, "y")
    _mise_ok(monkeypatch)

    cmd_mv(config, source=str(ws), dest=str(target))

    out = capsys.readouterr().out
    assert "db_name" in out
    assert "quattromori" in out
    # And the rename really did land in odoorc.
    assert "db_name = quattromori" in (target / "odoorc").read_text()


def test_mv_reports_a_repo_it_could_not_repair(tmp_path, monkeypatch, capsys, config):
    """A missing bare repo is not a reason to leave the workspace behind."""
    bare = _bare_repo(tmp_path)
    ws = _make_workspace(tmp_path, "parrot", {"community": bare})
    target = tmp_path / "moved"
    _answer(monkeypatch, "y")
    _mise_ok(monkeypatch)

    # Make the config name a repo ow has no bare clone for.
    write_workspace_config(
        ws / MARKER,
        WorkspaceConfig(
            repos={
                "community": BranchSpec("origin/master", "master-parrot"),
                "enterprise": BranchSpec("origin/master", "master-parrot"),
            },
        ),
    )

    with pytest.raises(SystemExit) as exc:
        cmd_mv(config, source=str(ws), dest=str(target))

    assert exc.value.code == 1
    assert "enterprise" in capsys.readouterr().err
    # The move still happened, and the repo that could be repaired was.
    assert (target / MARKER).exists()
    assert str(target / "community") in _registered_worktrees(bare)


# ---------------------------------------------------------------------------
# Roundtrip: intent, ownership, and failure semantics through relocation
# ---------------------------------------------------------------------------

def test_mv_preserves_local_overrides_and_a_narrowed_base(tmp_path, monkeypatch, config):
    """A local http/python override, and a base that differs from upstream's
    own remote-tracking name, are the manifest's intent — mv never rewrites
    the manifest, so they survive verbatim."""
    bare = _bare_repo(tmp_path)
    ws = tmp_path / "workspaces" / "parrot"
    ws.mkdir(parents=True)
    write_workspace_config(
        ws / MARKER,
        WorkspaceConfig(
            repos={"community": BranchSpec("dev/master-phoenix", "feat-x")},
            odoo=OdooOverrides(http_port=9091),
            mise=MiseOverrides(python="3.12"),
        ),
    )
    _git(bare, "worktree", "add", "-b", "feat-x", str(ws / "community"), "origin/master")
    index.remember(ws)

    target = tmp_path / "moved"
    _answer(monkeypatch, "y")
    _mise_ok(monkeypatch)

    cmd_mv(config, source=str(ws), dest=str(target))

    reloaded = load_workspace_config(target / MARKER)
    assert reloaded.repos["community"] == BranchSpec("dev/master-phoenix", "feat-x")
    assert reloaded.odoo.http_port == 9091
    assert reloaded.mise.python == "3.12"


def test_mv_changes_the_absolute_data_dir_but_not_the_relative_addons_path(
    tmp_path, monkeypatch, config,
):
    """`data_dir` is the only absolute path a mv-triggered refresh owns;
    `addons_path` is workspace-relative and must not move just because the
    workspace did."""
    bare = _bare_repo(tmp_path)
    ws = _make_workspace(tmp_path, "parrot", {"community": bare})
    ws_config = load_workspace_config(ws / MARKER)
    refresh_workspace(config, ws_config, ws, trust=False)

    before = configparser.ConfigParser(interpolation=None)
    before.read(ws / "odoorc")
    addons_before = before["options"]["addons_path"]

    target = tmp_path / "moved"
    _answer(monkeypatch, "y")
    _mise_ok(monkeypatch)

    cmd_mv(config, source=str(ws), dest=str(target))

    after = configparser.ConfigParser(interpolation=None)
    after.read(target / "odoorc")
    assert after["options"]["addons_path"] == addons_before
    assert after["options"]["data_dir"] == str(target / ".odoo")
    assert str(ws) not in after["options"]["data_dir"]


def test_mv_leaves_a_hand_edited_odoorc_untouched_and_reports_it(
    tmp_path, monkeypatch, config, capsys,
):
    bare = _bare_repo(tmp_path)
    ws = _make_workspace(tmp_path, "parrot", {"community": bare})
    ws_config = load_workspace_config(ws / MARKER)
    refresh_workspace(config, ws_config, ws, trust=False)

    hand_edit = "[options]\nmy_own_words = true\n"
    (ws / "odoorc").write_text(hand_edit)

    target = tmp_path / "moved"
    _answer(monkeypatch, "y")
    _mise_ok(monkeypatch)

    capsys.readouterr()
    cmd_mv(config, source=str(ws), dest=str(target))

    assert (target / "odoorc").read_text() == hand_edit
    assert "yours, left alone: odoorc" in capsys.readouterr().out


def test_mv_of_a_schema1_workspace_skips_render_and_points_at_ow_render(
    tmp_path, monkeypatch, config, capsys,
):
    bare = _bare_repo(tmp_path)
    ws = tmp_path / "workspaces" / "parrot"
    (ws / ".ow").mkdir(parents=True)
    (ws / MARKER).write_text(
        "[repos]\n"
        'community = "origin/master..master-parrot"\n'
    )
    _git(bare, "worktree", "add", "-b", "master-parrot", str(ws / "community"), "origin/master")
    index.remember(ws)

    target = tmp_path / "moved"
    _answer(monkeypatch, "y")

    capsys.readouterr()
    cmd_mv(config, source=str(ws), dest=str(target))

    out = capsys.readouterr().out
    assert f"ow render -w {target}" in out
    assert not (target / "odoorc").exists()
    # The manifest itself is untouched — still schema 1, byte for byte.
    assert (target / MARKER).read_text().startswith("[repos]")
    assert index.known_workspaces() == [target]


def test_mv_render_failure_after_a_successful_move_leaves_index_at_target(
    tmp_path, monkeypatch, capsys, config,
):
    """Relocation and the index update already happened by the time a
    prerequisite can fail — that failure must not roll either back."""
    bare = _bare_repo(tmp_path)
    ws = _make_workspace(tmp_path, "parrot", {"community": bare})
    target = tmp_path / "parrot"
    _answer(monkeypatch, "y")
    monkeypatch.setattr(
        "ow.utils.relocate.require_mise",
        lambda: (_ for _ in ()).throw(ValueError("mise 2026.8.13 or newer is required")),
    )

    with pytest.raises(SystemExit) as exc:
        cmd_mv(config, source=str(ws), dest=str(target))

    assert exc.value.code == 1
    assert "mise" in capsys.readouterr().err
    assert not ws.exists()
    assert (target / MARKER).exists()
    assert index.known_workspaces() == [target]

    resolved_dir, resolved_ws = resolve_workspace(name="parrot")
    assert resolved_dir == target
    assert "community" in resolved_ws.repos
