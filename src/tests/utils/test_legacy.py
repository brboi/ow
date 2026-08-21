"""Tests for ow.utils.legacy — detecting the pre-2.0 layout.

check_legacy_layout() exists to point someone still on the old per-project
layout (an `ow.toml` project root, a per-workspace `.ow/config` with no
extension) at the migration guide, instead of leaving them staring at "no
workspace found" while their workspaces sit right there.
"""

from pathlib import Path

import pytest
import typer

from ow.utils import paths
from ow.utils.legacy import check_legacy_layout


def test_silent_when_global_config_already_exists(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No false positive: a normal, already-migrated setup says nothing."""
    paths.config_home().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("")
    monkeypatch.chdir(xdg)

    check_legacy_layout()  # must not raise


def test_silent_in_an_empty_directory(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No false positive: nothing anywhere, nothing to say."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)

    check_legacy_layout()  # must not raise


def test_detects_old_project_root(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """Form 1: no global config yet, but an ow.toml project root exists above cwd."""
    project = tmp_path / "project"
    sub = project / "workspaces" / "demo"
    sub.mkdir(parents=True)
    (project / "ow.toml").write_text("")
    monkeypatch.chdir(sub)
    assert not paths.config_file().exists()

    with pytest.raises(typer.Exit) as exc:
        check_legacy_layout()

    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert str(project) in err
    assert "ow.toml" in err
    assert "docs/migrating-to-2.0.md" in err


def test_detects_old_workspace_config(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """Form 2: a workspace still has `.ow/config`, not `.ow/config.toml`."""
    ws = tmp_path / "myws"
    (ws / ".ow").mkdir(parents=True)
    old_config = ws / ".ow" / "config"
    old_config.write_text("")
    monkeypatch.chdir(ws)

    with pytest.raises(typer.Exit) as exc:
        check_legacy_layout()

    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert str(old_config) in err
    assert "docs/migrating-to-2.0.md" in err


def test_the_guide_is_named_by_url(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """The reader is not standing in a checkout.

    Most people meeting this message installed ow from PyPI, so a repo-relative
    path names a file they do not have. It has to be something they can open.
    """
    (tmp_path / "ow.toml").write_text("")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(typer.Exit):
        check_legacy_layout()

    err = capsys.readouterr().err
    assert "https://github.com/brboi/ow/blob/main/docs/migrating-to-2.0.md" in err


def test_project_root_path_with_brackets_is_not_treated_as_markup(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    """A directory name containing square brackets must print literally.

    err_console.print() parses Rich markup by default; an interpolated path
    is data, not markup, and must survive intact even if it happens to
    contain something that looks like a markup tag.
    """
    project = tmp_path / "[weird]"
    sub = project / "workspaces" / "demo"
    sub.mkdir(parents=True)
    (project / "ow.toml").write_text("")
    monkeypatch.chdir(sub)

    with pytest.raises(typer.Exit):
        check_legacy_layout()

    err = capsys.readouterr().err
    assert str(project) in err


def test_old_workspace_config_is_not_flagged_once_migrated(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A workspace with both files (mid-migration, or .ow/config kept as a
    backup) is not a legacy layout: config.toml is what ow actually reads."""
    ws = tmp_path / "myws"
    (ws / ".ow").mkdir(parents=True)
    (ws / ".ow" / "config").write_text("")
    (ws / ".ow" / "config.toml").write_text("")
    monkeypatch.chdir(ws)

    check_legacy_layout()  # must not raise


def test_silent_with_a_global_config_beside_an_old_ow_toml(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "already migrated" guard, with something for it to guard against.

    test_silent_when_global_config_already_exists is silent because there
    is no ow.toml anywhere above its cwd — it would pass with the guard
    deleted. Put a stray ow.toml above cwd and the guard is the only thing
    standing between an already-migrated user and a hard exit 1 on every
    single ow command.
    """
    paths.config_home().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("")
    (tmp_path / "ow.toml").write_text("")
    monkeypatch.chdir(tmp_path)

    check_legacy_layout()  # must not raise


def test_detects_an_old_workspace_config_from_a_subdirectory(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Someone hits this from wherever they happen to be working — inside
    `community/addons`, not politely standing at the workspace root. Every
    other test of this branch runs from the workspace itself, so the
    walk-up over `current.parents` was never exercised."""
    ws = tmp_path / "myws"
    old_config = ws / ".ow" / "config"
    old_config.parent.mkdir(parents=True)
    old_config.write_text("")
    deep = ws / "community" / "addons"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    with pytest.raises(typer.Exit) as exc:
        check_legacy_layout()

    assert exc.value.exit_code == 1
    assert str(old_config) in capsys.readouterr().err


def test_the_workspace_config_message_names_the_fix(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A one-line rename should not cost the reader a trip to a web page."""
    ws = tmp_path / "myws"
    (ws / ".ow").mkdir(parents=True)
    (ws / ".ow" / "config").write_text("")
    monkeypatch.chdir(ws)

    with pytest.raises(typer.Exit):
        check_legacy_layout()

    assert "mv .ow/config .ow/config.toml" in capsys.readouterr().err


def test_fatal_false_warns_about_an_old_workspace_config_and_returns(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """`ow ls` is read-only, so a stray legacy config must not block it."""
    ws = tmp_path / "myws"
    (ws / ".ow").mkdir(parents=True)
    old_config = ws / ".ow" / "config"
    old_config.write_text("")
    monkeypatch.chdir(ws)

    check_legacy_layout(fatal=False)  # must not raise

    err = capsys.readouterr().err
    assert str(old_config) in err
    assert "docs/migrating-to-2.0.md" in err


def test_fatal_false_warns_about_an_old_project_root_and_returns(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "ow.toml").write_text("")
    monkeypatch.chdir(project)

    check_legacy_layout(fatal=False)  # must not raise

    err = capsys.readouterr().err
    assert str(project) in err
    assert "docs/migrating-to-2.0.md" in err


def test_fatal_false_is_still_silent_on_a_migrated_setup(
    xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Warning instead of exiting must not turn into warning unconditionally."""
    paths.config_home().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("")
    monkeypatch.chdir(tmp_path)

    check_legacy_layout(fatal=False)

    assert capsys.readouterr().err == ""
