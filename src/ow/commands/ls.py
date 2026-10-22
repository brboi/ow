"""`ow ls` — one line per known workspace: name, path, then each repo's branch or base.

A dead index entry prints `missing`; an existing but unreadable workspace `unreadable`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from ow.core import index
from ow.core.session import open_session

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def run(plugins: "Plugins") -> int:
    """List the index's workspaces, in index order; dead entries degrade to `missing`."""
    for path in index.read():
        sys.stdout.write(f"{path.name}\t{_display(path)}\t{_repos(path, plugins)}\n")
    return 0


def _repos(path: Path, plugins: "Plugins") -> str:
    """The repos column for one workspace: `<repo>:<state>` comma-joined, `missing`, or `unreadable`."""
    if not path.exists():
        # A dead index entry: the path is gone.
        return "missing"
    try:
        session = open_session(str(path), need_workspace=False, plugins=plugins)
    except Exception:
        # The path is there but its config cannot be read.
        return "unreadable"
    if session.workspace is None:
        return "unreadable"
    entries = []
    for location in session.locations:
        state = location.branch or location.default_base or "(unset)"
        entries.append(f"{location.name}:{state}")
    return ", ".join(entries)


def _display(path: Path) -> str:
    """`~/…` under `$HOME`, else absolute: a listing column, so no shell quoting."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)
