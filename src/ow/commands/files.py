"""`ow files` — the generated files' states, or the diff of the ones that differ.

Read-only: it projects, it never writes. An `error` diagnostic exits 1 for the same reason `ow refresh` would refuse to write; the listing is tab-separated and written to stdout verbatim.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.commands._runtime import open_target
from ow.core import refresh, stacks
from ow.core.refresh import Projection
from ow.core.session import Session
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def _emit_projection(projection: Projection, *, diff: bool) -> int:
    if diff:
        _print_diffs(projection)
    else:
        _print_states(projection)

    if projection.blocked:
        for plugin_name, diag in projection.diagnostics:
            if diag.level == "error":
                term.error(f"{plugin_name}: {diag.text}")
        return 1
    return 0


def run(session: Session, *, diff: bool) -> int:
    """List the files and their states, or diff the ones that differ; 1 when blocked."""
    if session.workspace is None:
        raise UsageError("not inside an ow workspace; pass -w <name|path> or run `ow init`")

    multi = None
    if session.workspace.is_stack:
        parent = stacks.parent_multi_workspace(session.workspace.root)
        if parent is not None:
            loaded = stacks.load_multi_workspace(
                parent, session.plugins, discover_repo_plugins=False
            )
            multi = loaded[0] if loaded is not None else None
    projection = (
        refresh.project(session.workspace, session.plugins, multi=multi)
        if multi is not None
        else refresh.project(session.workspace, session.plugins)
    )
    return _emit_projection(projection, diff=diff)


def run_multi(multi_root: Path, plugins: Plugins, *, diff: bool) -> int:
    """Inspect multi-stack workspace-level generated files (e.g. `<ws>.code-workspace`)."""
    target = open_target(multi_root, "multi", plugins)
    assert target.multi is not None
    projection = stacks.project_multi_workspace_files(target.multi, target.plugins)
    return _emit_projection(projection, diff=diff)


def _print_states(projection: Projection) -> None:
    """One `<path>\\t<state>` line per file, with the reason for yours/skipped."""
    for status in projection.files:
        line = f"{status.path.as_posix()}\t{status.state}"
        if status.state in ("yours", "skipped") and status.reason:
            line += f"\t{status.reason}"
        term.out(line)
    _print_stale(projection)


def _print_diffs(projection: Projection) -> None:
    """A unified diff per file ow would write, plus the stale lock entries.

    Each diff reads current → ow's proposal, so `+` lines are what ow would add; a file ow has no text for gets one line instead.
    """
    for status in projection.files:
        if status.state not in ("absent", "outdated", "yours"):
            continue
        path = status.path.as_posix()
        proposal = _text(status.proposal)
        # An absent file has no current bytes, so every proposal line is an addition.
        current = "" if status.state == "absent" else _text(status.current)
        if proposal is None or current is None:
            term.out(f"{path}: binary")
            continue
        for line in difflib.unified_diff(
            current.splitlines(),
            proposal.splitlines(),
            fromfile=path,
            tofile=f"ow's {path}",
            lineterm="",
        ):
            term.out(line)
    _print_stale(projection)


def _print_stale(projection: Projection) -> None:
    """The lock entries no plugin produces any more; their files stay put."""
    for path in projection.stale_lock:
        term.out(f"{path}\tstale (no longer produced)")


def _text(data: bytes | None) -> str | None:
    """The bytes as text, or None when no line diff exists for them."""
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
