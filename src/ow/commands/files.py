"""`ow files` — see the files ow generates for a workspace, and diff them.

Read-only: `inspect_workspace` is the whole implementation, and nothing
here writes a byte, fetches a ref, or migrates a config. A workspace still
on schema 1 is inspected as-is, its pending conversion surfaced alongside
the file states rather than converted.
"""

import difflib
import sys

from ow.utils.config import Config
from ow.utils.display import console, err_console
from ow.utils.render import ABSENT, OUTDATED, RenderedFile, YOURS
from ow.utils.resolver import resolve_workspace
from ow.utils.workspace import inspect_workspace


def _list(states: list[RenderedFile]) -> None:
    if not states:
        console.print("nothing to render.")
        return
    width = max(len(s.path) for s in states)
    for s in states:
        console.print(f"{s.path.ljust(width)}  {s.state}", markup=False)


def _binary_note(s: RenderedFile) -> str:
    """One line for a file whose bytes are not text, so no line diff exists."""
    if s.state == ABSENT:
        return f"{s.path}: binary file, ow would write it (absent)"
    if s.ow_text is None:
        return f"{s.path}: binary file, differs from yours"
    return f"{s.path}: yours is not text, ow would write text"


def _diff(states: list[RenderedFile]) -> None:
    """What ow would change, line by line wherever both sides are text.

    An output not there yet is diffed against `/dev/null`, the file the
    addition would be made to.
    """
    diffable = [s for s in states if s.state in (YOURS, OUTDATED, ABSENT)]
    if not diffable:
        console.print("nothing differs from what ow would write.")
        return
    for s in diffable:
        if s.state == ABSENT:
            yours, fromfile = "", "/dev/null"
        else:
            yours, fromfile = s.your_text, f"{s.path} (yours)"
        if s.ow_text is None or yours is None:
            console.print(_binary_note(s), markup=False)
            continue
        lines = difflib.unified_diff(
            yours.splitlines(keepends=True),
            s.ow_text.splitlines(keepends=True),
            fromfile=fromfile,
            tofile=f"{s.path} (ow)",
        )
        for line in lines:
            console.print(line, end="" if line.endswith("\n") else "\n", markup=False)


def cmd_files(config: Config, workspace: str | None = None, *, show_diff: bool = False) -> None:
    """Show the files ow manages in the workspace, or diff the ones that differ."""
    ws_dir, ws = resolve_workspace(name=workspace)
    plan = inspect_workspace(config, ws, ws_dir)

    for line in plan.errors:
        err_console.print(f"Error: {line}", markup=False)
    for line in plan.gate_blockers:
        err_console.print(f"Error: {line}", markup=False)
    for line in plan.warnings:
        err_console.print(line, markup=False)

    states = list(plan.states)
    if show_diff:
        _diff(states)
        sys.exit(1 if plan.file_gate_failed else 0)

    _list(states)
    if plan.errors or plan.gate_blockers:
        sys.exit(1)
