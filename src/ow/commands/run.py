"""`ow run` — run a workspace or multi-stack workspace (mono TTY or native multi-process)."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.commands._repos import pr_local_ref
from ow.commands._runtime import (
    EXIT_INTERRUPTED,
    Target,
    combined_plugins,
    enclosing_multi,
    exit_status,
    open_target,
    plugin_running,
    run_command,
    running_probe,
    scoped_answer,
)
from ow.commands._shared import known_repo_catalog, prepare_services
from ow.commands._specs import deduplicate_name, parse_unit_spec
from ow.commands._workspace import (
    UnitMode,
    create_workspace_unit,
    resolve_required_spec,
    stack_info,
)
from ow.commands.logs import entries_for
from ow.commands.stop import plugin_stop
from ow.core import git, refresh, runtime, stacks
from ow.core.paths import repos_dir
from ow.core.plugins import ResolvedSpec, RunCommand
from ow.core.session import global_settings, open_session
from ow.core.workspace import resolve_target_root
from ow.errors import OwError, UsageError
from ow.recipes.removal import pr_ref_of

if TYPE_CHECKING:
    from ow.core.plugins import Plugins
    from ow.core.workspace import Workspace

_ANSI_COLORS = ("\033[36m", "\033[33m", "\033[35m", "\033[32m", "\033[34m")
_ANSI_RESET = "\033[0m"


def _branch_uses_pr_ref(bare: Path, pr_ref: str, *, pr: str | None = None) -> bool:
    """Whether any local branch in `bare` records the identity `pr`, or one fetched into `pr_ref`."""
    res = git.run(["git", "-C", str(bare), "config", "--get-regexp", r"^branch\..*\.ow-pr$"])
    if res.returncode != 0:
        return False
    expected = pr.strip().lower() if pr else None
    for line in res.stdout.splitlines():
        val = line.split(" ", 1)[-1].strip().lower()
        if expected is not None:
            if val == expected:
                return True
        elif pr_ref_of(val) == pr_ref:
            return True
    return False


def _cleanup_ephemeral(
    root: Path,
    plugins: Plugins,
    *,
    resolved: ResolvedSpec | None = None,
) -> None:
    """Remove worktrees, orphan ephemeral PR refs, and the temporary workspace directory."""
    try:
        session = open_session(str(root), need_workspace=False, plugins=plugins)
        for loc in session.locations:
            git.run(["git", "-C", str(loc.bare), "worktree", "remove", "--force", str(loc.path)])
            git.run(["git", "-C", str(loc.bare), "worktree", "prune"])
        if resolved is not None:
            for spec in resolved.repos:
                if not spec.fetch_ref:
                    continue
                bare = repos_dir() / f"{spec.name}.git"
                if not bare.is_dir():
                    continue
                pr_ref, _num = pr_local_ref(spec.fetch_ref, spec.branch)
                if not _branch_uses_pr_ref(bare, pr_ref, pr=spec.pr):
                    git.run(["git", "-C", str(bare), "update-ref", "-d", pr_ref])
    except Exception as exc:
        term.warn(f"ephemeral cleanup for {root.name} incomplete: {exc}")
    finally:
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            pass
        except OSError as exc:
            term.warn(f"could not remove temporary directory {root}: {exc}")


def _find_runner_with_name(
    ws: Workspace | None,
    plugins: Plugins,
    args: tuple[str, ...],
    multi: stacks.MultiWorkspace | None,
) -> tuple[str, RunCommand] | None:
    """Return `(plugin_name, RunCommand)` for the first enabled plugin answering `ow_runner`, or None.

    `ws` None asks for the whole multi-stack root `multi`; otherwise `ws` is one workspace or stack
    and `multi` its multi-stack root (None for a mono-stack workspace).
    """
    hit = scoped_answer(plugins, "ow_runner", ws, multi, args=args)
    if hit is not None and isinstance(hit[1], RunCommand):
        return hit[0], hit[1]
    return None


def _find_runner(
    ws: Workspace | None,
    plugins: Plugins,
    args: tuple[str, ...],
    multi: stacks.MultiWorkspace | None,
) -> RunCommand | None:
    """Return the first RunCommand contributed by an enabled plugin for that scope, or None."""
    hit = _find_runner_with_name(ws, plugins, args, multi)
    return hit[1] if hit is not None else None


def _select_runner(
    ws: Workspace, plugins: Plugins, args: tuple[str, ...], multi: stacks.MultiWorkspace | None
) -> tuple[str, RunCommand]:
    hit = _find_runner_with_name(ws, plugins, args, multi)
    if hit is None:
        raise UsageError(
            f"no runner available for workspace {ws.name!r}; ensure a runnable plugin (e.g. odoo) is enabled"
        )
    return hit


def _ensure_not_running(workspaces: Sequence[Workspace], plugins: Plugins) -> None:
    """Refuse to start a workspace/stack that already has live background processes."""
    for ws in workspaces:
        proc = runtime.running_for_workspace(ws)
        if proc is not None:
            raise UsageError(
                f"{ws.name} is already running (pid {proc.pid}); run `ow stop` or `ow logs -f`"
            )
        plugin_procs = plugin_running(ws, plugins)
        if plugin_procs:
            detail = ", ".join(plugin_procs)
            raise UsageError(
                f"{ws.name} is already running ({detail}); run `ow stop` or `ow logs -f`"
            )


def _announce_plugin_runner(target_name: str, plugin_name: str, plugins: Plugins, *, always: bool) -> None:
    """Print which plugin is executing `ow run` (always for multi-runners, and for external plugins)."""
    ext_map = dict(plugins.external_plugins)
    if plugin_name in ext_map:
        shown = git.display_path(ext_map[plugin_name])
        term.out(f"# starting {target_name} (plugin {plugin_name}: {shown})")
    elif always:
        term.out(f"# starting {target_name} (plugin {plugin_name})")


def _signal_group(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    try:
        os.killpg(proc.pid, sig)
    except OSError:
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def _terminate_all(
    procs: Sequence[tuple[str, subprocess.Popen[str]]],
    signals: Sequence[signal.Signals] = (signal.SIGINT, signal.SIGTERM),
) -> None:
    """Terminate all live child process groups: each of `signals` with 2 s grace, then SIGKILL."""
    for sig in signals:
        alive = [p for _label, p in procs if p.poll() is None]
        if not alive:
            return
        for proc in alive:
            _signal_group(proc, sig)
        for proc in alive:
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
    for _label, proc in procs:
        if proc.poll() is None:
            _signal_group(proc, signal.SIGKILL)
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass


def _interrupt_all(procs: Sequence[tuple[str, subprocess.Popen[str]]]) -> None:
    """Ctrl+C: SIGINT every child group and wait for them; one more Ctrl+C terminates them."""
    for _label, proc in procs:
        _signal_group(proc, signal.SIGINT)
    term.warn("interrupted; waiting for the processes to exit (Ctrl+C again to terminate them)")
    try:
        for _label, proc in procs:
            proc.wait()
    except KeyboardInterrupt:
        _terminate_all(procs, (signal.SIGTERM,))


def _run_multi(
    entries: Sequence[tuple[str, str | None, RunCommand]], *, fail_fast: bool = False
) -> int:
    """Run multiple stack commands in parallel with `[label]` prefixed logs."""
    use_color = sys.stdout.isatty()
    write_lock = threading.Lock()
    done_cond = threading.Condition()
    procs: list[tuple[str, subprocess.Popen[str]]] = []
    stream_threads: list[threading.Thread] = []
    wait_threads: list[threading.Thread] = []
    results: dict[str, int] = {}

    def _stream(prefix: str, pipe) -> None:
        try:
            for line in pipe:
                with write_lock:
                    sys.stdout.write(f"{prefix}{line}")
                    sys.stdout.flush()
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def _wait_proc(label: str, proc: subprocess.Popen[str]) -> None:
        rc = exit_status(proc.wait())
        with done_cond:
            results[label] = rc
            done_cond.notify_all()

    interrupted = False
    try:
        for idx, (label, stack_note, cmd) in enumerate(entries):
            note = f" ({stack_note})" if stack_note else ""
            term.out(f"# starting {label}{note}")
            env = dict(os.environ)
            env.update(cmd.env)
            try:
                proc = subprocess.Popen(
                    cmd.argv,
                    cwd=cmd.cwd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
            except OSError as exc:
                _terminate_all(procs)
                raise UsageError(f"failed to execute {cmd.argv[0]!r} for {label}: {exc}") from exc

            color = _ANSI_COLORS[idx % len(_ANSI_COLORS)] if use_color else ""
            reset = _ANSI_RESET if use_color else ""
            prefix = f"{color}[{label}]{reset} "
            procs.append((label, proc))
            assert proc.stdout is not None
            st = threading.Thread(target=_stream, args=(prefix, proc.stdout), daemon=True)
            st.start()
            stream_threads.append(st)
            wt = threading.Thread(target=_wait_proc, args=(label, proc), daemon=True)
            wt.start()
            wait_threads.append(wt)

        terminated_fast = False
        with done_cond:
            while len(results) < len(procs):
                if fail_fast and not terminated_fast and any(rc != 0 for rc in results.values()):
                    terminated_fast = True
                    _terminate_all(procs)
                done_cond.wait(timeout=0.5)
    except KeyboardInterrupt:
        interrupted = True
        _interrupt_all(procs)
    finally:
        for t in stream_threads:
            t.join(timeout=2.0)
        for wt in wait_threads:
            wt.join(timeout=2.0)

    if interrupted:
        return 130

    exit_code = 0
    for label, _proc in procs:
        rc = results.get(label, 1)
        if rc != 0:
            term.warn(f"{label} exited with code {rc}")
            if exit_code == 0:
                exit_code = rc
    if exit_code != 0:
        summary = ", ".join(f"{label}: exit {results.get(label, 1)}" for label, _p in procs)
        term.out(f"# summary: {summary}")
    return exit_code


def _confirm_reset(workspaces: Sequence[Workspace], plugins: Plugins, *, yes: bool) -> None:
    """Print what `--reset` will drop, as plugins preview it, and ask; no TTY and no `-y` refuses.

    A plugin that implements `ow_reset_run` but no `ow_reset_preview` is listed as `<plugin>: local
    state`, so the user is still asked. Nothing to drop (no plugin resets anything) means nothing
    to confirm. A preview that raises aborts the run (`ow_reset_preview` propagates) instead of
    asking without the full list.
    """
    lines: list[str] = []
    for ws in workspaces:
        for _name, answer in plugins.call("ow_reset_preview", ws=ws, answers=plugins.answers(ws)):
            if isinstance(answer, list):
                lines.extend(item for item in answer if isinstance(item, str) and item)
        previewing = set(plugins.implementers("ow_reset_preview", ws))
        for name in plugins.implementers("ow_reset_run", ws):
            line = f"{name}: local state"
            if name not in previewing and line not in lines:
                lines.append(line)
    if not lines:
        return
    term.out("# --reset will drop:")
    for line in lines:
        term.out(f"#   {line}")
    if yes:
        return
    if not term.interactive():
        raise UsageError("no terminal to confirm --reset; re-run with -y")
    if not term.confirm("Drop it?"):
        raise UsageError("aborted")


def _reset_workspaces(workspaces: Sequence[Workspace], plugins: Plugins) -> None:
    """Invoke `ow_reset_run` for each workspace to reset runtime state (e.g. DB + filestore)."""
    for ws in workspaces:
        results = plugins.call("ow_reset_run", ws=ws, answers=plugins.answers(ws))
        for _plugin_name, result in results:
            if isinstance(result, list):
                for line in result:
                    if isinstance(line, str) and line:
                        term.out(f"# {line}")


def _refresh_before_run(
    workspaces: Sequence[Workspace],
    plugins: Plugins,
    *,
    multi: stacks.MultiWorkspace | None = None,
) -> None:
    """Sync managed files after `prepare_services` if dynamic service values (e.g. Postgres port) changed."""
    for ws in workspaces:
        eff_multi = multi if multi is not None else refresh._enclosing_multi(ws, plugins)
        try:
            written = refresh.sync_if_stale(ws, plugins, multi=eff_multi)
        except OwError as exc:
            term.warn(f"{ws.name}: refresh before run skipped: {exc}")
            continue
        if written:
            prefix = f"{ws.name}/" if ws.is_stack else ""
            paths = ", ".join(f"{prefix}{p.as_posix()}" for p in written)
            term.out(f"# refreshed {paths}")


def _run_workspaces(
    items: Sequence[tuple[str, Workspace, tuple[str, ...]]],
    plugins: Plugins,
    *,
    fail_fast: bool = False,
    reset: bool = False,
    yes: bool = False,
    detach: bool = False,
    multi: stacks.MultiWorkspace | None = None,
) -> int:
    """Run one workspace directly (TTY) or several in parallel; `-d` detaches them under a launch lock."""
    guard = (
        runtime.launch_lock(runtime.runtime_root(ws) for _label, ws, _args in items)
        if detach
        else contextlib.nullcontext()
    )
    with guard:
        return _launch(
            items, plugins, fail_fast=fail_fast, reset=reset, yes=yes, detach=detach, multi=multi
        )


def _launch(
    items: Sequence[tuple[str, Workspace, tuple[str, ...]]],
    plugins: Plugins,
    *,
    fail_fast: bool,
    reset: bool,
    yes: bool,
    detach: bool,
    multi: stacks.MultiWorkspace | None,
) -> int:
    def hook_multi(ws: Workspace) -> stacks.MultiWorkspace | None:
        """The multi-stack root a stack's hooks get: the one being run, else the stack's own."""
        return multi if multi is not None else enclosing_multi(ws, plugins)

    if len(items) == 1:
        _label, ws, args = items[0]
        ws_multi = hook_multi(ws)
        # Verify a runner exists before starting services, then re-resolve after syncing generated files.
        _select_runner(ws, plugins, args, ws_multi)
        if not args or detach:
            _ensure_not_running((ws,), plugins)
        if reset:
            _confirm_reset((ws,), plugins, yes=yes)
        prepare_services((ws,), plugins)
        _refresh_before_run((ws,), plugins, multi=multi)
        plugin_name, cmd = _select_runner(ws, plugins, args, ws_multi)
        if reset:
            _reset_workspaces((ws,), plugins)
        _announce_plugin_runner(ws.name, plugin_name, plugins, always=False)
        return _execute(cmd, plugins, ws=ws, multi=ws_multi, detach=detach)

    runnable_items: list[tuple[str, Workspace, tuple[str, ...]]] = []
    for label, ws, args in items:
        if _find_runner(ws, plugins, args, hook_multi(ws)) is None:
            term.out(f"# {label}: no runner available, skipping")
            continue
        runnable_items.append((label, ws, args))

    if not runnable_items:
        names = ", ".join(label for label, _, _ in items)
        raise UsageError(f"no runnable stacks among: {names}")

    runnable_ws = [ws for _label, ws, _args in runnable_items]
    _ensure_not_running(runnable_ws, plugins)
    if reset:
        _confirm_reset(runnable_ws, plugins, yes=yes)
    prepare_services(runnable_ws, plugins)
    _refresh_before_run(runnable_ws, plugins, multi=multi)

    runnable: list[_Runnable] = []
    for label, ws, args in runnable_items:
        ws_multi = hook_multi(ws)
        cmd = _find_runner(ws, plugins, args, ws_multi)
        if cmd is not None:
            runnable.append(_Runnable(label, ws, ws_multi, stack_info(ws, plugins).note, cmd))

    if not runnable:
        names = ", ".join(label for label, _, _ in runnable_items)
        raise UsageError(f"no runnable stacks among: {names}")

    if reset:
        _reset_workspaces(runnable_ws, plugins)

    if detach:
        started: list[tuple[Path, runtime.RunningProcess]] = []
        try:
            for unit in runnable:
                if unit.cmd.self_detaching:
                    rc = run_command(unit.cmd)
                    if rc != 0:
                        raise UsageError(f"{unit.label} failed to start (exit {rc})")
                    continue
                root = runtime.runtime_root(unit.ws)
                proc = runtime.start_detached(root, unit.ws.name, unit.cmd.argv, cwd=unit.cmd.cwd, env=unit.cmd.env)
                started.append((root, proc))
                term.out(f"# started {unit.label} (pid {proc.pid}, logs: {git.display_path(proc.log_path)})")
        except Exception:
            for root, proc in reversed(started):
                runtime.stop_process(root, proc)
            raise
        return 0

    if any(unit.cmd.self_detaching for unit in runnable):
        return _run_foreground_with_services(runnable, plugins, fail_fast=fail_fast)

    if len(runnable) == 1:
        return run_command(runnable[0].cmd)

    return _run_multi([(unit.label, unit.note, unit.cmd) for unit in runnable], fail_fast=fail_fast)


@dataclass(frozen=True)
class _Runnable:
    """One stack to run: the command that answered for it, and the `multi` its hooks were called with."""

    label: str
    ws: Workspace
    multi: stacks.MultiWorkspace | None
    note: str | None
    cmd: RunCommand


def _execute(
    cmd: RunCommand,
    plugins: Plugins,
    *,
    ws: Workspace | None,
    multi: stacks.MultiWorkspace | None,
    detach: bool,
) -> int:
    """Run the command that answered `ow_runner` for one scope (`ws` None: a multi-stack root as a whole).

    ow decides how it runs: a self-detaching command starts services and returns (`-d` stops
    there; in the foreground ow then follows the logs and stops the services when it ends or is
    interrupted), any other command runs on the terminal, or detached under `-d`.
    """
    if cmd.self_detaching:
        return _run_self_detaching(cmd, plugins, ((ws, multi),), detach=detach)
    if detach:
        if ws is not None:
            root, name = runtime.runtime_root(ws), ws.name
        else:
            assert multi is not None
            root, name = multi.root, multi.name
        proc = runtime.start_detached(root, name, cmd.argv, cwd=cmd.cwd, env=cmd.env)
        term.out(f"# started {name} (pid {proc.pid}, logs: {git.display_path(proc.log_path)})")
        return 0
    return run_command(cmd)


Scope = tuple["Workspace | None", "stacks.MultiWorkspace | None"]


def _run_self_detaching(cmd: RunCommand, plugins: Plugins, scopes: Sequence[Scope], *, detach: bool) -> int:
    """Start a self-detaching command; unless detached, follow its services' logs, then stop them."""
    rc = run_command(cmd)
    if detach:
        return rc
    try:
        result = _follow_services(plugins, scopes) if rc == 0 else rc
    finally:
        stop_rc = _stop_services(plugins, scopes)
    return result or stop_rc


def _run_foreground_with_services(runnable: Sequence[_Runnable], plugins: Plugins, *, fail_fast: bool) -> int:
    """Run stacks of which some are self-detaching: start those, run the others on the terminal
    (or follow the services' logs when there are no others), then stop the started services."""
    started: list[Scope] = []
    result = 0
    try:
        for unit in runnable:
            if not unit.cmd.self_detaching:
                continue
            started.append((unit.ws, unit.multi))
            rc = run_command(unit.cmd)
            if rc != 0:
                raise UsageError(f"{unit.label} failed to start (exit {rc})")
        plain = [unit for unit in runnable if not unit.cmd.self_detaching]
        if not plain:
            result = _follow_services(plugins, started)
        elif len(plain) == 1:
            result = run_command(plain[0].cmd)
        else:
            result = _run_multi([(unit.label, unit.note, unit.cmd) for unit in plain], fail_fast=fail_fast)
    finally:
        stop_rc = _stop_services(plugins, started)
    return result or stop_rc


def _follow_services(plugins: Plugins, scopes: Sequence[Scope]) -> int:
    """Follow the logs of the services just started, as `ow logs -f` does, until none is running or Ctrl+C.

    Returns 130 when interrupted. Without a plugin reporting what runs (`ow_running`) there is
    nothing to wait on but Ctrl+C.
    """
    entries: list[tuple[str, Path]] = []
    probes = []
    tracked = False
    for ws, multi in scopes:
        entries.extend(entries_for(plugins, ws, multi))
        workspaces = (ws,) if ws is not None else (multi.stacks if multi is not None else ())
        probes.append(running_probe(plugins, workspaces, multi if ws is None else None))
        tracked = tracked or any(plugins.implementers("ow_running", w) for w in workspaces)
    running = (lambda: any(probe() for probe in probes)) if tracked else None
    if entries:
        return runtime.follow_files(entries, lines=50, follow=True, running=running)
    try:
        while running is None or running():
            time.sleep(0.5)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return 0


def _stop_services(plugins: Plugins, scopes: Sequence[Scope]) -> int:
    """Run `ow_stop` for every scope whose services were started; the first non-zero exit code."""
    exit_code = 0
    for ws, multi in scopes:
        _stopped, rc = plugin_stop(plugins, ws, multi)
        if rc != 0 and exit_code == 0:
            exit_code = rc
    return exit_code


def _exec_multi_runner(
    multi: stacks.MultiWorkspace,
    plugins: Plugins,
    args: tuple[str, ...],
    *,
    reset: bool,
    yes: bool,
    detach: bool,
) -> int:
    """Prepare services, sync stale files, and run the plugin's whole-root `ow_runner` answer."""
    guard = runtime.launch_lock((multi.root,)) if detach else contextlib.nullcontext()
    with guard:
        if not args or detach:
            _ensure_not_running(multi.stacks, plugins)
        if reset:
            _confirm_reset(multi.stacks, plugins, yes=yes)
        prepare_services(multi.stacks, plugins)
        _refresh_before_run(multi.stacks, plugins, multi=multi)
        multi_hit = _find_runner_with_name(None, plugins, args, multi)
        assert multi_hit is not None
        plugin_name, multi_cmd = multi_hit
        if reset:
            _reset_workspaces(multi.stacks, plugins)
        _announce_plugin_runner(multi.name, plugin_name, plugins, always=True)
        return _execute(multi_cmd, plugins, ws=None, multi=multi, detach=detach)


def run(
    plugins: Plugins,
    *,
    items: Sequence[str] = (),
    workspace: str | None = None,
    tmp: bool = False,
    fail_fast: bool = False,
    reset: bool = False,
    yes: bool = False,
    detach: bool = False,
    extra_args: Sequence[str] = (),
    cwd: Path | None = None,
) -> int:
    """Run a workspace or multi-stack workspace (or create temporary stack(s) with `--tmp`).

    `plugins` is the built-in set; every workspace opened here brings its own in-repo plugins
    and the run then goes through that one set.
    """
    if items and workspace is not None:
        raise UsageError("pass either WS or -w/--workspace, not both")
    if tmp and detach:
        raise UsageError("cannot combine -d/--detach with --tmp")
    if tmp and not items:
        raise UsageError("--tmp requires at least one SPEC (e.g. `ow run --tmp enterprise#12345`)")
    if len(items) > 1 and extra_args:
        raise UsageError(
            'cannot combine `-- ARGS` with more than one WS or SPEC; pass per-stack arguments as WS:"ARGS"'
        )
    if reset and not yes and not term.interactive():
        raise UsageError("no terminal to confirm --reset; re-run with -y")

    base_cwd = (cwd or Path.cwd()).resolve()
    passthrough = tuple(extra_args)

    # Mode 1: Ephemeral `--tmp` run (1 or more specs, optional names, non-colliding ports)
    if tmp:
        catalog = known_repo_catalog(plugins)
        settings = global_settings(plugins)
        ephemeral_parent = Path(tempfile.mkdtemp(prefix="ow-run-"))
        created_ephemerals: list[tuple[Path, ResolvedSpec]] = []
        try:
            taken: set[str] = set()
            pending: list[dict[str, object]] = []
            opened: list[Target] = []
            to_run: list[tuple[str, Workspace, tuple[str, ...]]] = []

            for raw in items:
                unit = parse_unit_spec(raw, allow_run_args=True)
                resolved = resolve_required_spec(unit.spec, plugins, catalog, settings)
                if unit.name is not None:
                    if unit.name in taken:
                        raise UsageError(f"duplicate stack name {unit.name!r}")
                    taken.add(unit.name)
                    label = unit.name
                else:
                    label = deduplicate_name(resolved.name, taken)

                unit_root = ephemeral_parent / label
                created, code = create_workspace_unit(
                    plugins,
                    root=unit_root,
                    resolved=resolved,
                    mode=UnitMode.ephemeral(
                        is_stack=len(items) > 1,
                        parent_multi_root=ephemeral_parent if len(items) > 1 else None,
                    ),
                    pending=pending if len(items) > 1 else None,
                )
                created_ephemerals.append((created.root, resolved))
                if code != 0:
                    return code
                target = open_target(created.root, "workspace", plugins)
                opened.append(target)
                combined_args = unit.extra_args + (passthrough if len(items) == 1 else ())
                to_run.append((label, target.workspace, combined_args))

            return _run_workspaces(
                to_run,
                combined_plugins(plugins, opened),
                fail_fast=fail_fast,
                reset=reset,
                yes=yes,
            )
        finally:
            try:
                for root, resolved in reversed(created_ephemerals):
                    _cleanup_ephemeral(root, plugins, resolved=resolved)
            finally:
                if ephemeral_parent.exists():
                    shutil.rmtree(ephemeral_parent, ignore_errors=True)

    # Mode 2: No positional items (`ow run` or `ow run -w WS`)
    if not items:
        root, kind = resolve_target_root(workspace, cwd=base_cwd)
        target = open_target(root, kind, plugins)
        if target.multi is None:
            ws = target.workspace
            return _run_workspaces(
                [(ws.name, ws, passthrough)],
                target.plugins,
                fail_fast=fail_fast,
                reset=reset,
                yes=yes,
                detach=detach,
            )

        multi = target.multi
        if _find_runner(None, target.plugins, passthrough, multi) is not None:
            return _exec_multi_runner(
                multi, target.plugins, passthrough, reset=reset, yes=yes, detach=detach
            )
        if passthrough:
            runnable_ws = [
                ws
                for ws in multi.stacks
                if _find_runner(ws, target.plugins, passthrough, multi) is not None
            ]
            if len(runnable_ws) > 1:
                names = ", ".join(ws.name for ws in runnable_ws)
                raise UsageError(
                    f"multiple runnable stacks in {multi.name!r} ({names}); "
                    'specify a stack with `ow run <stack> -- ...` or `ow run <stack>:"..."`'
                )
        items = [(ws.name, ws, passthrough) for ws in multi.stacks]
        return _run_workspaces(
            items,
            target.plugins,
            fail_fast=fail_fast,
            reset=reset,
            yes=yes,
            detach=detach,
            multi=multi,
        )

    # Mode 3: Positional workspaces without `--tmp` -> existing workspace(s) or stack(s) only
    to_run = []
    opened = []
    for raw in items:
        unit = parse_unit_spec(raw, allow_run_args=True)
        lookup = unit.name if unit.name is not None else unit.spec
        if unit.name is not None:
            raise UsageError(
                f"unexpected '=' in {raw!r}; did you mean `ow run --tmp {raw}`, "
                "or run `ow init` first?"
            )
        try:
            root, kind = resolve_target_root(lookup, cwd=base_cwd)
        except UsageError as exc:
            # A token that names no indexed workspace or stack is a SPEC, which needs `--tmp`.
            raise UsageError(
                f"unknown workspace or stack {lookup!r}; did you mean `ow run --tmp {raw}`, "
                "or initialize a workspace with `ow init` first?"
            ) from exc

        combined_args = unit.extra_args + (passthrough if len(items) == 1 else ())
        target = open_target(root, kind, plugins)
        opened.append(target)
        if target.multi is None:
            to_run.append((target.workspace.name, target.workspace, combined_args))
            continue
        if len(items) == 1 and _find_runner(None, target.plugins, combined_args, target.multi) is not None:
            return _exec_multi_runner(
                target.multi, target.plugins, combined_args, reset=reset, yes=yes, detach=detach
            )
        if combined_args:
            raise UsageError(
                f"{root.name!r} is a multi-stack workspace; pass arguments to individual stacks"
            )
        for ws in target.multi.stacks:
            to_run.append((ws.name, ws, ()))

    return _run_workspaces(
        to_run,
        combined_plugins(plugins, opened),
        fail_fast=fail_fast,
        reset=reset,
        yes=yes,
        detach=detach,
    )
