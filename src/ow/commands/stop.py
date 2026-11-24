"""`ow stop` — stop detached workspaces/stacks and/or global shared services."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.commands._runtime import open_target, plugin_running, run_command
from ow.core import index, runtime, stacks
from ow.core.plugins import RunCommand
from ow.core.workspace import resolve_target_root
from ow.errors import PluginError, UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins
    from ow.core.workspace import Workspace


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
        rc = run_command(answer)
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

    should_call_plugin = (not quiet_if_idle) or bool(plugin_running(ws, plugins))
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
    any_plugin_running = any(plugin_running(ws, plugins) for ws in multi.stacks)
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
            if quiet_if_idle and not plugin_running(ws, plugins):
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


def run(
    plugins: Plugins,
    *,
    ws_names: Sequence[str] = (),
    services: bool = False,
    all_workspaces: bool = False,
    cwd: Path | None = None,
) -> int:
    """Stop one or more running workspaces/stacks, global services (`--services`), or everything (`--all`)."""
    if all_workspaces and ws_names:
        raise UsageError("cannot combine --all with WS or -w/--workspace")

    if all_workspaces:
        exit_code = 0
        stopped_any = False
        for root in index.read():
            if not root.is_dir():
                continue
            kind = "multi" if stacks.detect_multi_workspace(root) is not None else "workspace"
            try:
                target = open_target(root, kind, plugins)
            except Exception:
                continue
            if target.multi is not None:
                did_stop, rc = _stop_multi(target.multi, target.plugins, quiet_if_idle=True)
            else:
                did_stop, rc = _stop_workspace(target.workspace, target.plugins, quiet_if_idle=True)
            stopped_any = stopped_any or did_stop
            if rc != 0 and exit_code == 0:
                exit_code = rc
        if not stopped_any:
            term.out("# workspaces: none running")
        svc_rc = _stop_global_services(plugins)
        return exit_code or svc_rc

    if services and not ws_names:
        return _stop_global_services(plugins)

    base_cwd = (cwd or Path.cwd()).resolve()
    raw_names: list[str | None] = list(ws_names) or [None]
    exit_code = 0

    for raw in raw_names:
        root, kind = resolve_target_root(raw, cwd=base_cwd)
        target = open_target(root, kind, plugins)
        if target.multi is not None:
            _stopped, rc = _stop_multi(target.multi, target.plugins, quiet_if_idle=False)
        else:
            _stopped, rc = _stop_workspace(target.workspace, target.plugins, quiet_if_idle=False)
        if rc != 0 and exit_code == 0:
            exit_code = rc

    if services:
        svc_rc = _stop_global_services(plugins)
        if svc_rc != 0 and exit_code == 0:
            exit_code = svc_rc

    return exit_code
