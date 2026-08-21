"""`ow ls` — list every known workspace, its path, and its repos.

No git: that is what `ow status` is for. Test 6 pins this at the single
subprocess choke point (`subprocess.Popen`, inside `ow.utils.git._run`)
rather than at some downstream symptom, so a future command that starts
calling git by a different path still trips it. Patching `ow.utils.git._run`
itself would not be airtight: `ow.utils.refs` and `ow.commands.prune` both
import `_run` by value (`from ow.utils.git import _run`), so a patch on the
name in `ow.utils.git` never reaches them.

Every test here reaches the index, so every test takes the `xdg` fixture.
Without it a test writes into the developer's real XDG state directory.
"""

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from rich.console import Console

from ow.commands.ls import cmd_ls
from ow.utils import index
from ow.utils.config import WorkspaceConfig, parse_branch_spec, write_workspace_config


def _make_ws(base: Path, name: str, repos: dict[str, str] | None = None) -> Path:
    """A real workspace on disk, remembered in the index."""
    ws_dir = base / name
    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(
            repos={alias: parse_branch_spec(s) for alias, s in (repos or {}).items()},
            templates=["common"],
        ),
    )
    index.remember(ws_dir)
    return ws_dir


# ---------------------------------------------------------------------------
# 0. A legacy layout must be reported, not hidden behind "no workspaces".
#
# Three other error messages (resolver.py x2, index.py) tell a lost user to
# run `ow ls`. If `ls` itself skips the legacy guard, the command they are
# sent to is the one that lies to them.
# ---------------------------------------------------------------------------

def test_detects_legacy_layout(tmp_path, capsys, xdg):
    """An old-layout ow.toml with no global config yet must point at the
    migration guide — but as a warning: ls is where a migrating user goes to
    see what ow has picked up so far, so it must still list."""
    (tmp_path / "ow.toml").write_text("")
    _make_ws(tmp_path, "alpha", {"community": "master"})

    cmd_ls()  # must not raise

    out, err = capsys.readouterr()
    assert "docs/migrating-to-2.0.md" in err
    assert "alpha" in out


def test_a_stray_legacy_workspace_config_in_cwd_does_not_block_the_listing(
    tmp_path, capsys, xdg, monkeypatch
):
    """The one command a migrating user needs is the one they were running
    from inside the very workspaces they are migrating."""
    listed = _make_ws(tmp_path / "known", "alpha", {"community": "master"})

    cwd = tmp_path / "legacy-ws"
    (cwd / ".ow").mkdir(parents=True)
    (cwd / ".ow" / "config").write_text("")
    monkeypatch.chdir(cwd)

    cmd_ls()  # must not raise

    out, err = capsys.readouterr()
    assert "alpha" in out
    assert str(cwd / ".ow" / "config") in err
    assert "mv .ow/config .ow/config.toml" in err
    assert listed.exists()


def test_other_commands_still_exit_on_a_legacy_workspace_config(tmp_path, capsys, xdg, monkeypatch):
    """Only ls is exempt: everything else writes, so it still stops."""
    from ow.utils.legacy import check_legacy_layout

    cwd = tmp_path / "legacy-ws"
    (cwd / ".ow").mkdir(parents=True)
    (cwd / ".ow" / "config").write_text("")
    monkeypatch.chdir(cwd)

    with pytest.raises(typer.Exit) as exc:
        check_legacy_layout()

    assert exc.value.exit_code == 1


# ---------------------------------------------------------------------------
# 1. Two known workspaces: both appear, with name and aliases.
# ---------------------------------------------------------------------------

def test_two_workspaces_both_appear(tmp_path, capsys, xdg):
    _make_ws(tmp_path, "alpha", {"community": "master"})
    _make_ws(tmp_path, "beta", {"enterprise": "master..fix"})

    cmd_ls()

    out = capsys.readouterr().out
    assert "alpha" in out
    assert "beta" in out
    assert "community" in out
    assert "enterprise" in out


# ---------------------------------------------------------------------------
# 2. The home directory is abbreviated to ~.
# ---------------------------------------------------------------------------

def test_home_is_abbreviated(tmp_path, capsys, xdg, monkeypatch):
    """Rich sizes the PATH column to the 80-column fallback console width and
    ellipsizes long cells, so on a narrow console the raw absolute path never
    appears whatever the production code does — that made
    `assert str(ws_dir) not in out` pass even against a `_display_path` that
    returns the full path unabbreviated. Widening the console so the full
    path actually fits is what makes the negative assertion mean something.
    """
    fake_home = tmp_path / "home" / "dev"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setattr("ow.commands.ls.console", Console(highlight=False, soft_wrap=True, width=300))

    ws_dir = _make_ws(fake_home, "myws")

    cmd_ls()

    out = capsys.readouterr().out
    # Built independently of the production helper: the literal string that
    # a correct implementation must produce, not a call to the same code.
    assert "~/myws" in out
    assert str(ws_dir) not in out


# ---------------------------------------------------------------------------
# 3. Empty index: message, exit 0, no exception.
# ---------------------------------------------------------------------------

def test_empty_index_reports_and_exits_cleanly(capsys, xdg):
    cmd_ls()  # must not raise

    out = capsys.readouterr().out
    assert "ow init" in out


# ---------------------------------------------------------------------------
# 4. A workspace with invalid TOML is marked in error, others still listed.
# ---------------------------------------------------------------------------

def test_broken_config_marked_error_others_still_listed(tmp_path, capsys, xdg):
    good = _make_ws(tmp_path, "good", {"community": "master"})
    broken = tmp_path / "broken"
    (broken / ".ow").mkdir(parents=True)
    (broken / ".ow" / "config.toml").write_text("this is [ not valid toml")
    index.remember(broken)

    cmd_ls()

    out = capsys.readouterr().out
    # The bug this guards: an implementation that lets the TOML error
    # propagate would blow up before "good" is ever printed. Only checking
    # exit behaviour would miss a "found the first error and stopped" bug
    # that still exits 0 but silently drops "good".
    assert "good" in out
    assert "community" in out
    assert "broken" in out
    assert "error" in out.lower()


# ---------------------------------------------------------------------------
# 4b. A workspace name that looks like Rich markup must not blow up the
#     whole listing: table cells are plain data, not markup source.
# ---------------------------------------------------------------------------

def test_bracketed_path_does_not_break_the_listing(tmp_path, capsys, xdg, monkeypatch):
    """"/" cannot appear inside a single filename, so the dangerous pattern
    a real filesystem can produce is not one bracketed directory name but a
    bracket left dangling by one path component and closed by the next one
    down — e.g. a directory named "ws[" containing a workspace named
    "bad]" renders, PATH column included, as ".../ws[/bad]": a closing Rich
    tag with no opening match. console.print(table) would raise
    MarkupError and the whole listing would be lost, including any other,
    unrelated workspace.

    Console width is widened (as in test_home_is_abbreviated) so the long
    PATH string is not ellipsized away before the assertion can see it.
    """
    monkeypatch.setattr("ow.commands.ls.console", Console(highlight=False, soft_wrap=True, width=300))
    _make_ws(tmp_path / "ws[", "bad]", {"community": "master"})
    _make_ws(tmp_path, "normal", {"community": "master"})

    cmd_ls()  # must not raise rich.errors.MarkupError

    out = capsys.readouterr().out
    assert "ws[/bad]" in out
    assert "normal" in out


def test_error_message_with_markup_like_text_does_not_break_the_listing(tmp_path, capsys, xdg, monkeypatch):
    """The error cell interpolates an exception message, which is data too:
    an exception whose text happens to contain bracket-like content must
    not be parsed as markup either.
    """
    _make_ws(tmp_path, "broken")

    monkeypatch.setattr(
        "ow.commands.ls.load_workspace_config",
        lambda path: (_ for _ in ()).throw(ValueError("bad value: [/nope]")),
    )

    cmd_ls()  # must not raise rich.errors.MarkupError

    out = capsys.readouterr().out
    assert "error" in out.lower()


# ---------------------------------------------------------------------------
# 5. A workspace whose directory vanished does not appear.
# ---------------------------------------------------------------------------

def test_vanished_workspace_does_not_appear(tmp_path, capsys, xdg):
    live = _make_ws(tmp_path, "live")
    gone = _make_ws(tmp_path, "gone")
    shutil.rmtree(gone)

    cmd_ls()

    out = capsys.readouterr().out
    assert "live" in out
    assert "gone" not in out


# ---------------------------------------------------------------------------
# 6. No git subprocess is ever launched.
# ---------------------------------------------------------------------------

def test_never_runs_git(tmp_path, capsys, xdg):
    _make_ws(tmp_path, "alpha", {"community": "master"})

    # subprocess.Popen (ow/utils/git.py:44) is the single point every git
    # invocation in ow passes through, whichever module holds the name that
    # kicked it off. Patching `ow.utils.git._run` instead would miss calls
    # made through names bound by value elsewhere, e.g. `ow.utils.refs._run`
    # or `ow.commands.prune._run` (both `from ow.utils.git import _run`).
    with patch("ow.utils.git.subprocess.Popen") as mock_popen:
        cmd_ls()

    mock_popen.assert_not_called()
