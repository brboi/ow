"""`ow cd` — print a workspace's absolute path.

A process cannot change its parent shell's directory, so `cd` prints a path
and a shell function — installed by `ow shell-init` — does the actual `cd`.
"""

from ow.utils.resolver import resolve_workspace


def cmd_cd(workspace: str | None = None) -> None:
    """Print a workspace's absolute path, for a shell function to cd into."""
    ws_dir, _ws = resolve_workspace(name=workspace)
    print(ws_dir)
