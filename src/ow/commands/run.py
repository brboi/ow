"""`ow run` — run a workspace or multi-stack workspace (mono TTY or native multi-process)."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.commands._repos import pr_local_ref
from ow.commands._shared import known_repo_catalog, prepare_services
from ow.commands._targets import deduplicate_name, parse_unit_spec
from ow.commands._workspace import (
    UnitMode,
    create_workspace_unit,
    resolve_required_target,
    stack_info,
)
from ow.core import git, refresh, runtime, stacks
from ow.core.paths import repos_dir
from ow.core.plugins import ResolvedTarget, RunCommand
from ow.core.session import global_settings, open_session
from ow.core.workspace import resolve_target_root
from ow.errors import OwError, PluginError, UsageError
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
    resolved: ResolvedTarget | None = None,
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
    ws: Workspace, plugins: Plugins, args: tuple[str, ...], *, detach: bool = False
) -> tuple[str, RunCommand] | None:
    """Return `(plugin_name, RunCommand)` for the first enabled plugin answering `ow_runner`, or None."""
    hit = plugins.first_with_name("ow_runner", ws=ws, args=args, detach=detach)
    if hit is not None and isinstance(hit[1], RunCommand):
        return hit[0], hit[1]
    return None


def _find_runner(
    ws: Workspace, plugins: Plugins, args: tuple[str, ...], *, detach: bool = False
) -> RunCommand | None:
    """Return the first RunCommand contributed by an enabled plugin for `ws`, or None."""
    hit = _find_runner_with_name(ws, plugins, args, detach=detach)
    return hit[1] if hit is not None else None


def _select_runner(
    ws: Workspace, plugins: Plugins, args: tuple[str, ...], *, detach: bool = False
) -> tuple[str, RunCommand]:
    hit = _find_runner_with_name(ws, plugins, args, detach=detach)
    if hit is None:
        raise UsageError(
            f"no runner available for workspace {ws.name!r}; ensure a runnable plugin (e.g. odoo) is enabled"
        )
    return hit


def _plugin_running(ws: Workspace, plugins: Plugins) -> list[str]:
    """Return running process/service descriptions reported by plugins for `ws`."""
    found: list[str] = []
    for _name, answer in plugins.call("ow_running", ws=ws, errors="collect"):
        if isinstance(answer, list):
            found.extend(str(item) for item in answer if isinstance(item, str) and item)
    return found


def _ensure_not_running(workspaces: Sequence[Workspace], plugins: Plugins) -> None:
    """Refuse to start a workspace/stack that already has live background processes."""
    for ws in workspaces:
        proc = runtime.running_for_workspace(ws)
        if proc is not None:
            raise UsageError(
                f"{ws.name} is already running (pid {proc.pid}); run `ow stop` or `ow logs -f`"
            )
        plugin_procs = _plugin_running(ws, plugins)
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


def _run_single(cmd: RunCommand) -> int:
    """Execute one runner with direct inherited TTY (preserves pdb, shell, signals)."""
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


def _terminate_all(procs: Sequence[tuple[str, subprocess.Popen[str]]]) -> None:
    """Gracefully terminate all live child process groups (SIGINT -> SIGTERM -> SIGKILL)."""
    for sig, timeout in ((signal.SIGINT, 2.0), (signal.SIGTERM, 2.0)):
        alive = [(label, p) for label, p in procs if p.poll() is None]
        if not alive:
            return
        for _label, proc in alive:
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.send_signal(sig)
                except OSError:
                    pass
        for _label, proc in alive:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
    for _label, proc in procs:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass


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
        rc = proc.wait()
        if rc < 0:
            rc = 128 + abs(rc)
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
        _terminate_all(procs)
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
    detach: bool = False,
    multi: stacks.MultiWorkspace | None = None,
) -> int:
    """Run one workspace directly (TTY) or multiple workspaces in parallel (or detached with `-d`)."""
    if len(items) == 1:
        _label, ws, args = items[0]
        # Verify a runner exists before starting services, then re-resolve after syncing generated files.
        _select_runner(ws, plugins, args, detach=detach)
        if not args or detach:
            _ensure_not_running((ws,), plugins)
        prepare_services((ws,), plugins)
        _refresh_before_run((ws,), plugins, multi=multi)
        plugin_name, cmd = _select_runner(ws, plugins, args, detach=detach)
        if reset:
            _reset_workspaces((ws,), plugins)
        _announce_plugin_runner(ws.name, plugin_name, plugins, always=False)
        if detach and not cmd.self_detaching:
            root = runtime.runtime_root(ws)
            proc = runtime.start_detached(root, ws.name, cmd.argv, cwd=cmd.cwd, env=cmd.env)
            term.out(f"# started {ws.name} (pid {proc.pid}, logs: {git.display_path(proc.log_path)})")
            return 0
        return _run_single(cmd)

    runnable_items: list[tuple[str, Workspace, tuple[str, ...]]] = []
    for label, ws, args in items:
        if _find_runner(ws, plugins, args, detach=detach) is None:
            term.out(f"# {label}: no runner available, skipping")
            continue
        runnable_items.append((label, ws, args))

    if not runnable_items:
        names = ", ".join(label for label, _, _ in items)
        raise UsageError(f"no runnable stacks among: {names}")

    runnable_ws = [ws for _label, ws, _args in runnable_items]
    _ensure_not_running(runnable_ws, plugins)
    prepare_services(runnable_ws, plugins)
    _refresh_before_run(runnable_ws, plugins, multi=multi)

    runnable: list[tuple[str, Workspace, str | None, RunCommand]] = []
    for label, ws, args in runnable_items:
        cmd = _find_runner(ws, plugins, args, detach=detach)
        if cmd is not None:
            runnable.append((label, ws, stack_info(ws, plugins).note, cmd))

    if not runnable:
        names = ", ".join(label for label, _, _ in runnable_items)
        raise UsageError(f"no runnable stacks among: {names}")

    if reset:
        _reset_workspaces(runnable_ws, plugins)

    if detach:
        started: list[tuple[Path, runtime.RunningProcess]] = []
        try:
            for label, ws, _note, cmd in runnable:
                if cmd.self_detaching:
                    rc = _run_single(cmd)
                    if rc != 0:
                        raise UsageError(f"{label} failed to start (exit {rc})")
                    continue
                root = runtime.runtime_root(ws)
                proc = runtime.start_detached(root, ws.name, cmd.argv, cwd=cmd.cwd, env=cmd.env)
                started.append((root, proc))
                term.out(f"# started {label} (pid {proc.pid}, logs: {git.display_path(proc.log_path)})")
        except Exception:
            for root, proc in reversed(started):
                runtime.stop_process(root, proc)
            raise
        return 0

    if len(runnable) == 1:
        return _run_single(runnable[0][3])

    return _run_multi([(label, note, cmd) for label, _ws, note, cmd in runnable], fail_fast=fail_fast)


def _open_ws(path: Path, plugins: Plugins) -> tuple[Workspace, Plugins]:
    session = open_session(str(path), need_workspace=True, plugins=plugins, cwd=path)
    for warning in session.warnings:
        term.warn(warning)
    assert session.workspace is not None
    return session.workspace, session.plugins


def _find_multi_runner(
    multi: stacks.MultiWorkspace, plugins: Plugins, args: tuple[str, ...], *, detach: bool = False
) -> tuple[str, RunCommand] | None:
    """Return `(plugin_name, RunCommand)` for the first enabled plugin answering `ow_multi_runner`, or None."""
    ws_ctx = multi.stacks[0] if multi.stacks else None
    hit = plugins.first_with_name("ow_multi_runner", ws=ws_ctx, multi=multi, args=args, detach=detach)
    if hit is not None and isinstance(hit[1], RunCommand):
        return hit[0], hit[1]
    return None


def _exec_multi_runner(
    multi: stacks.MultiWorkspace,
    eff_plugins: Plugins,
    args: tuple[str, ...],
    *,
    reset: bool,
    detach: bool,
) -> int:
    """Prepare services, sync stale files, and run a plugin's `ow_multi_runner` (foreground or detached)."""
    if not args or detach:
        _ensure_not_running(multi.stacks, eff_plugins)
    prepare_services(multi.stacks, eff_plugins)
    _refresh_before_run(multi.stacks, eff_plugins, multi=multi)
    multi_hit = _find_multi_runner(multi, eff_plugins, args, detach=detach)
    assert multi_hit is not None
    plugin_name, multi_cmd = multi_hit
    if reset:
        _reset_workspaces(multi.stacks, eff_plugins)
    _announce_plugin_runner(multi.name, plugin_name, eff_plugins, always=True)
    if detach and not multi_cmd.self_detaching:
        proc = runtime.start_detached(
            multi.root, multi.name, multi_cmd.argv, cwd=multi_cmd.cwd, env=multi_cmd.env
        )
        term.out(f"# started {multi.name} (pid {proc.pid}, logs: {git.display_path(proc.log_path)})")
        return 0
    return _run_single(multi_cmd)


def _looks_like_target_spec(token: str) -> bool:
    return any(sym in token for sym in ("#", "@", ",", "://", "/pull/", "/runbot/"))


def run(
    plugins: Plugins,
    *,
    targets: Sequence[str] = (),
    workspace: str | None = None,
    tmp: bool = False,
    fail_fast: bool = False,
    reset: bool = False,
    detach: bool = False,
    extra_args: Sequence[str] = (),
    cwd: Path | None = None,
) -> int:
    """Run a workspace or multi-stack workspace (or create temporary stack(s) with `--tmp`)."""
    if targets and workspace is not None:
        raise UsageError("pass either TARGET(s) or -w/--workspace, not both")
    if tmp and detach:
        raise UsageError("cannot combine -d/--detach with --tmp")
    if tmp and not targets:
        raise UsageError("--tmp requires at least one TARGET (e.g. `ow run --tmp enterprise#12345`)")
    if len(targets) > 1 and extra_args:
        raise UsageError(
            'cannot combine `-- ARGS` with multiple targets; pass per-stack arguments as SPEC:"ARGS"'
        )

    base_cwd = (cwd or Path.cwd()).resolve()
    passthrough = tuple(extra_args)

    # Mode 1: Ephemeral `--tmp` run (1 or more targets, optional names, non-colliding ports)
    if tmp:
        catalog = known_repo_catalog(plugins)
        settings = global_settings(plugins)
        ephemeral_parent = Path(tempfile.mkdtemp(prefix="ow-run-"))
        created_ephemerals: list[tuple[Path, ResolvedTarget]] = []
        try:
            taken: set[str] = set()
            pending: list[dict[str, object]] = []
            to_run: list[tuple[str, Workspace, tuple[str, ...]]] = []

            for raw in targets:
                unit = parse_unit_spec(raw, allow_run_args=True)
                resolved = resolve_required_target(unit.spec, plugins, catalog, settings)
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
                        is_stack=len(targets) > 1,
                        parent_multi_root=ephemeral_parent if len(targets) > 1 else None,
                    ),
                    pending=pending if len(targets) > 1 else None,
                )
                created_ephemerals.append((created.root, resolved))
                if code != 0:
                    return code
                ws, _ws_plugins = _open_ws(created.root, plugins)
                combined_args = unit.extra_args + (passthrough if len(targets) == 1 else ())
                to_run.append((label, ws, combined_args))

            return _run_workspaces(to_run, plugins, fail_fast=fail_fast, reset=reset)
        finally:
            try:
                for root, resolved in reversed(created_ephemerals):
                    _cleanup_ephemeral(root, plugins, resolved=resolved)
            finally:
                if ephemeral_parent.exists():
                    shutil.rmtree(ephemeral_parent, ignore_errors=True)

    # Mode 2: No positional targets (`ow run` or `ow run -w WS`)
    if not targets:
        root, kind = resolve_target_root(workspace, cwd=base_cwd)
        if kind == "workspace":
            ws, eff_plugins = _open_ws(root, plugins)
            return _run_workspaces(
                [(ws.name, ws, passthrough)],
                eff_plugins,
                fail_fast=fail_fast,
                reset=reset,
                detach=detach,
            )

        loaded = stacks.load_multi_workspace(root, plugins)
        if loaded is None:
            raise UsageError(f"no valid stacks found in {root}")
        multi, sessions = loaded
        eff_plugins = sessions[0].plugins if sessions else plugins
        for sess in sessions:
            for warning in sess.warnings:
                term.warn(warning)
        if _find_multi_runner(multi, eff_plugins, passthrough, detach=detach) is not None:
            return _exec_multi_runner(multi, eff_plugins, passthrough, reset=reset, detach=detach)
        if passthrough:
            runnable_ws = [
                ws for ws in multi.stacks if _find_runner(ws, eff_plugins, passthrough, detach=detach) is not None
            ]
            if len(runnable_ws) > 1:
                names = ", ".join(ws.name for ws in runnable_ws)
                raise UsageError(
                    f"multiple runnable stacks in {multi.name!r} ({names}); "
                    'specify a stack with `ow run <stack> -- ...` or `ow run <stack>:"..."`'
                )
        items = [(ws.name, ws, passthrough) for ws in multi.stacks]
        return _run_workspaces(items, eff_plugins, fail_fast=fail_fast, reset=reset, detach=detach, multi=multi)

    # Mode 3: Positional targets without `--tmp` -> existing workspace(s) or stack(s) only
    to_run = []
    run_plugins = plugins
    for raw in targets:
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
            if _looks_like_target_spec(raw):
                raise UsageError(
                    f"unknown workspace or stack {lookup!r}; did you mean `ow run --tmp {raw}`, "
                    "or initialize a workspace with `ow init` first?"
                ) from exc
            raise UsageError(
                f"unknown workspace or stack {lookup!r}; pass --tmp for an ephemeral run, "
                "or initialize a workspace with `ow init` first"
            ) from exc

        combined_args = unit.extra_args + (passthrough if len(targets) == 1 else ())
        if kind == "workspace":
            ws, eff_plugins = _open_ws(root, plugins)
            run_plugins = eff_plugins
            to_run.append((ws.name, ws, combined_args))
        else:
            loaded = stacks.load_multi_workspace(root, plugins)
            if loaded is None:
                raise UsageError(f"no valid stacks found in {root}")
            multi, sessions = loaded
            eff_plugins = sessions[0].plugins if sessions else plugins
            run_plugins = eff_plugins
            if len(targets) == 1:
                if _find_multi_runner(multi, eff_plugins, combined_args, detach=detach) is not None:
                    for sess in sessions:
                        for warning in sess.warnings:
                            term.warn(warning)
                    return _exec_multi_runner(multi, eff_plugins, combined_args, reset=reset, detach=detach)
            if combined_args:
                raise UsageError(
                    f"{root.name!r} is a multi-stack workspace; pass arguments to individual stacks"
                )
            for ws in multi.stacks:
                to_run.append((ws.name, ws, ()))

    return _run_workspaces(to_run, run_plugins, fail_fast=fail_fast, reset=reset, detach=detach)
