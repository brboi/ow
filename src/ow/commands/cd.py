"""`ow cd [<ws>]` — print a workspace or stack path."""
from __future__ import annotations

from ow.core.workspace import resolve_target_root


def run(ws: str | None) -> int:
    """Print the resolved workspace or stack path: the shell wrapper changes directory to it."""
    root, _kind = resolve_target_root(ws)
    print(root)
    return 0
