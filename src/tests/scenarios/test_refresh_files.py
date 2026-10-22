"""`ow refresh` and `ow files` scenarios: the outputs, ownership and the lock.

The workspace is built here with plain git, `write_workspace` and the index —
never with `ow init` — so these scenarios stand alone.
"""

from __future__ import annotations

import hashlib
import stat

from ow.core.refresh import dumps_lock, read_lock
from tests.conftest import workspace

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


def test_refresh_writes_the_nine_outputs(lab):
    root = workspace(lab, branches={BASE: lab.odoo_files()})

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
    root = workspace(lab, branches={BASE: lab.odoo_files()})
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
    assert lines[0] == "--- odoorc"
    assert lines[1] == "+++ ow's odoorc"
    assert lines[2].startswith("@@ ")
    # The diff reads current -> ow's proposal, so the hand edit is a line ow would drop.
    assert [line for line in lines[3:] if line.startswith("-")] == ["-# hand edit"]
    assert lines[-1] == "-# hand edit"

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
    root = workspace(lab, branches={BASE: lab.odoo_files()})

    result = lab.ow("files", "--diff", cwd=root)
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    # One block per absent file, an empty current file against ow's proposal, so
    # every body line of every hunk is one ow would add.
    assert [line for line in lines if line.startswith("--- ")] == [f"--- {path}" for path in NINE]
    assert [line for line in lines if line.startswith("+++ ")] == [
        f"+++ ow's {path}" for path in NINE
    ]
    body = [line for line in lines if not line.startswith(("--- ", "+++ ", "@@ "))]
    assert body and all(line.startswith("+") for line in body)
    assert "@@ -0,0" in result.stdout and " +1," in result.stdout


def test_files_diff_reports_a_binary_file(lab):
    root = workspace(lab, branches={BASE: lab.odoo_files()})
    assert lab.ow("refresh", cwd=root).exit_code == 0

    (root / "odoorc").write_bytes(b"\xff\xfe\x00 not text")

    result = lab.ow("files", "--diff", cwd=root)
    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["odoorc: binary"]


def test_files_stale_lock_entry_and_the_refresh_that_drops_it(lab):
    root = workspace(lab, branches={BASE: lab.odoo_files()})
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


def test_files_names_the_error_that_blocks_it(lab, monkeypatch, capsys):
    """`ow files` exits 1 with the error diagnostic, not a bare listing."""
    from ow.commands import files as files_cmd
    from ow.core.plugins import Diagnostic, load_plugins
    from ow.core.refresh import Projection
    from ow.core.session import open_session

    root = workspace(lab, branches={BASE: lab.odoo_files()})
    session = open_session(str(root), need_workspace=True, plugins=load_plugins())
    blocked = Projection(
        files=(),
        stale_lock=(),
        diagnostics=(("odoo", Diagnostic("error", "bad setting")),),
        blocked=True,
    )
    monkeypatch.setattr(files_cmd.refresh, "project", lambda ws, plugins: blocked)

    assert files_cmd.run(session, diff=False) == 1
    assert "error: odoo: bad setting" in capsys.readouterr().err
