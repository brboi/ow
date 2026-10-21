"""`ow refresh` and `ow files` scenarios: the outputs, ownership and the lock.

The workspace is built here with plain git, `write_workspace` and the index —
never with `ow init` — so these scenarios stand alone.
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from ow.core.config import write_workspace
from ow.core.index import add as index_add
from ow.core.paths import repos_dir
from ow.core.refresh import dumps_lock, read_lock

REPO = "community"
BASE = "18.0"

# Every output an Odoo-shaped workspace gets, in plugin tuple order.
NINE = (
    "mise/conf.d/00-ow.toml",
    "odoorc",
    "odools.toml",
    "pyrightconfig.json",
    "requirements-dev.txt",
    ".vscode/settings.json",
    ".vscode/launch.json",
    ".zed/settings.json",
    ".zed/debug.json",
)


def workspace(lab, files: dict[str, str], *, name: str = "voip") -> Path:
    """A real workspace: a bare repo, a worktree, a config file and an index entry."""
    origin = lab.remote(REPO, {BASE: files}, head=BASE)
    bare = repos_dir() / f"{REPO}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    lab.git(bare.parent, "init", "--bare", str(bare))
    lab.git(bare, "remote", "add", "origin", str(origin))
    lab.git(bare, "fetch", "origin")

    root = lab.ws(name)
    root.mkdir(parents=True, exist_ok=True)
    lab.git(bare, "worktree", "add", "--detach", str(root / REPO), f"origin/{BASE}")

    write_workspace(root / ".ow" / "config.toml", {REPO: f"origin/{BASE}"}, {}, declared={})
    index_add(root)
    return root


def test_refresh_writes_the_nine_outputs(lab):
    root = workspace(lab, lab.odoo_files())

    result = lab.ow("refresh", cwd=root)
    assert result.exit_code == 0
    assert result.stdout.splitlines() == [f"wrote {path}" for path in NINE]
    assert "warning: mise ≥ 2026.8.13 not found" in result.stderr

    for path in NINE:
        assert (root / path).is_file()
    assert stat.S_IMODE((root / "odoorc").stat().st_mode) == 0o600

    # Everything matches the lock now, so a second refresh writes nothing.
    again = lab.ow("refresh", cwd=root)
    assert again.exit_code == 0
    assert again.stdout == ""


def test_files_states_yours_diff_and_untouched_refresh(lab):
    root = workspace(lab, lab.odoo_files())
    assert lab.ow("refresh", cwd=root).exit_code == 0

    listing = lab.ow("files", cwd=root)
    assert listing.exit_code == 0
    assert listing.stdout.splitlines() == [f"{path}\tup to date" for path in NINE]

    odoorc = root / "odoorc"
    odoorc.write_text(odoorc.read_text() + "# hand edit\n")

    listing = lab.ow("files", cwd=root)
    assert listing.exit_code == 0
    assert listing.stdout.splitlines() == [
        "mise/conf.d/00-ow.toml\tup to date",
        "odoorc\tyours",
        *[f"{path}\tup to date" for path in NINE[2:]],
    ]

    diff = lab.ow("files", "--diff", cwd=root)
    assert diff.exit_code == 0
    lines = diff.stdout.splitlines()
    assert lines[0] == "--- ow's odoorc"
    assert lines[1] == "+++ odoorc"
    assert lines[2].startswith("@@ ")
    assert [line for line in lines[3:] if line.startswith("+")] == ["+# hand edit"]
    assert lines[-1] == "+# hand edit"

    # A hand-edited file is yours: the next refresh leaves its bytes and its
    # mtime alone and says how to get ow's version back.
    before = (odoorc.stat().st_mtime_ns, odoorc.read_bytes())
    second = lab.ow("refresh", cwd=root)
    assert second.exit_code == 0
    assert second.stdout == ""
    assert (
        "warning: odoorc: not overwritten (yours); delete it and run `ow refresh` "
        "to get ow's version back" in second.stderr
    )
    assert (odoorc.stat().st_mtime_ns, odoorc.read_bytes()) == before


def test_files_diff_of_a_workspace_nothing_was_written_to(lab):
    root = workspace(lab, lab.odoo_files())

    result = lab.ow("files", "--diff", cwd=root)
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    # One block per absent file, ow's proposal against an empty file, so every
    # body line of every hunk is one ow would add.
    assert [line for line in lines if line.startswith("--- ")] == [
        f"--- ow's {path}" for path in NINE
    ]
    assert [line for line in lines if line.startswith("+++ ")] == [f"+++ {path}" for path in NINE]
    body = [line for line in lines if not line.startswith(("--- ", "+++ ", "@@ "))]
    assert body and all(line.startswith("-") for line in body)
    assert "@@ -1," in result.stdout and " +0,0 @@" in result.stdout


def test_files_diff_reports_a_binary_file(lab):
    root = workspace(lab, lab.odoo_files())
    assert lab.ow("refresh", cwd=root).exit_code == 0

    (root / "odoorc").write_bytes(b"\xff\xfe\x00 not text")

    result = lab.ow("files", "--diff", cwd=root)
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["odoorc: binary"]


def test_files_stale_lock_entry_and_the_refresh_that_drops_it(lab):
    root = workspace(lab, lab.odoo_files())
    assert lab.ow("refresh", cwd=root).exit_code == 0

    # A lock entry no plugin produces any more, and the file it was for.
    lock = read_lock(root)
    lock["old/thing.txt"] = hashlib.sha256(b"gone\n").hexdigest()

    (root / ".ow" / "rendered.lock.toml").write_bytes(dumps_lock(lock))
    (root / "old").mkdir()
    (root / "old" / "thing.txt").write_text("gone\n")

    listing = lab.ow("files", cwd=root)
    assert listing.exit_code == 0
    assert listing.stdout.splitlines()[-1] == "old/thing.txt\tstale (no longer produced)"

    result = lab.ow("refresh", cwd=root)
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "old/thing.txt" not in read_lock(root)
    # The file itself stays: ow never deletes a workspace file.
    assert (root / "old" / "thing.txt").read_text() == "gone\n"
