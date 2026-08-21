"""Tests for ow.utils.resolver.

Four forms, one meaning each, one failure each, and no fallback between
them: a path-shaped argument, a bare name looked up in the discovery index,
OW_WORKSPACE as an absolute path, and the walk-up from cwd. Every successful
resolution is written back to the index.

Every test here reaches the index, so every test takes the `xdg` fixture.
Without it a test writes into the developer's real XDG state directory.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from ow.utils import index
from ow.utils.config import WorkspaceConfig, write_workspace_config
from ow.utils.resolver import resolve_workspace


def _make_ws(base: Path, name: str, *, templates: list[str] | None = None) -> Path:
    """A real workspace on disk: a directory holding .ow/config.toml."""
    ws_dir = base / name
    write_workspace_config(
        ws_dir / ".ow" / "config.toml",
        WorkspaceConfig(repos={}, templates=templates or ["common"], vars={}),
    )
    return ws_dir


@pytest.fixture(autouse=True)
def _no_inherited_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's own OW_WORKSPACE must not leak into these tests."""
    monkeypatch.delenv("OW_WORKSPACE", raising=False)


# ---------------------------------------------------------------------------
# 1-2. No argument: walk up from cwd
# ---------------------------------------------------------------------------


def test_walks_up_from_a_subdirectory(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir = _make_ws(xdg, "walkup", templates=["common", "odoo"])
    deep = ws_dir / "community" / "addons"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    resolved_dir, ws = resolve_workspace()

    assert resolved_dir == ws_dir.resolve()
    assert ws.templates == ["common", "odoo"]


def test_outside_any_workspace_fails_and_suggests_ow_ls(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    barren = xdg / "nowhere" / "at" / "all"
    barren.mkdir(parents=True)
    monkeypatch.chdir(barren)

    with pytest.raises(SystemExit):
        resolve_workspace()

    err = capsys.readouterr().err
    assert "no workspace" in err.lower()
    assert "ow ls" in err


# ---------------------------------------------------------------------------
# 3-5. A bare name: looked up in the index
# ---------------------------------------------------------------------------


def test_unique_name_resolves_through_the_index(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir = _make_ws(xdg / "somewhere", "quattromori", templates=["odoo"])
    index.remember(ws_dir)
    # cwd is deliberately not the workspace: the name is what resolves it.
    monkeypatch.chdir(xdg)

    resolved_dir, ws = resolve_workspace(name="quattromori")

    assert resolved_dir == ws_dir.resolve()
    assert ws.templates == ["odoo"]


def test_unknown_name_fails_and_suggests_ow_ls(
    xdg: Path, capsys: pytest.CaptureFixture
) -> None:
    with pytest.raises(SystemExit):
        resolve_workspace(name="quattromori")

    err = capsys.readouterr().err
    assert "quattromori" in err
    assert "ow ls" in err


def test_ambiguous_name_lists_every_candidate_and_picks_none(
    xdg: Path, capsys: pytest.CaptureFixture
) -> None:
    left = _make_ws(xdg / "left", "twin")
    right = _make_ws(xdg / "right", "twin")
    index.remember(left)
    index.remember(right)

    with pytest.raises(SystemExit):
        resolve_workspace(name="twin")

    err = capsys.readouterr().err
    assert "twin" in err
    assert str(left.resolve()) in err
    assert str(right.resolve()) in err


def test_a_name_never_falls_back_to_a_relative_path(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A real workspace sitting in cwd is still not a *name* the index knows."""
    decoy = _make_ws(xdg / "cwd", "ghost")
    monkeypatch.chdir(decoy.parent)
    assert index.find_by_name("ghost") == []

    with pytest.raises(SystemExit):
        resolve_workspace(name="ghost")

    err = capsys.readouterr().err
    assert "ghost" in err
    assert "ow ls" in err


# ---------------------------------------------------------------------------
# 6-8. A path-shaped argument
# ---------------------------------------------------------------------------


def test_absolute_path_argument_resolves(xdg: Path) -> None:
    ws_dir = _make_ws(xdg, "by-path", templates=["common"])

    resolved_dir, ws = resolve_workspace(name=str(ws_dir))

    assert resolved_dir == ws_dir.resolve()
    assert ws.templates == ["common"]


def test_relative_path_argument_resolves(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir = _make_ws(xdg / "holder", "relative-ws")
    monkeypatch.chdir(ws_dir.parent)

    resolved_dir, _ = resolve_workspace(name="./relative-ws")

    assert resolved_dir == ws_dir.resolve()


def test_dot_is_a_path_not_a_name(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir = _make_ws(xdg, "dot-ws")
    monkeypatch.chdir(ws_dir)

    resolved_dir, _ = resolve_workspace(name=".")

    assert resolved_dir == ws_dir.resolve()


def test_dotdot_is_a_path_not_a_name(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir = _make_ws(xdg, "dotdot-ws")
    inside = ws_dir / "community"
    inside.mkdir()
    monkeypatch.chdir(inside)

    resolved_dir, _ = resolve_workspace(name="..")

    assert resolved_dir == ws_dir.resolve()


def test_tilde_is_expanded(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = xdg / "home"
    home.mkdir()
    ws_dir = _make_ws(home, "tilde-ws")
    monkeypatch.setenv("HOME", str(home))
    # cwd is not the home, so an unexpanded "~/tilde-ws" would resolve to the
    # non-existent <cwd>/~/tilde-ws rather than to the workspace.
    monkeypatch.chdir(xdg)

    resolved_dir, _ = resolve_workspace(name="~/tilde-ws")

    assert resolved_dir == ws_dir.resolve()


def test_path_that_is_not_a_workspace_names_the_missing_config(
    xdg: Path, capsys: pytest.CaptureFixture
) -> None:
    stray = xdg / "not-a-workspace"
    stray.mkdir()

    with pytest.raises(SystemExit):
        resolve_workspace(name=str(stray))

    err = capsys.readouterr().err
    assert str(stray.resolve() / ".ow" / "config.toml") in err


def test_workspace_config_is_named_config_toml(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: the per-workspace config file is `.ow/config.toml`,
    not the old extensionless `.ow/config`. Uses the path form of resolution
    so that only the filename literal is under test."""
    ws_dir = _make_ws(xdg, "toml-check", templates=["common"])
    assert (ws_dir / ".ow" / "config.toml").exists()
    assert not (ws_dir / ".ow" / "config").exists()

    resolved_dir, ws = resolve_workspace(name=str(ws_dir))

    assert resolved_dir == ws_dir.resolve()
    assert ws.templates == ["common"]


# ---------------------------------------------------------------------------
# 9-10. OW_WORKSPACE: one form only
# ---------------------------------------------------------------------------


def test_ow_workspace_empty_string_means_unset(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty OW_WORKSPACE is an absence, not a fifth form: it must fall
    through to the cwd walk-up, consistent with the XDG variables."""
    ws_dir = _make_ws(xdg, "empty-env", templates=["common"])
    deep = ws_dir / "community"
    deep.mkdir()
    monkeypatch.setenv("OW_WORKSPACE", "")
    monkeypatch.chdir(deep)

    resolved_dir, ws = resolve_workspace()

    assert resolved_dir == ws_dir.resolve()
    assert ws.templates == ["common"]


def test_ow_workspace_absolute_path_resolves(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir = _make_ws(xdg, "env-ws", templates=["common"])
    monkeypatch.setenv("OW_WORKSPACE", str(ws_dir))
    monkeypatch.chdir(xdg)

    resolved_dir, ws = resolve_workspace()

    assert resolved_dir == ws_dir.resolve()
    assert ws.templates == ["common"]


def test_ow_workspace_as_a_bare_name_fails_even_when_the_index_knows_it(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The point of the rewrite: OW_WORKSPACE is never guessed as a name."""
    ws_dir = _make_ws(xdg, "env-name")
    index.remember(ws_dir)
    assert index.find_by_name("env-name") == [ws_dir.resolve()]
    monkeypatch.setenv("OW_WORKSPACE", "env-name")

    with pytest.raises(SystemExit):
        resolve_workspace()

    err = capsys.readouterr().err
    assert "OW_WORKSPACE" in err
    assert "env-name" in err
    assert "absolute path" in err


def test_ow_workspace_as_a_relative_path_fails(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    ws_dir = _make_ws(xdg / "holder", "rel-env")
    monkeypatch.chdir(ws_dir.parent)
    monkeypatch.setenv("OW_WORKSPACE", "./rel-env")

    with pytest.raises(SystemExit):
        resolve_workspace()

    err = capsys.readouterr().err
    assert "OW_WORKSPACE" in err
    assert "absolute path" in err


def test_ow_workspace_as_a_tilde_path_fails(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """`~` is a shell nicety, not an absolute path — one form only."""
    home = xdg / "home"
    home.mkdir()
    _make_ws(home, "tilde-env")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OW_WORKSPACE", "~/tilde-env")

    with pytest.raises(SystemExit):
        resolve_workspace()

    err = capsys.readouterr().err
    assert "OW_WORKSPACE" in err
    assert "absolute path" in err
    # The value looks absolute enough to the user; explain why `~` doesn't
    # count and what to do instead, rather than leaving them puzzled.
    assert "not expanded" in err


def test_ow_workspace_absolute_path_that_is_not_a_workspace_fails(
    xdg: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    stray = xdg / "env-stray"
    stray.mkdir()
    monkeypatch.setenv("OW_WORKSPACE", str(stray))

    with pytest.raises(SystemExit):
        resolve_workspace()

    err = capsys.readouterr().err
    assert str(stray.resolve() / ".ow" / "config.toml") in err


def test_an_explicit_argument_beats_ow_workspace(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wanted = _make_ws(xdg, "wanted", templates=["wanted"])
    other = _make_ws(xdg, "other", templates=["other"])
    monkeypatch.setenv("OW_WORKSPACE", str(other))

    resolved_dir, ws = resolve_workspace(name=str(wanted))

    assert resolved_dir == wanted.resolve()
    assert ws.templates == ["wanted"]


# ---------------------------------------------------------------------------
# 11. Every success is remembered
# ---------------------------------------------------------------------------


def test_path_form_is_remembered(xdg: Path) -> None:
    ws_dir = _make_ws(xdg, "remember-path")
    assert index.known_workspaces() == []

    resolve_workspace(name=str(ws_dir))

    assert index.known_workspaces() == [ws_dir.resolve()]


def test_cwd_walkup_is_remembered(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir = _make_ws(xdg, "remember-cwd")
    inside = ws_dir / "community"
    inside.mkdir()
    monkeypatch.chdir(inside)
    assert index.known_workspaces() == []

    resolve_workspace()

    assert index.known_workspaces() == [ws_dir.resolve()]


def test_name_form_is_remembered(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`find_by_name` only searches what the index already knows, so a
    workspace must be pre-seeded to be resolvable by name — which means
    `test_unique_name_resolves_through_the_index` proves resolution works
    but, since the index already knows the entry, would still pass even if
    the name path's own `index.remember` call were deleted. Spy on the call
    itself so this test actually has teeth for that call."""
    ws_dir = _make_ws(xdg / "somewhere", "remember-name")
    index.remember(ws_dir)
    monkeypatch.chdir(xdg)

    with patch("ow.utils.resolver.index.remember", wraps=index.remember) as spy:
        resolve_workspace(name="remember-name")

    spy.assert_called_once_with(ws_dir.resolve())


def test_ow_workspace_form_is_remembered(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir = _make_ws(xdg, "remember-env")
    monkeypatch.setenv("OW_WORKSPACE", str(ws_dir))
    assert index.known_workspaces() == []

    resolve_workspace()

    assert index.known_workspaces() == [ws_dir.resolve()]


def test_a_failed_resolution_is_not_remembered(
    xdg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stray = xdg / "failed"
    stray.mkdir()
    monkeypatch.chdir(stray)

    with pytest.raises(SystemExit):
        resolve_workspace(name=str(stray))

    assert index.known_workspaces() == []


# ---------------------------------------------------------------------------
# 12. A workspace config the user broke by hand
# ---------------------------------------------------------------------------


def test_malformed_workspace_config_fails_cleanly(
    xdg: Path, capsys: pytest.CaptureFixture
) -> None:
    """`ow init` says "Edit it to customize vars" — so people edit it.

    A fumbled quote must not answer with an eight-frame traceback ending in
    TOMLDecodeError. The global config already gets a one-line message for
    exactly this; the four resolver forms are the only place it was missing.
    """
    ws_dir = xdg / "broken"
    (ws_dir / ".ow").mkdir(parents=True)
    (ws_dir / ".ow" / "config.toml").write_text('templates = ["common\n')

    with pytest.raises(SystemExit) as exc:
        resolve_workspace(name=str(ws_dir))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert str(ws_dir.resolve() / ".ow" / "config.toml") in err
    assert "could not load" in err


def test_workspace_config_missing_templates_fails_cleanly(
    xdg: Path, capsys: pytest.CaptureFixture
) -> None:
    """Valid TOML, missing a required key: a ValueError, not a decode error,
    and just as much of a traceback before this."""
    ws_dir = xdg / "keyless"
    (ws_dir / ".ow").mkdir(parents=True)
    (ws_dir / ".ow" / "config.toml").write_text("[repos]\n")

    with pytest.raises(SystemExit) as exc:
        resolve_workspace(name=str(ws_dir))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert str(ws_dir.resolve() / ".ow" / "config.toml") in err
    assert "templates" in err


def test_a_workspace_that_will_not_load_is_not_remembered(xdg: Path) -> None:
    """The index records where a workspace *is*, and a file ow cannot read
    is not evidence of one — remembering it would hand `ow ls` an entry
    that fails the same way on every listing."""
    ws_dir = xdg / "unreadable"
    (ws_dir / ".ow").mkdir(parents=True)
    (ws_dir / ".ow" / "config.toml").write_text("templates = [\n")

    with pytest.raises(SystemExit):
        resolve_workspace(name=str(ws_dir))

    assert index.known_workspaces() == []
