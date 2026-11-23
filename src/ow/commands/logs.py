"""`ow logs` — view or follow logs of a detached workspace or stack."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.core import runtime, stacks
from ow.core.plugins import RunCommand
from ow.core.session import open_session
from ow.core.workspace import resolve_target_root
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins
    from ow.core.workspace import Workspace


def _exec_logs_command(cmd: RunCommand) -> int:
    env = dict(os.environ)
    env.update(cmd.env)
    try:
        proc = subprocess.run(cmd.argv, cwd=cmd.cwd, env=env)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        raise UsageError(f"failed to execute {cmd.argv[0]!r}: {exc}") from exc
    if proc.returncode < 0:
        return 128 + abs(proc.returncode)
    return proc.returncode


def _open_ws(path: Path, plugins: Plugins) -> tuple[Workspace, Plugins]:
    session = open_session(str(path), need_workspace=True, plugins=plugins, cwd=path)
    for warning in session.warnings:
        term.warn(warning)
    assert session.workspace is not None
    return session.workspace, session.plugins


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


def run(
    plugins: Plugins,
    *,
    targets: Sequence[str] = (),
    workspace: str | None = None,
    follow: bool = False,
    lines: int = 50,
    cwd: Path | None = None,
) -> int:
    """Display or follow logs for one or more workspaces/stacks."""
    if targets and workspace is not None:
        raise UsageError("pass either TARGET(s) or -w/--workspace, not both")
    if lines < 1:
        raise UsageError("--lines must be at least 1")

    base_cwd = (cwd or Path.cwd()).resolve()
    raw_targets: list[str | None] = list(targets) if targets else [workspace]
    entries: list[tuple[str, Path]] = []

    for raw in raw_targets:
        root, kind = resolve_target_root(raw, cwd=base_cwd)
        if kind == "workspace":
            ws, eff_plugins = _open_ws(root, plugins)
            if len(raw_targets) == 1:
                hit = eff_plugins.first_with_name("ow_logs", ws=ws, follow=follow, lines=lines)
                if hit is not None:
                    if isinstance(hit[1], RunCommand):
                        return _exec_logs_command(hit[1])
                    plugin_entries = _coerce_log_entries(hit[1])
                    if plugin_entries is not None:
                        if not plugin_entries:
                            raise UsageError("no logs found; start the workspace with `ow run -d` first")
                        return runtime.follow_files(plugin_entries, lines=lines, follow=follow)
            lpath = runtime.log_file(runtime.runtime_root(ws), ws.name)
            if lpath.is_file():
                entries.append((ws.name, lpath))
        else:
            loaded = stacks.load_multi_workspace(root, plugins)
            if loaded is None:
                raise UsageError(f"no valid stacks found in {root}")
            multi, sessions = loaded
            eff_plugins = sessions[0].plugins if sessions else plugins
            for sess in sessions:
                for warning in sess.warnings:
                    term.warn(warning)
            if len(raw_targets) == 1:
                ws_ctx = multi.stacks[0] if multi.stacks else None
                hit = eff_plugins.first_with_name(
                    "ow_multi_logs", ws=ws_ctx, multi=multi, follow=follow, lines=lines
                )
                if hit is not None:
                    if isinstance(hit[1], RunCommand):
                        return _exec_logs_command(hit[1])
                    plugin_entries = _coerce_log_entries(hit[1])
                    if plugin_entries is not None:
                        if not plugin_entries:
                            raise UsageError("no logs found; start the workspace with `ow run -d` first")
                        return runtime.follow_files(plugin_entries, lines=lines, follow=follow)
            for ws in multi.stacks:
                lpath = runtime.log_file(multi.root, ws.name)
                if lpath.is_file():
                    entries.append((ws.name, lpath))
            multi_log = runtime.log_file(multi.root, multi.name)
            if multi_log.is_file() and all(p != multi_log for _lbl, p in entries):
                entries.append((multi.name, multi_log))

    if not entries:
        raise UsageError("no logs found; start the workspace with `ow run -d` first")

    return runtime.follow_files(entries, lines=lines, follow=follow)
