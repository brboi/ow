"""Tests for ow.core.refresh: projection, ownership, the lock, and writes."""

from __future__ import annotations

import stat
import types
from pathlib import Path, PurePosixPath
from types import MappingProxyType

import pytest

from ow.core.plugins import Diagnostic, GeneratedFile, hookimpl, load_plugins
from ow.core.refresh import (
    FileStatus,
    Projection,
    _sha256_bytes,
    dumps_lock,
    project,
    read_lock,
    refresh,
    report,
)
from ow.core.workspace import Workspace
from ow.errors import OwError, PluginError

LOCK_REL = Path(".ow") / "rendered.lock.toml"


def make_ws(root: Path, settings: dict[str, object] | None = None) -> Workspace:
    return Workspace(
        root=root,
        name="ws",
        repos=(),
        settings=MappingProxyType(dict(settings or {})),
    )


def plugin(
    name: str,
    *,
    diagnostics: object = (),
    files: object = None,
    refreshed: object = None,
    raise_files: bool = False,
) -> types.ModuleType:
    """A stub plugin module exposing only the hooks a test needs.

    `files` and `diagnostics` are sequences (or callables returning one) of
    `GeneratedFile`/`Diagnostic`; `refreshed` is called with the `owned` tuple.
    """
    mod = types.ModuleType(name)
    mod.HOOKSPECS = ()

    if diagnostics:
        @hookimpl
        def ow_diagnostics(ws):
            return list(diagnostics() if callable(diagnostics) else diagnostics)

        mod.ow_diagnostics = ow_diagnostics

    if files is not None or raise_files:
        @hookimpl
        def ow_generated_files(ws, answers):
            if raise_files:
                raise RuntimeError("boom")
            return list(files() if callable(files) else files)

        mod.ow_generated_files = ow_generated_files

    if refreshed is not None:
        @hookimpl
        def ow_refreshed(ws, owned):
            refreshed(tuple(owned))

        mod.ow_refreshed = ow_refreshed

    return mod


def plugins_of(*modules: tuple[str, types.ModuleType]):
    return load_plugins(tuple(modules))


def write_lock(root: Path, entries: dict[str, str]) -> None:
    (root / ".ow").mkdir(parents=True, exist_ok=True)
    (root / LOCK_REL).write_bytes(dumps_lock(entries))


def lock_entries(root: Path) -> dict[str, str]:
    return read_lock(root)


def by_path(projection: Projection) -> dict[PurePosixPath, FileStatus]:
    return {status.path: status for status in projection.files}


# --- projection: fields and classification ----------------------------------


def test_absent_file_carries_owner_state_and_proposal(tmp_path):
    mod = plugin("a", files=[GeneratedFile(PurePosixPath("test.txt"), b"content")])
    projection = project(make_ws(tmp_path), plugins_of(("a", mod)))

    status = by_path(projection)[PurePosixPath("test.txt")]
    assert status.owner == "a"
    assert status.state == "absent"
    assert status.proposal == b"content"
    assert status.current is None
    assert status.reason is None
    assert status.mode == 0o644


def test_identical_file_is_adopted(tmp_path):
    (tmp_path / "test.txt").write_bytes(b"content")
    mod = plugin("a", files=[GeneratedFile(PurePosixPath("test.txt"), b"content")])

    projection = project(make_ws(tmp_path), plugins_of(("a", mod)))

    status = by_path(projection)[PurePosixPath("test.txt")]
    assert status.state == "up to date"
    assert status.current == b"content"


def test_file_matching_the_lock_is_outdated(tmp_path):
    (tmp_path / "test.txt").write_bytes(b"old")
    write_lock(tmp_path, {"test.txt": _sha256_bytes(b"old")})
    mod = plugin("a", files=[GeneratedFile(PurePosixPath("test.txt"), b"new")])

    projection = project(make_ws(tmp_path), plugins_of(("a", mod)))

    assert by_path(projection)[PurePosixPath("test.txt")].state == "outdated"


def test_edited_file_is_yours(tmp_path):
    (tmp_path / "test.txt").write_bytes(b"user edit")
    write_lock(tmp_path, {"test.txt": _sha256_bytes(b"ow's")})
    mod = plugin("a", files=[GeneratedFile(PurePosixPath("test.txt"), b"ow's")])

    projection = project(make_ws(tmp_path), plugins_of(("a", mod)))

    assert by_path(projection)[PurePosixPath("test.txt")].state == "yours"


def test_files_follow_plugin_tuple_order(tmp_path):
    b = plugin("b", files=[
        GeneratedFile(PurePosixPath("b1.txt"), b"1"),
        GeneratedFile(PurePosixPath("b2.txt"), b"2"),
    ])
    a = plugin("a", files=[GeneratedFile(PurePosixPath("a1.txt"), b"3")])

    projection = project(make_ws(tmp_path), plugins_of(("b", b), ("a", a)))

    assert [s.path for s in projection.files] == [
        PurePosixPath("b1.txt"),
        PurePosixPath("b2.txt"),
        PurePosixPath("a1.txt"),
    ]
    assert [s.owner for s in projection.files] == ["b", "b", "a"]


def test_diagnostics_follow_plugin_tuple_order(tmp_path):
    b = plugin(
        "b",
        diagnostics=[Diagnostic("warning", "b-answer")],
        files=[Diagnostic("info", "b-contrib"), GeneratedFile(PurePosixPath("b.txt"), b"b")],
    )
    a = plugin(
        "a",
        diagnostics=[Diagnostic("warning", "a-answer")],
        files=[Diagnostic("info", "a-contrib")],
    )

    projection = project(make_ws(tmp_path), plugins_of(("b", b), ("a", a)))

    assert [(name, diag.text) for name, diag in projection.diagnostics] == [
        ("b", "b-answer"),
        ("b", "b-contrib"),
        ("a", "a-answer"),
        ("a", "a-contrib"),
    ]


def test_error_diagnostic_sets_blocked(tmp_path):
    mod = plugin(
        "a",
        diagnostics=[Diagnostic("error", "boom")],
        files=[GeneratedFile(PurePosixPath("test.txt"), b"x")],
    )

    projection = project(make_ws(tmp_path), plugins_of(("a", mod)))

    assert projection.blocked is True
    assert [s.state for s in projection.files] == ["absent"]


# --- projection: aborts ------------------------------------------------------


def test_raising_generated_files_hook_becomes_plugin_error(tmp_path):
    mod = plugin("a", raise_files=True)

    with pytest.raises(PluginError) as exc:
        project(make_ws(tmp_path), plugins_of(("a", mod)))

    assert exc.value.plugin == "a"
    assert exc.value.hook == "ow_generated_files"


def test_two_owners_of_one_path_name_both(tmp_path):
    a = plugin("a", files=[GeneratedFile(PurePosixPath("same.txt"), b"a")])
    b = plugin("b", files=[GeneratedFile(PurePosixPath("same.txt"), b"b")])

    with pytest.raises(PluginError) as exc:
        project(make_ws(tmp_path), plugins_of(("a", a), ("b", b)))

    assert exc.value.plugin == "a and b"
    assert "both produce same.txt" in str(exc.value)


@pytest.mark.parametrize(
    "bad",
    ["/abs.txt", "../escape.txt", ".ow/config.toml", ".ow/rendered.lock.toml"],
)
def test_unsafe_path_aborts_at_collection(tmp_path, bad):
    mod = plugin("a", files=[GeneratedFile(PurePosixPath(bad), b"x")])

    with pytest.raises(PluginError) as exc:
        project(make_ws(tmp_path), plugins_of(("a", mod)))

    assert exc.value.hook == "ow_generated_files"
    assert bad in str(exc.value)


# --- projection: symlinked parent -------------------------------------------


def test_symlinked_parent_is_skipped_and_project_does_not_raise(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    mod = plugin("a", files=[GeneratedFile(PurePosixPath("link/inside.txt"), b"x")])

    projection = project(make_ws(tmp_path), plugins_of(("a", mod)))

    status = by_path(projection)[PurePosixPath("link/inside.txt")]
    assert status.state == "skipped"
    assert "symlink" in status.reason


# --- refresh: writes and the lock -------------------------------------------


def test_absent_file_is_written_with_mode_and_locked(tmp_path):
    mod = plugin("a", files=[GeneratedFile(PurePosixPath("script.sh"), b"#!/bin/sh\n", mode=0o755)])

    projection, written = refresh(make_ws(tmp_path), plugins_of(("a", mod)))

    script = tmp_path / "script.sh"
    assert written == (PurePosixPath("script.sh"),)
    assert script.read_bytes() == b"#!/bin/sh\n"
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert by_path(projection)[PurePosixPath("script.sh")].mode == 0o755
    assert lock_entries(tmp_path) == {"script.sh": _sha256_bytes(b"#!/bin/sh\n")}


def test_identical_file_is_adopted_without_a_write_but_locked(tmp_path):
    target = tmp_path / "test.txt"
    target.write_bytes(b"content")
    before = target.stat().st_mtime_ns
    mod = plugin("a", files=[GeneratedFile(PurePosixPath("test.txt"), b"content")])

    projection, written = refresh(make_ws(tmp_path), plugins_of(("a", mod)))

    assert written == ()
    assert by_path(projection)[PurePosixPath("test.txt")].state == "up to date"
    assert target.stat().st_mtime_ns == before
    assert lock_entries(tmp_path) == {"test.txt": _sha256_bytes(b"content")}


def test_outdated_file_is_rewritten(tmp_path):
    target = tmp_path / "test.txt"
    target.write_bytes(b"old")
    write_lock(tmp_path, {"test.txt": _sha256_bytes(b"old")})
    mod = plugin("a", files=[GeneratedFile(PurePosixPath("test.txt"), b"new")])

    _, written = refresh(make_ws(tmp_path), plugins_of(("a", mod)))

    assert written == (PurePosixPath("test.txt"),)
    assert target.read_bytes() == b"new"
    assert lock_entries(tmp_path) == {"test.txt": _sha256_bytes(b"new")}


def test_yours_file_is_never_rewritten_and_keeps_its_lock(tmp_path):
    target = tmp_path / "test.txt"
    target.write_bytes(b"user edit")
    write_lock(tmp_path, {"test.txt": _sha256_bytes(b"ow's")})
    mod = plugin("a", files=[GeneratedFile(PurePosixPath("test.txt"), b"ow's")])

    _, written = refresh(make_ws(tmp_path), plugins_of(("a", mod)))

    assert written == ()
    assert target.read_bytes() == b"user edit"
    assert lock_entries(tmp_path)["test.txt"] == _sha256_bytes(b"ow's")


def test_symlinked_parent_is_skipped_but_siblings_are_written(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    mod = plugin("a", files=[
        GeneratedFile(PurePosixPath("link/inside.txt"), b"x"),
        GeneratedFile(PurePosixPath("ok.txt"), b"y"),
    ])

    _, written = refresh(make_ws(tmp_path), plugins_of(("a", mod)))

    assert written == (PurePosixPath("ok.txt"),)
    assert (tmp_path / "ok.txt").read_bytes() == b"y"
    assert not (real / "inside.txt").exists()


def test_unproduced_path_loses_its_lock_entry_but_keeps_the_file(tmp_path):
    (tmp_path / "old.txt").write_bytes(b"old")
    write_lock(tmp_path, {"old.txt": _sha256_bytes(b"old")})
    mod = plugin("a", files=[GeneratedFile(PurePosixPath("new.txt"), b"new")])

    projection, written = refresh(make_ws(tmp_path), plugins_of(("a", mod)))

    assert projection.stale_lock == ("old.txt",)
    assert (tmp_path / "old.txt").exists()
    assert lock_entries(tmp_path) == {"new.txt": _sha256_bytes(b"new")}


def test_disabled_plugin_outputs_become_stale(tmp_path):
    a = plugin("a", files=[GeneratedFile(PurePosixPath("a.txt"), b"a")])
    b = plugin("b", files=[GeneratedFile(PurePosixPath("b.txt"), b"b")])
    plugins = plugins_of(("a", a), ("b", b))

    refresh(make_ws(tmp_path), plugins)
    assert lock_entries(tmp_path) == {
        "a.txt": _sha256_bytes(b"a"),
        "b.txt": _sha256_bytes(b"b"),
    }

    projection, written = refresh(make_ws(tmp_path, {"a.enabled": False}), plugins)

    assert projection.stale_lock == ("a.txt",)
    assert written == ()
    assert (tmp_path / "a.txt").exists()
    assert lock_entries(tmp_path) == {"b.txt": _sha256_bytes(b"b")}


def test_error_diagnostic_blocks_every_write(tmp_path):
    mod = plugin(
        "a",
        diagnostics=[Diagnostic("error", "boom")],
        files=[GeneratedFile(PurePosixPath("test.txt"), b"x")],
    )

    projection, written = refresh(make_ws(tmp_path), plugins_of(("a", mod)))

    assert projection.blocked is True
    assert written == ()
    assert not (tmp_path / "test.txt").exists()
    assert not (tmp_path / LOCK_REL).exists()


def test_lock_is_not_rewritten_when_its_bytes_do_not_change(tmp_path):
    mod = plugin("a", files=[GeneratedFile(PurePosixPath("test.txt"), b"x")])
    plugins = plugins_of(("a", mod))

    refresh(make_ws(tmp_path), plugins)
    lock_file = tmp_path / LOCK_REL
    before = lock_file.stat().st_mtime_ns

    refresh(make_ws(tmp_path), plugins)

    assert lock_file.stat().st_mtime_ns == before


# --- refresh: events ---------------------------------------------------------


def test_refreshed_hook_receives_written_and_up_to_date_not_yours(tmp_path):
    (tmp_path / "u.txt").write_bytes(b"u")
    (tmp_path / "y.txt").write_bytes(b"edited")
    write_lock(tmp_path, {"y.txt": _sha256_bytes(b"ow-y")})
    seen: list[tuple[PurePosixPath, ...]] = []
    mod = plugin(
        "a",
        files=[
            GeneratedFile(PurePosixPath("w.txt"), b"w"),
            GeneratedFile(PurePosixPath("u.txt"), b"u"),
            GeneratedFile(PurePosixPath("y.txt"), b"ow-y"),
        ],
        refreshed=seen.append,
    )

    refresh(make_ws(tmp_path), plugins_of(("a", mod)))

    assert len(seen) == 1
    assert set(seen[0]) == {PurePosixPath("w.txt"), PurePosixPath("u.txt")}


def test_refreshed_hook_failure_propagates_after_the_writes(tmp_path):
    def boom(owned):
        raise RuntimeError("boom")

    mod = plugin(
        "a",
        files=[GeneratedFile(PurePosixPath("test.txt"), b"x")],
        refreshed=boom,
    )

    with pytest.raises(PluginError) as exc:
        refresh(make_ws(tmp_path), plugins_of(("a", mod)))

    assert exc.value.hook == "ow_refreshed"
    assert (tmp_path / "test.txt").read_bytes() == b"x"
    assert lock_entries(tmp_path) == {"test.txt": _sha256_bytes(b"x")}


def test_raising_hook_aborts_before_any_write_or_lock(tmp_path):
    mod = plugin("a", raise_files=True)
    root = tmp_path / "ws"
    root.mkdir()

    with pytest.raises(PluginError):
        refresh(make_ws(root), plugins_of(("a", mod)))

    assert list(root.iterdir()) == []


def test_write_error_stops_and_locks_what_was_written(tmp_path, monkeypatch):
    import ow.core.refresh as refresh_mod

    real = refresh_mod._atomic_write

    def flaky(dest, data, mode):
        if dest.name == "two.txt":
            raise OSError("disk full")
        return real(dest, data, mode)

    monkeypatch.setattr(refresh_mod, "_atomic_write", flaky)
    mod = plugin("a", files=[
        GeneratedFile(PurePosixPath("one.txt"), b"1"),
        GeneratedFile(PurePosixPath("two.txt"), b"2"),
    ])

    with pytest.raises(OwError) as exc:
        refresh(make_ws(tmp_path), plugins_of(("a", mod)))

    assert "cannot write two.txt" in str(exc.value)
    assert "already wrote one.txt" in str(exc.value)
    assert (tmp_path / "one.txt").read_bytes() == b"1"
    assert not (tmp_path / "two.txt").exists()
    assert lock_entries(tmp_path) == {"one.txt": _sha256_bytes(b"1")}


def test_hand_edit_between_projection_and_write_is_never_overwritten(tmp_path, monkeypatch):
    import ow.core.refresh as refresh_mod

    target = tmp_path / "test.txt"
    target.write_bytes(b"old")
    write_lock(tmp_path, {"test.txt": _sha256_bytes(b"old")})
    mod = plugin("a", files=[GeneratedFile(PurePosixPath("test.txt"), b"ow's")])

    real_project = refresh_mod.project

    def project_then_hand_edit(ws, plugins):
        projection = real_project(ws, plugins)
        target.write_bytes(b"hand edit")  # lands after classification, before the write
        return projection

    monkeypatch.setattr(refresh_mod, "project", project_then_hand_edit)

    _, written = refresh(make_ws(tmp_path), plugins_of(("a", mod)))

    assert written == ()
    assert target.read_bytes() == b"hand edit"
    assert lock_entries(tmp_path)["test.txt"] == _sha256_bytes(b"old")
    status = by_path(real_project(make_ws(tmp_path), plugins_of(("a", mod))))[
        PurePosixPath("test.txt")
    ]
    assert status.state == "yours"


def test_hand_created_file_between_projection_and_write_is_not_locked(tmp_path, monkeypatch):
    import ow.core.refresh as refresh_mod

    target = tmp_path / "test.txt"
    mod = plugin("a", files=[GeneratedFile(PurePosixPath("test.txt"), b"ow's")])

    real_project = refresh_mod.project

    def project_then_hand_edit(ws, plugins):
        projection = real_project(ws, plugins)
        target.write_bytes(b"hand edit")
        return projection

    monkeypatch.setattr(refresh_mod, "project", project_then_hand_edit)

    _, written = refresh(make_ws(tmp_path), plugins_of(("a", mod)))

    assert written == ()
    assert target.read_bytes() == b"hand edit"
    assert lock_entries(tmp_path) == {}
    status = by_path(real_project(make_ws(tmp_path), plugins_of(("a", mod))))[
        PurePosixPath("test.txt")
    ]
    assert status.state == "yours"


# --- the lock file itself ----------------------------------------------------


def test_malformed_lock_is_empty_with_a_core_warning(tmp_path):
    (tmp_path / ".ow").mkdir()
    (tmp_path / LOCK_REL).write_bytes(b"not = valid = toml\n")
    mod = plugin("a", files=[GeneratedFile(PurePosixPath("test.txt"), b"x")])
    plugins = plugins_of(("a", mod))

    projection = project(make_ws(tmp_path), plugins)

    assert any(name == "core" and diag.level == "warning" for name, diag in projection.diagnostics)
    assert by_path(projection)[PurePosixPath("test.txt")].state == "absent"

    _, written = refresh(make_ws(tmp_path), plugins)
    assert written == (PurePosixPath("test.txt"),)
    assert lock_entries(tmp_path) == {"test.txt": _sha256_bytes(b"x")}


# --- report ------------------------------------------------------------------


def test_report_prints_writes_yours_skipped_and_blocked(tmp_path, monkeypatch):
    written_out: list[str] = []
    warnings: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr("ow.term.out", written_out.append)
    monkeypatch.setattr("ow.term.warn", warnings.append)
    monkeypatch.setattr("ow.term.error", errors.append)

    projection = Projection(
        files=(
            FileStatus(PurePosixPath("y.txt"), "a", "yours", b"p", b"c"),
            FileStatus(PurePosixPath("d.txt"), "a", "yours", b"p", None, "d.txt is a directory"),
            FileStatus(PurePosixPath("s.txt"), "a", "skipped", b"p", None, "link is a symlink"),
        ),
        stale_lock=(),
        diagnostics=(("a", Diagnostic("warning", "w")), ("a", Diagnostic("error", "e"))),
        blocked=True,
    )

    report(projection, [PurePosixPath("w.txt")])

    assert written_out == ["wrote w.txt"]
    assert any(
        "delete it and run `ow refresh` to get ow's version back" in line for line in warnings
    )
    assert any(
        "d.txt: not overwritten (yours); move it away and run `ow refresh`" in line
        for line in warnings
    )
    assert any("s.txt: skipped (link is a symlink)" in line for line in warnings)
    assert any(line == "a: w" for line in warnings)
    assert errors == ["a: e", "nothing was written"]
