"""`ow ls` — one line per known workspace: name, path, each repo's branch or base (§4).

    <name>\t<path>\t<repo>:<branch or default base>, <repo>:<…>

A repo on a branch shows the branch; a detached one shows its default base, `(unset)`
when it has none. A dead index entry — or one whose workspace cannot be read — shows
`missing` in the repos column: a listing must never become a traceback. Paths print
`~`-relative under `$HOME`, unquoted: this is a listing, not a command line. No known
workspace prints nothing; the exit code is 0 either way.
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
        # Rich expands tabs to tab stops, so the columns go out verbatim.
        sys.stdout.write(f"{path.name}\t{_display(path)}\t{_repos(path, plugins)}\n")
    return 0


def _repos(path: Path, plugins: "Plugins") -> str:
    """The repos column for one workspace: `<repo>:<state>` comma-joined, or `missing`."""
    try:
        session = open_session(str(path), need_workspace=False, plugins=plugins)
    except Exception:
        # Whatever is wrong with that workspace — gone, unreadable, a raising plugin —
        # the listing goes on, and `missing` is the mark (§4).
        return "missing"
    if session.workspace is None:
        return "missing"
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
