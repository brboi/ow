"""`ow cd [<ws>]` — print a workspace's path."""
from __future__ import annotations

from ow.core.session import resolve_root


def run(ws: str | None) -> int:
    """Print the workspace path: the shell wrapper changes directory to it."""
    print(resolve_root(ws))
    return 0
