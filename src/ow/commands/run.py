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
from ow.core import git, stacks
from ow.core.paths import repos_dir
from ow.core.plugins import ResolvedTarget, RunCommand
from ow.core.session import global_settings, open_session
from ow.core.workspace import resolve_target_root
from ow.errors import PluginError, UsageError
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


def _find_runner(ws: Workspace, plugins: Plugins, args: tuple[str, ...]) -> RunCommand | None:
    """Return the first RunCommand contributed by an enabled plugin for `ws`, or None."""
    answer = plugins.first("ow_runner", ws=ws, args=args)
    return answer if isinstance(answer, RunCommand) else None


def _select_runner(ws: Workspace, plugins: Plugins, args: tuple[str, ...]) -> RunCommand:
    cmd = _find_runner(ws, plugins, args)
    if cmd is None:
        raise UsageError(
            f"no runner available for workspace {ws.name!r}; ensure a runnable plugin (e.g. odoo) is enabled"
        )
    return cmd


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


def _run_workspaces(
    items: Sequence[tuple[str, Workspace, tuple[str, ...]]],
    plugins: Plugins,
    *,
    fail_fast: bool = False,
    reset: bool = False,
) -> int:
    """Run one workspace directly (TTY) or multiple workspaces in parallel."""
    if len(items) == 1:
        _label, ws, args = items[0]
        cmd = _select_runner(ws, plugins, args)
        prepare_services((ws,), plugins)
        if reset:
            _reset_workspaces((ws,), plugins)
        return _run_single(cmd)

    runnable: list[tuple[str, str | None, RunCommand]] = []
    runnable_ws: list[Workspace] = []
    for label, ws, args in items:
        cmd = _find_runner(ws, plugins, args)
        if cmd is None:
            term.out(f"# {label}: no runner available, skipping")
            continue
        runnable.append((label, stack_info(ws, plugins).note, cmd))
        runnable_ws.append(ws)

    if not runnable:
        names = ", ".join(label for label, _, _ in items)
        raise UsageError(f"no runnable stacks among: {names}")

    prepare_services(runnable_ws, plugins)
    if reset:
        _reset_workspaces(runnable_ws, plugins)

    if len(runnable) == 1:
        return _run_single(runnable[0][2])

    return _run_multi(runnable, fail_fast=fail_fast)


def _open_ws(path: Path, plugins: Plugins) -> Workspace:
    session = open_session(str(path), need_workspace=True, plugins=plugins, cwd=path)
    for warning in session.warnings:
        term.warn(warning)
    assert session.workspace is not None
    return session.workspace


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
    extra_args: Sequence[str] = (),
    cwd: Path | None = None,
) -> int:
    """Run a workspace or multi-stack workspace (or create temporary stack(s) with `--tmp`)."""
    if targets and workspace is not None:
        raise UsageError("pass either TARGET(s) or -w/--workspace, not both")
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
                    mode=UnitMode.ephemeral(is_stack=len(targets) > 1),
                    pending=pending if len(targets) > 1 else None,
                )
                created_ephemerals.append((created.root, resolved))
                if code != 0:
                    return code
                ws = _open_ws(created.root, plugins)
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
            ws = _open_ws(root, plugins)
            return _run_workspaces([(ws.name, ws, passthrough)], plugins, fail_fast=fail_fast, reset=reset)

        loaded = stacks.load_multi_workspace(root, plugins)
        if loaded is None:
            raise UsageError(f"no valid stacks found in {root}")
        multi, sessions = loaded
        for sess in sessions:
            for warning in sess.warnings:
                term.warn(warning)
        if passthrough:
            runnable_ws = [
                ws for ws in multi.stacks if _find_runner(ws, plugins, passthrough) is not None
            ]
            if len(runnable_ws) > 1:
                names = ", ".join(ws.name for ws in runnable_ws)
                raise UsageError(
                    f"multiple runnable stacks in {multi.name!r} ({names}); "
                    'specify a stack with `ow run <stack> -- ...` or `ow run <stack>:"..."`'
                )
        items = [(ws.name, ws, passthrough) for ws in multi.stacks]
        return _run_workspaces(items, plugins, fail_fast=fail_fast, reset=reset)

    # Mode 3: Positional targets without `--tmp` -> existing workspace(s) or stack(s) only
    to_run = []
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
            ws = _open_ws(root, plugins)
            to_run.append((ws.name, ws, combined_args))
        else:
            if combined_args:
                raise UsageError(
                    f"{root.name!r} is a multi-stack workspace; pass arguments to individual stacks"
                )
            loaded = stacks.load_multi_workspace(root, plugins)
            if loaded is None:
                raise UsageError(f"no valid stacks found in {root}")
            for ws in loaded[0].stacks:
                to_run.append((ws.name, ws, ()))

    return _run_workspaces(to_run, plugins, fail_fast=fail_fast, reset=reset)
