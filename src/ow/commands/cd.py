"""`ow cd [<ws>]` — print a workspace's path (§4)."""

from __future__ import annotations

from ow import term
from ow.core.session import Session


def run(session: Session) -> int:
    """Print the session workspace's root, absolute: the shell wrapper `cd`s to it.

    `ow shell-init` defines a shell function that reads this output and changes the
    directory, so the path must be the real thing, never `~`-relative or quoted.
    """
    assert session.workspace is not None  # cli opens `<ws>` (or the cwd) with need_workspace=True
    term.out(str(session.workspace.root))
    return 0
