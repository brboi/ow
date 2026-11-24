"""`ow logs` — view or follow logs of a detached workspace or stack."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ow.commands._runtime import Target, open_target, plugin_running
from ow.core import runtime
from ow.core.workspace import resolve_target_root
from ow.errors import OwError, UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins

_NOTHING = "no logs found; start the workspace with `ow run -d` first"


def _coerce_log_entries(answer: object) -> list[tuple[str, Path]] | None:
    """Accept a plugin's `[(label, Path), ...]` log file list, filtering to existing paths."""
    if not isinstance(answer, list):
        return None
    entries: list[tuple[str, Path]] = []
    for item in answer:
        if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str) and isinstance(item[1], Path):
            if item[1].is_file():
                entries.append((item[0], item[1]))
    return entries


def _any_running(targets: Sequence[Target]) -> Callable[[], bool]:
    """Whether any followed workspace still has a detached process or a plugin-reported one.

    Polled while following, so a failing `ow_running` is warned about on the first poll only.
    """
    first_poll = True

    def running() -> bool:
        nonlocal first_poll
        report, first_poll = first_poll, False
        for target in targets:
            if target.multi is not None and runtime.list_running(target.multi.root):
                return True
            for ws in target.workspaces:
                if runtime.running_for_workspace(ws) is not None:
                    return True
                if plugin_running(ws, target.plugins, report=report):
                    return True
        return False

    return running


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
    opened: list[Target] = []
    entries: list[tuple[str, Path]] = []

    for raw in raw_names:
        root, kind = resolve_target_root(raw, cwd=base_cwd)
        target = open_target(root, kind, plugins)
        opened.append(target)

        if len(raw_names) == 1:
            if target.multi is None:
                hit = target.plugins.first_with_name("ow_logs", ws=target.workspace)
            else:
                hit = target.plugins.first_with_name(
                    "ow_multi_logs", ws=target.multi.stacks[0], multi=target.multi
                )
            plugin_entries = _coerce_log_entries(hit[1]) if hit is not None else None
            if plugin_entries:  # None or [] mean "nothing from this plugin": use `.ow/logs`
                return runtime.follow_files(
                    plugin_entries, lines=lines, follow=follow, running=_any_running(opened)
                )

        if target.multi is None:
            ws = target.workspace
            lpath = runtime.log_file(runtime.runtime_root(ws), ws.name)
            if lpath.is_file():
                entries.append((ws.name, lpath))
        else:
            for ws in target.multi.stacks:
                lpath = runtime.log_file(target.multi.root, ws.name)
                if lpath.is_file():
                    entries.append((ws.name, lpath))
            multi_log = runtime.log_file(target.multi.root, target.multi.name)
            if multi_log.is_file() and all(p != multi_log for _lbl, p in entries):
                entries.append((target.multi.name, multi_log))

    if not entries:
        raise OwError(_NOTHING)

    return runtime.follow_files(entries, lines=lines, follow=follow, running=_any_running(opened))
