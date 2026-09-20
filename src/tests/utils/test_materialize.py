"""Copy-once local seeds: what lands in a workspace's `.local`, and what refuses to.

`seed_local_files` takes both trees as arguments, so these tests build them
under tmp_path and never touch the XDG locations.
"""

import os
import stat
from pathlib import Path

from ow.utils.materialize import SeedResult, seed_local_files


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "local"
    source.mkdir()
    return source


def _seed(source: Path, relative: str, data: bytes = b"stock\n", mode: int = 0o644) -> Path:
    path = source / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)
    return path


def test_missing_files_are_copied_into_local_with_their_mode(tmp_path):
    source = _source(tmp_path)
    _seed(source, "addons/foo/models.py", b"class Foo:\n")
    _seed(source, "script.sh", b"#!/bin/sh\n", mode=0o755)
    root = tmp_path / "ws"
    (root / ".local" / "addons").mkdir(parents=True)  # an existing ancestor directory

    result = seed_local_files(source, root)

    assert result == SeedResult(
        copied=(".local/addons/foo/models.py", ".local/script.sh"), kept=(), errors=()
    )
    assert (root / ".local/addons/foo/models.py").read_bytes() == b"class Foo:\n"
    assert stat.S_IMODE((root / ".local/script.sh").stat().st_mode) == 0o755


def test_special_permission_bits_do_not_survive_the_copy(tmp_path):
    source = _source(tmp_path)
    _seed(source, "tool.sh", b"#!/bin/sh\n", mode=0o4755)
    destination = tmp_path / "ws" / ".local/tool.sh"

    seed_local_files(source, tmp_path / "ws")

    assert stat.S_IMODE(destination.stat().st_mode) == 0o755


def test_existing_destination_files_are_kept_even_when_their_content_differs(tmp_path):
    source = _source(tmp_path)
    _seed(source, "settings.json", b"stock\n")
    root = tmp_path / "ws"
    (root / ".local").mkdir(parents=True)
    (root / ".local/settings.json").write_bytes(b"mine\n")

    result = seed_local_files(source, root)

    assert result == SeedResult(copied=(), kept=(".local/settings.json",), errors=())
    assert (root / ".local/settings.json").read_bytes() == b"mine\n"


def test_rerunning_only_adds_what_is_still_missing(tmp_path):
    source = _source(tmp_path)
    _seed(source, "first.py", b"one\n")
    root = tmp_path / "ws"
    seed_local_files(source, root)

    _seed(source, "second.py", b"two\n")
    result = seed_local_files(source, root)

    assert result == SeedResult(copied=(".local/second.py",), kept=(".local/first.py",), errors=())
    assert (root / ".local/second.py").read_bytes() == b"two\n"


def test_a_directory_without_files_seeds_nothing(tmp_path):
    source = _source(tmp_path)
    (source / "empty").mkdir()
    root = tmp_path / "ws"

    result = seed_local_files(source, root)

    assert result == SeedResult()
    assert not root.exists()


def test_a_symlink_in_the_source_stops_the_whole_copy(tmp_path):
    """Preflight is whole-tree: one unusable entry copies nothing at all."""
    source = _source(tmp_path)
    _seed(source, "addons/real.py", b"x\n")
    (source / "addons" / "link.py").symlink_to(source / "addons" / "real.py")
    root = tmp_path / "ws"

    result = seed_local_files(source, root)

    assert result == SeedResult(errors=(f"addons/link.py is a symlink in {source}",))
    assert not (root / ".local").exists()


def test_a_symlinked_directory_in_the_source_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "elsewhere.py").write_bytes(b"not ours\n")
    source = _source(tmp_path)
    (source / "pkg").symlink_to(outside, target_is_directory=True)

    result = seed_local_files(source, tmp_path / "ws")

    assert result == SeedResult(errors=(f"pkg is a symlink in {source}",))


def test_a_non_regular_source_entry_is_rejected(tmp_path):
    source = _source(tmp_path)
    os.mkfifo(source / "pipe")

    result = seed_local_files(source, tmp_path / "ws")

    assert result == SeedResult(errors=(f"pipe is not a regular file in {source}",))


def test_a_symlinked_local_directory_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    source = _source(tmp_path)
    _seed(source, "addons/foo/models.py", b"class Foo:\n")
    root = tmp_path / "ws"
    root.mkdir()
    (root / ".local").symlink_to(elsewhere, target_is_directory=True)

    result = seed_local_files(source, root)

    assert result == SeedResult(errors=(".local is a symlink",))
    assert list(elsewhere.iterdir()) == []


def test_a_symlink_inside_the_destination_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    source = _source(tmp_path)
    _seed(source, "addons/foo/models.py", b"class Foo:\n")
    root = tmp_path / "ws"
    (root / ".local").mkdir(parents=True)
    (root / ".local/addons").symlink_to(elsewhere, target_is_directory=True)

    result = seed_local_files(source, root)

    assert result == SeedResult(errors=(".local/addons is a symlink",))
    assert list(elsewhere.iterdir()) == []


def test_a_destination_that_is_already_a_directory_is_refused(tmp_path):
    """A directory is only ever an ancestor, never a file replacement."""
    source = _source(tmp_path)
    _seed(source, "addons")
    root = tmp_path / "ws"
    (root / ".local/addons").mkdir(parents=True)

    result = seed_local_files(source, root)

    assert result == SeedResult(errors=(".local/addons is a directory",))


def test_a_destination_ancestor_that_is_a_file_is_refused(tmp_path):
    source = _source(tmp_path)
    _seed(source, "addons/foo/models.py", b"class Foo:\n")
    root = tmp_path / "ws"
    (root / ".local").mkdir(parents=True)
    (root / ".local/addons").write_bytes(b"not a directory\n")

    result = seed_local_files(source, root)

    assert result == SeedResult(errors=(".local/addons is not a directory",))


def test_an_absent_source_is_simply_no_seeds(tmp_path):
    result = seed_local_files(tmp_path / "local", tmp_path / "ws")

    assert result == SeedResult()
    assert not (tmp_path / "ws").exists()


def test_a_source_that_is_not_a_directory_is_an_error(tmp_path):
    source = tmp_path / "local"
    source.write_bytes(b"not a tree\n")

    result = seed_local_files(source, tmp_path / "ws")

    assert result == SeedResult(errors=(f"seed source {source} is not a directory",))