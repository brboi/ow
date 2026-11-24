"""`ow refresh` — recompute and write generated files for a workspace or multi-stack workspace."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.commands._runtime import open_target
from ow.commands._shared import refresh_workspace
from ow.core import stacks
from ow.core.session import Session

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def run(session: Session) -> int:
    """Refresh the session's workspace (and its parent's `.code-workspace` if it is a stack)."""
    code = refresh_workspace(session)
    if code == 0 and session.workspace is not None and session.workspace.is_stack:
        parent_root = stacks.parent_multi_workspace(session.workspace.root)
        if parent_root is not None:
            loaded = stacks.load_multi_workspace(
                parent_root, session.plugins, discover_repo_plugins=False
            )
            if loaded is not None:
                for wf in stacks.sync_multi_workspace_files(loaded[0], session.plugins):
                    term.out(f"wrote {wf}")
    return code


def run_multi(multi_root: Path, plugins: Plugins) -> int:
    """Refresh every stack under multi-stack workspace `multi_root` and sync workspace files."""
    target = open_target(multi_root, "multi", plugins)
    assert target.multi is not None
    exit_code = 0
    for sess in target.sessions:
        assert sess.workspace is not None
        term.out(f"== {sess.workspace.name} ==")
        exit_code = max(exit_code, refresh_workspace(sess, multi=target.multi))
    if exit_code == 0:
        for wf in stacks.sync_multi_workspace_files(target.multi, target.plugins):
            term.out(f"wrote {wf}")
    return exit_code
