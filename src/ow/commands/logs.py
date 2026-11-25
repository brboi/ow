"""`ow logs` — view or follow logs of a detached workspace or stack."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ow.commands._runtime import (
    Target,
    enclosing_multi,
    open_target,
    running_probe,
    scoped_answer,
)
from ow.core import runtime
from ow.core.workspace import resolve_target_root
from ow.errors import OwError, UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins
    from ow.core.stacks import MultiWorkspace
    from ow.core.workspace import Workspace

_NOTHING = "no logs found; start the workspace with `ow run -d` first"


def _coerce_log_entries(answer: object) -> list[tuple[str, Path]]:
    """Accept a plugin's `[(label, Path), ...]` log file list, filtering to existing paths."""
    if not isinstance(answer, list):
        return []
    entries: list[tuple[str, Path]] = []
    for item in answer:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str) and isinstance(item[1], Path):
            if item[1].is_file():
                entries.append((item[0], item[1]))
    return entries


def _stack_entries(plugins: Plugins, ws: Workspace, multi: MultiWorkspace | None) -> list[tuple[str, Path]]:
    """The log files of one workspace or stack: its plugin's `ow_logs`, else its `.ow/logs` file.

    A plugin answer of None or `[]` means "nothing from this plugin": the `.ow/logs` file is used.
    """
    hit = scoped_answer(plugins, "ow_logs", ws, multi)
    entries = _coerce_log_entries(hit[1]) if hit is not None else []
    if entries:
        return entries
    lpath = runtime.log_file(runtime.runtime_root(ws), ws.name)
    return [(ws.name, lpath)] if lpath.is_file() else []


def entries_for(plugins: Plugins, ws: Workspace | None, multi: MultiWorkspace | None) -> list[tuple[str, Path]]:
    """The `(label, path)` log files of a scope; the one `ow logs` shows and `ow run` follows.

    With `ws` set, that workspace or stack (`multi` its multi-stack root, None for a mono-stack
    workspace). With `ws` None, the multi-stack root `multi`: its plugin's whole-root `ow_logs` answer,
    else its stacks' files and the root's own.
    """
    if ws is not None:
        return _stack_entries(plugins, ws, multi)
    assert multi is not None
    hit = scoped_answer(plugins, "ow_logs", None, multi)
    entries = _coerce_log_entries(hit[1]) if hit is not None else []
    if entries:
        return entries
    for stack in multi.stacks:
        entries.extend(_stack_entries(plugins, stack, multi))
    multi_log = runtime.log_file(multi.root, multi.name)
    if multi_log.is_file() and all(path != multi_log for _label, path in entries):
        entries.append((multi.name, multi_log))
    return entries


def _target_entries(target: Target) -> list[tuple[str, Path]]:
    """The log files of an opened target."""
    if target.multi is not None:
        return entries_for(target.plugins, None, target.multi)
    ws = target.workspace
    return entries_for(target.plugins, ws, enclosing_multi(ws, target.plugins))


def run(
    plugins: Plugins,
    *,
    ws_names: Sequence[str] = (),
    follow: bool = False,
    lines: int = 50,
    cwd: Path | None = None,
) -> int:
    """Display or follow logs for one or more workspaces/stacks.

    Following ends when nothing followed is running any more. Exits 1 when there is nothing to show.
    """
    if lines < 1:
        raise UsageError("--lines must be at least 1")

    base_cwd = (cwd or Path.cwd()).resolve()
    raw_names: list[str | None] = list(ws_names) or [None]
    probes = []
    entries: list[tuple[str, Path]] = []

    for raw in raw_names:
        root, kind = resolve_target_root(raw, cwd=base_cwd)
        target = open_target(root, kind, plugins)
        probes.append(running_probe(target.plugins, target.workspaces, target.multi))
        entries.extend(_target_entries(target))

    if not entries:
        raise OwError(_NOTHING)

    return runtime.follow_files(
        entries, lines=lines, follow=follow, running=lambda: any(probe() for probe in probes)
    )
