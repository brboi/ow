"""Byte ownership, ignore rules and safe writes.

`GeneratedFile` is a local stand-in with the fields Task 3's frozen
`ow.utils.generate.GeneratedFile` declares (`path`, `data`, `mode`);
`render.py` only duck-types on those, so this file never imports
`ow.utils.generate`.
"""

import stat
from dataclasses import dataclass
from pathlib import PurePosixPath

import pytest

from ow.utils.render import (
    ABSENT,
    IGNORED,
    NOT_RENDERED,
    OUTDATED,
    UP_TO_DATE,
    YOURS,
    RenderedFile,
    RenderPlan,
    RenderResult,
    dumps_lock,
    plan_files,
    read_lock,
    write_files,
)


@dataclass(frozen=True)
class GeneratedFile:
    path: PurePosixPath
    data: bytes | None
    mode: int = 0o644


def gf(path: str, data: bytes | None, mode: int = 0o644) -> GeneratedFile:
    return GeneratedFile(PurePosixPath(path), data, mode)


def states_by_path(plan: RenderPlan) -> dict[str, RenderedFile]:
    return {r.path: r for r in plan.states}


# ---------------------------------------------------------------------------
# The mandatory consumer-level regression sequence.
# ---------------------------------------------------------------------------


def test_unknown_edits_are_preserved_then_identical_content_can_be_adopted(tmp_path):
    output = tmp_path / "settings.json"
    output.write_bytes(b'{"mine": true}\n')
    proposed = (gf("settings.json", b'{"mine": false}\n'),)
    first = write_files(plan_files(tmp_path, proposed, ()))
    assert first.yours == ("settings.json",)
    assert output.read_bytes() == b'{"mine": true}\n'
    same = (gf("settings.json", output.read_bytes()),)
    write_files(plan_files(tmp_path, same, ()))
    write_files(plan_files(tmp_path, proposed, ()))
    assert output.read_bytes() == b'{"mine": false}\n'


# ---------------------------------------------------------------------------
# The six states, individually.
# ---------------------------------------------------------------------------


def test_absent_output_is_written_and_locked(tmp_path):
    dest = tmp_path / "odoorc"
    proposed = (gf("odoorc", b"[options]\n", mode=0o600),)

    plan = plan_files(tmp_path, proposed, ())
    assert states_by_path(plan)["odoorc"].state == ABSENT

    result = write_files(plan)
    assert result.wrote == ("odoorc",)
    assert dest.read_bytes() == b"[options]\n"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert read_lock(tmp_path)["odoorc"]


def test_preexisting_identical_file_is_adopted_without_rewrite(tmp_path):
    dest = tmp_path / "settings.json"
    dest.write_text("same\n")
    mtime_before = dest.stat().st_mtime_ns
    proposed = (gf("settings.json", b"same\n"),)

    plan = plan_files(tmp_path, proposed, ())
    assert states_by_path(plan)["settings.json"].state == UP_TO_DATE

    result = write_files(plan)
    assert result.adopted == ("settings.json",)
    assert dest.stat().st_mtime_ns == mtime_before
    assert read_lock(tmp_path)["settings.json"]


def test_locked_file_matching_lock_hash_is_updated(tmp_path):
    dest = tmp_path / "settings.json"
    write_files(plan_files(tmp_path, (gf("settings.json", b"v1\n"),), ()))

    plan = plan_files(tmp_path, (gf("settings.json", b"v2\n"),), ())
    assert states_by_path(plan)["settings.json"].state == OUTDATED

    result = write_files(plan)
    assert result.updated == ("settings.json",)
    assert dest.read_bytes() == b"v2\n"


def test_divergent_file_is_preserved_as_yours(tmp_path):
    dest = tmp_path / "settings.json"
    dest.write_text("hand edited\n")
    proposed = (gf("settings.json", b"generated\n"),)

    plan = plan_files(tmp_path, proposed, ())
    assert states_by_path(plan)["settings.json"].state == YOURS

    result = write_files(plan)
    assert result.yours == ("settings.json",)
    assert dest.read_text() == "hand edited\n"
    assert read_lock(tmp_path) == {}


def test_output_removed_from_table_stays_not_rendered_while_on_disk(tmp_path):
    dest = tmp_path / "legacy.txt"
    write_files(plan_files(tmp_path, (gf("legacy.txt", b"legacy\n"),), ()))

    # The current generation still names the path but has nothing to put
    # there (data=None) -- same story as a path the fixed table dropped.
    plan = plan_files(tmp_path, (gf("legacy.txt", None),), ())
    rendered = states_by_path(plan)["legacy.txt"]
    assert rendered.state == NOT_RENDERED
    assert rendered.ow_text is None
    assert rendered.your_text == "legacy\n"
    assert dest.read_bytes() == b"legacy\n"

    # A lock-only entry not named by the outputs tuple at all behaves the same.
    plan2 = plan_files(tmp_path, (), ())
    assert states_by_path(plan2)["legacy.txt"].state == NOT_RENDERED

    result = write_files(plan2)
    assert result.skipped == ("legacy.txt",)
    assert dest.read_bytes() == b"legacy\n"


def test_obsolete_locked_path_whose_file_is_gone_is_not_listed(tmp_path):
    dest = tmp_path / "legacy.txt"
    write_files(plan_files(tmp_path, (gf("legacy.txt", b"legacy\n"),), ()))
    dest.unlink()

    plan = plan_files(tmp_path, (), ())
    assert "legacy.txt" not in states_by_path(plan)
    assert "legacy.txt" in plan.lock  # the lock is not a tombstone


def test_deleted_owned_output_is_recreated(tmp_path):
    dest = tmp_path / "settings.json"
    proposed = (gf("settings.json", b"hello\n"),)
    write_files(plan_files(tmp_path, proposed, ()))
    dest.unlink()

    result = write_files(plan_files(tmp_path, proposed, ()))
    assert result.wrote == ("settings.json",)
    assert dest.read_bytes() == b"hello\n"


# ---------------------------------------------------------------------------
# Ignore rules.
# ---------------------------------------------------------------------------


def test_ignored_then_unignored_divergent_output(tmp_path):
    dest = tmp_path / "settings.json"
    dest.write_text("mine\n")
    proposed = (gf("settings.json", b"generated\n"),)

    ignored_plan = plan_files(tmp_path, proposed, ("settings.json",))
    assert states_by_path(ignored_plan)["settings.json"].state == IGNORED
    ignored_result = write_files(ignored_plan)
    assert ignored_result.ignored == ("settings.json",)
    assert dest.read_text() == "mine\n"
    assert read_lock(tmp_path) == {}

    unignored_plan = plan_files(tmp_path, proposed, ())
    assert states_by_path(unignored_plan)["settings.json"].state == YOURS
    unignored_result = write_files(unignored_plan)
    assert unignored_result.yours == ("settings.json",)
    assert dest.read_text() == "mine\n"


def test_ignore_retains_existing_lock_entry(tmp_path):
    proposed = (gf("settings.json", b"generated\n"),)
    write_files(plan_files(tmp_path, proposed, ()))

    plan = plan_files(tmp_path, proposed, ("settings.json",))
    assert states_by_path(plan)["settings.json"].state == IGNORED
    result = write_files(plan)
    assert result.ignored == ("settings.json",)
    assert read_lock(tmp_path)["settings.json"]


def test_ignore_directory_and_negation_semantics(tmp_path):
    """Verified against the real pathspec library, not an assumed glob."""
    proposed = (
        gf(".vscode/settings.json", b"a\n"),
        gf(".vscode/launch.json", b"b\n"),
    )
    ignore = (".vscode/**", "!.vscode/launch.json")

    plan = plan_files(tmp_path, proposed, ignore)
    states = states_by_path(plan)
    assert states[".vscode/settings.json"].state == IGNORED
    assert states[".vscode/launch.json"].state == ABSENT

    result = write_files(plan)
    assert result.ignored == (".vscode/settings.json",)
    assert result.wrote == (".vscode/launch.json",)
    assert not (tmp_path / ".vscode" / "settings.json").exists()
    assert (tmp_path / ".vscode" / "launch.json").read_bytes() == b"b\n"


def test_ignored_absent_path_is_still_shown(tmp_path):
    plan = plan_files(tmp_path, (gf("settings.json", b"a\n"),), ("settings.json",))
    rendered = states_by_path(plan)["settings.json"]
    assert rendered.state == IGNORED
    assert rendered.ow_text == "a\n"
    assert not (tmp_path / "settings.json").exists()


# ---------------------------------------------------------------------------
# Path/lock safety.
# ---------------------------------------------------------------------------


def test_config_toml_destination_is_rejected(tmp_path):
    plan = plan_files(tmp_path, (gf(".ow/config.toml", b"x\n"),), ())
    assert plan.errors
    assert plan.outputs == ()
    result = write_files(plan)
    assert result.failed
    assert not (tmp_path / ".ow" / "config.toml").exists()


def test_rendered_lock_destination_is_rejected(tmp_path):
    plan = plan_files(tmp_path, (gf(".ow/rendered.lock.toml", b"x\n"),), ())
    assert plan.errors
    assert plan.outputs == ()


def test_parent_escaping_output_path_is_rejected(tmp_path):
    plan = plan_files(tmp_path, (gf("../escape.txt", b"x\n"),), ())
    assert plan.errors
    assert plan.outputs == ()
    assert not (tmp_path.parent / "escape.txt").exists()


def test_absolute_output_path_is_rejected(tmp_path):
    plan = plan_files(tmp_path, (gf("/etc/passwd", b"x\n"),), ())
    assert plan.errors
    assert plan.outputs == ()
    assert not (tmp_path / "etc" / "passwd").exists()


def test_retired_locked_local_path_is_allowed_and_not_rendered(tmp_path):
    """`.local/...` is a safe retired workspace path: listed, never errored."""
    ws = tmp_path / "ws"
    ws.mkdir()
    retired = ws / ".local" / "plugin" / "notes.txt"
    retired.parent.mkdir(parents=True)
    retired.write_bytes(b"retired\n")
    lock_dir = ws / ".ow"
    lock_dir.mkdir()
    (lock_dir / "rendered.lock.toml").write_bytes(
        dumps_lock({".local/plugin/notes.txt": "a" * 64})
    )

    plan = plan_files(ws, (), ())
    assert not plan.errors
    rendered = states_by_path(plan)[".local/plugin/notes.txt"]
    assert rendered.state == NOT_RENDERED
    assert rendered.ow_text is None
    assert rendered.your_text == "retired\n"

    result = write_files(plan)
    assert not result.failed
    assert result.skipped == (".local/plugin/notes.txt",)
    assert retired.read_bytes() == b"retired\n"
    assert read_lock(ws)[".local/plugin/notes.txt"] == "a" * 64


def test_lock_entry_escaping_workspace_is_never_read(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"do not touch\n")

    lock_dir = ws / ".ow"
    lock_dir.mkdir()
    (lock_dir / "rendered.lock.toml").write_bytes(dumps_lock({"../outside.txt": "a" * 64}))

    plan = plan_files(ws, (), ())
    assert plan.errors
    assert "../outside.txt" not in states_by_path(plan)
    assert outside.read_bytes() == b"do not touch\n"

    result = write_files(plan)
    assert result.failed
    assert outside.read_bytes() == b"do not touch\n"


def test_directory_at_output_path_blocks_the_whole_plan(tmp_path):
    (tmp_path / "settings.json").mkdir()
    other_dest = tmp_path / "other.txt"
    proposed = (gf("settings.json", b"x\n"), gf("other.txt", b"y\n"))

    plan = plan_files(tmp_path, proposed, ())
    assert plan.errors

    result = write_files(plan)
    assert result.failed
    assert not other_dest.exists()


def test_dangling_symlink_at_output_path_blocks_the_whole_plan(tmp_path):
    link = tmp_path / "settings.json"
    link.symlink_to(tmp_path / "does-not-exist")
    other_dest = tmp_path / "other.txt"
    proposed = (gf("settings.json", b"x\n"), gf("other.txt", b"y\n"))

    plan = plan_files(tmp_path, proposed, ())
    assert plan.errors

    result = write_files(plan)
    assert result.failed
    assert not other_dest.exists()
    assert link.is_symlink()


def test_symlinked_parent_directory_blocks_the_whole_plan(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real_dir)
    other_dest = tmp_path / "other.txt"
    proposed = (gf("linked/settings.json", b"x\n"), gf("other.txt", b"y\n"))

    plan = plan_files(tmp_path, proposed, ())
    assert plan.errors

    result = write_files(plan)
    assert result.failed
    assert not other_dest.exists()
    assert not (real_dir / "settings.json").exists()


def test_symlinked_rendered_lock_is_a_blocking_error(tmp_path):
    real_lock = tmp_path / "real.lock.toml"
    real_lock.write_bytes(dumps_lock({}))
    (tmp_path / ".ow").mkdir()
    (tmp_path / ".ow" / "rendered.lock.toml").symlink_to(real_lock)

    plan = plan_files(tmp_path, (gf("settings.json", b"x\n"),), ())
    assert plan.errors
    assert not (tmp_path / "settings.json").exists()


# ---------------------------------------------------------------------------
# Corrupt lock is a blocking error, never a silent reset.
# ---------------------------------------------------------------------------


def test_corrupt_lock_toml_is_a_blocking_error(tmp_path):
    (tmp_path / ".ow").mkdir()
    (tmp_path / ".ow" / "rendered.lock.toml").write_text("this is not [ valid toml")

    plan = plan_files(tmp_path, (gf("settings.json", b"x\n"),), ())
    assert plan.errors
    assert plan.outputs == ()
    assert plan.lock == {}

    result = write_files(plan)
    assert result.failed
    assert not (tmp_path / "settings.json").exists()


def test_lock_with_non_hex_value_is_a_blocking_error(tmp_path):
    (tmp_path / ".ow").mkdir()
    (tmp_path / ".ow" / "rendered.lock.toml").write_bytes(b"settings.json = \"not-a-hash\"\n")

    plan = plan_files(tmp_path, (gf("settings.json", b"x\n"),), ())
    assert plan.errors


def test_unreadable_lock_is_a_planned_error_not_a_raise(tmp_path):
    lock_path = tmp_path / ".ow" / "rendered.lock.toml"
    lock_path.parent.mkdir()
    lock_path.write_bytes(dumps_lock({}))
    lock_path.chmod(0o000)
    try:
        plan = plan_files(tmp_path, (gf("settings.json", b"x\n"),), ())
    finally:
        lock_path.chmod(0o600)

    assert plan.errors
    assert any(str(lock_path) in message for message in plan.errors)
    assert plan.outputs == ()

    result = write_files(plan)
    assert result.failed
    assert not (tmp_path / "settings.json").exists()


def test_invalid_owignore_pattern_is_a_planned_error_not_a_raise(tmp_path):
    plan = plan_files(tmp_path, (gf("settings.json", b"x\n"),), ("!",))

    assert plan.outputs == () and plan.states == ()
    assert any("owignore" in message and "!" in message for message in plan.errors)

    result = write_files(plan)
    assert result.failed
    assert not (tmp_path / "settings.json").exists()


def test_unreadable_destination_is_a_planned_error(tmp_path):
    dest = tmp_path / "settings.json"
    dest.write_bytes(b"old\n")
    dest.chmod(0o000)
    try:
        plan = plan_files(tmp_path, (gf("settings.json", b"new\n"),), ())
    finally:
        dest.chmod(0o600)

    assert plan.errors
    assert any("settings.json" in message for message in plan.errors)

    result = write_files(plan)
    assert result.failed
    assert dest.read_bytes() == b"old\n"


def test_lock_with_wrong_value_type_is_a_blocking_error(tmp_path):
    (tmp_path / ".ow").mkdir()
    (tmp_path / ".ow" / "rendered.lock.toml").write_bytes(b"settings.json = 1\n")

    plan = plan_files(tmp_path, (gf("settings.json", b"x\n"),), ())
    assert plan.errors


def test_dumps_lock_rejects_a_non_hex_value():
    with pytest.raises(ValueError):
        dumps_lock({"settings.json": "not-a-hash"})


def test_dumps_lock_sorts_keys_and_writes_the_header():
    body = dumps_lock({"b.txt": "1" * 64, "a.txt": "2" * 64})
    text = body.decode("utf-8")
    assert text.startswith("# Managed by ow.\n")
    assert text.index("a.txt") < text.index("b.txt")


def test_read_lock_absent_file_is_empty(tmp_path):
    assert read_lock(tmp_path) == {}


# ---------------------------------------------------------------------------
# Diff data.
# ---------------------------------------------------------------------------


def test_binary_existing_file_has_no_your_text_but_does_not_crash(tmp_path):
    dest = tmp_path / "settings.json"
    dest.write_bytes(b"\xff\xfe\x00\x01binary")
    proposed = (gf("settings.json", b'{"generated": true}\n'),)

    plan = plan_files(tmp_path, proposed, ())
    rendered = states_by_path(plan)["settings.json"]
    assert rendered.state == YOURS
    assert rendered.your_text is None
    assert rendered.ow_text == '{"generated": true}\n'

    result = write_files(plan)
    assert result.yours == ("settings.json",)
    assert dest.read_bytes() == b"\xff\xfe\x00\x01binary"


# ---------------------------------------------------------------------------
# Partial write failure.
# ---------------------------------------------------------------------------


def test_partial_write_failure_keeps_prior_writes_and_their_lock_entries(tmp_path):
    good = tmp_path / "good.txt"
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    bad = locked_dir / "bad.txt"
    proposed = (gf("good.txt", b"ok\n"), gf("locked/bad.txt", b"blocked\n"))

    plan = plan_files(tmp_path, proposed, ())
    assert not plan.errors

    locked_dir.chmod(0o500)  # readable/listable, not writable: mkstemp fails
    try:
        result = write_files(plan)
    finally:
        locked_dir.chmod(0o700)

    assert result.wrote == ("good.txt",)
    assert result.errors
    assert result.failed
    assert good.read_bytes() == b"ok\n"
    assert not bad.exists()

    lock = read_lock(tmp_path)
    assert lock["good.txt"]
    assert "locked/bad.txt" not in lock


# ---------------------------------------------------------------------------
# Derived properties.
# ---------------------------------------------------------------------------


def test_file_gate_failed_true_for_each_blocking_state(tmp_path):
    (tmp_path / "yours.txt").write_text("mine\n")
    proposed = (
        gf("yours.txt", b"generated\n"),
        gf("absent.txt", b"generated\n"),
    )
    plan = plan_files(tmp_path, proposed, ())
    assert plan.file_gate_failed


def test_file_gate_passes_when_everything_is_up_to_date_or_ignored(tmp_path):
    (tmp_path / "settings.json").write_text("same\n")
    proposed = (
        gf("settings.json", b"same\n"),
        gf("ignored.txt", b"whatever\n"),
    )
    plan = plan_files(tmp_path, proposed, ("ignored.txt",))
    assert not plan.file_gate_failed


def test_render_result_failed_is_derived_from_errors():
    assert RenderResult().failed is False
    assert RenderResult(errors=("boom",)).failed is True
