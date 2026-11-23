"""`ow stop` — stop detached workspaces/stacks and/or global shared services."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.core import index, runtime, stacks
from ow.core.plugins import RunCommand
from ow.core.session import open_session
from ow.core.workspace import resolve_target_root
from ow.errors import PluginError, UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins
    from ow.core.workspace import Workspace


def _exec_command(cmd: RunCommand) -> int:
    env = dict(os.environ)
    env.update(cmd.env)
    try:
        proc = subprocess.run(cmd.argv, cwd=cmd.cwd, env=env)
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        raise UsageError(f"failed to execute {cmd.argv[0]!r}: {exc}") from exc
    if proc.returncode < 0:
        return 128 + abs(proc.returncode)
    return proc.returncode


def _plugin_running(ws: Workspace, plugins: Plugins) -> list[str]:
    found: list[str] = []
    for _name, answer in plugins.call("ow_running", ws=ws, errors="collect"):
        if isinstance(answer, list):
            found.extend(str(item) for item in answer if isinstance(item, str) and item)
    return found


def _stop_global_services(plugins: Plugins) -> int:
    """Invoke `ow_stop_services` across enabled plugins."""
    stopped_any = False
    for plugin_name, result in plugins.call("ow_stop_services", ws=None, errors="collect"):
        if isinstance(result, PluginError):
            term.warn(f"{plugin_name}: {result.cause}")
        elif isinstance(result, list):
            for line in result:
                if isinstance(line, str) and line:
                    term.out(f"# {line}")
                    stopped_any = True
    if not stopped_any:
        term.out("# services: none running")
    return 0


def _handle_stop_answer(answer: object) -> tuple[bool, int]:
    if isinstance(answer, RunCommand):
        rc = _exec_command(answer)
        return True, rc
    if isinstance(answer, list):
        printed = False
        for line in answer:
            if isinstance(line, str) and line:
                term.out(f"# {line}")
                printed = True
        return printed, 0
    return False, 0


def _stop_workspace(
    ws: Workspace, plugins: Plugins, *, quiet_if_idle: bool = False
) -> tuple[bool, int]:
    """Stop detached processes and plugin-managed services for a single workspace or stack."""
    stopped = False
    exit_code = 0

    proc = runtime.running_for_workspace(ws)
    if proc is not None:
        if runtime.stop_process(runtime.runtime_root(ws), proc):
            term.out(f"# stopped {ws.name} (pid {proc.pid})")
            stopped = True

    should_call_plugin = (not quiet_if_idle) or bool(_plugin_running(ws, plugins))
    if should_call_plugin:
        hit = plugins.first_with_name("ow_stop", ws=ws)
        if hit is not None:
            plugin_stopped, rc = _handle_stop_answer(hit[1])
            stopped = stopped or plugin_stopped
            if rc != 0 and exit_code == 0:
                exit_code = rc

    if not stopped and not quiet_if_idle:
        term.out(f"# {ws.name}: not running")
    return stopped, exit_code


def _stop_multi(
    multi: stacks.MultiWorkspace, plugins: Plugins, *, quiet_if_idle: bool = False
) -> tuple[bool, int]:
    """Stop detached processes and plugin-managed services for a multi-stack workspace."""
    stopped = False
    exit_code = 0

    for proc in runtime.list_running(multi.root):
        if runtime.stop_process(multi.root, proc):
            term.out(f"# stopped {multi.name}/{proc.label} (pid {proc.pid})")
            stopped = True

    ws_ctx = multi.stacks[0] if multi.stacks else None
    any_plugin_running = any(_plugin_running(ws, plugins) for ws in multi.stacks)
    multi_hit = None
    if (not quiet_if_idle) or any_plugin_running:
        multi_hit = plugins.first_with_name("ow_multi_stop", ws=ws_ctx, multi=multi)
    if multi_hit is not None:
        plugin_stopped, rc = _handle_stop_answer(multi_hit[1])
        stopped = stopped or plugin_stopped
        if rc != 0 and exit_code == 0:
            exit_code = rc
    else:
        for ws in multi.stacks:
            if quiet_if_idle and not _plugin_running(ws, plugins):
                continue
            hit = plugins.first_with_name("ow_stop", ws=ws)
            if hit is not None:
                plugin_stopped, rc = _handle_stop_answer(hit[1])
                stopped = stopped or plugin_stopped
                if rc != 0 and exit_code == 0:
                    exit_code = rc

    if not stopped and not quiet_if_idle:
        term.out(f"# {multi.name}: not running")
    return stopped, exit_code


def _open_ws(path: Path, plugins: Plugins) -> tuple[Workspace, Plugins]:
    session = open_session(str(path), need_workspace=True, plugins=plugins, cwd=path)
    for warning in session.warnings:
        term.warn(warning)
    assert session.workspace is not None
    return session.workspace, session.plugins


def run(
    plugins: Plugins,
    *,
    targets: Sequence[str] = (),
    workspace: str | None = None,
    services: bool = False,
    all_workspaces: bool = False,
    cwd: Path | None = None,
) -> int:
    """Stop one or more running workspaces/stacks, global services (`--services`), or everything (`--all`)."""
    if targets and workspace is not None:
        raise UsageError("pass either TARGET(s) or -w/--workspace, not both")
    if all_workspaces and (targets or workspace is not None):
        raise UsageError("cannot combine --all with TARGET(s) or -w/--workspace")

    if all_workspaces:
        exit_code = 0
        stopped_any = False
        for root in index.read():
            if not root.is_dir():
                continue
            loaded = stacks.load_multi_workspace(root, plugins)
            if loaded is not None:
                multi, sessions = loaded
                eff_plugins = sessions[0].plugins if sessions else plugins
                did_stop, rc = _stop_multi(multi, eff_plugins, quiet_if_idle=True)
                stopped_any = stopped_any or did_stop
                if rc != 0 and exit_code == 0:
                    exit_code = rc
            else:
                try:
                    ws, eff_plugins = _open_ws(root, plugins)
                except Exception:
                    continue
                did_stop, rc = _stop_workspace(ws, eff_plugins, quiet_if_idle=True)
                stopped_any = stopped_any or did_stop
                if rc != 0 and exit_code == 0:
                    exit_code = rc
        if not stopped_any:
            term.out("# workspaces: none running")
        svc_rc = _stop_global_services(plugins)
        return exit_code or svc_rc

    if services and not targets and workspace is None:
        return _stop_global_services(plugins)

    base_cwd = (cwd or Path.cwd()).resolve()
    raw_targets: list[str | None] = list(targets) if targets else [workspace]
    exit_code = 0

    for raw in raw_targets:
        root, kind = resolve_target_root(raw, cwd=base_cwd)
        if kind == "workspace":
            ws, eff_plugins = _open_ws(root, plugins)
            _stopped, rc = _stop_workspace(ws, eff_plugins, quiet_if_idle=False)
        else:
            loaded = stacks.load_multi_workspace(root, plugins)
            if loaded is None:
                raise UsageError(f"no valid stacks found in {root}")
            multi, sessions = loaded
            eff_plugins = sessions[0].plugins if sessions else plugins
            for sess in sessions:
                for warning in sess.warnings:
                    term.warn(warning)
            _stopped, rc = _stop_multi(multi, eff_plugins, quiet_if_idle=False)
        if rc != 0 and exit_code == 0:
            exit_code = rc

    if services:
        svc_rc = _stop_global_services(plugins)
        if svc_rc != 0 and exit_code == 0:
            exit_code = svc_rc

    return exit_code
