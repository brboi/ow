"""Helpers shared by the commands that open, run, stop, follow and remove workspaces.

A command that has a workspace opens it through `open_target` and from then on uses
`Target.plugins`: the one plugin set (built-ins plus the trusted in-repo plugins of that
workspace) every hook of that command goes through.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ow import term
from ow.core import config, index, refresh, runtime, stacks
from ow.core.session import Session, open_session
from ow.errors import PluginError, UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Plugins, RunCommand
    from ow.core.workspace import Workspace


@dataclass(frozen=True)
class Target:
    """An opened workspace, stack or multi-stack root and the plugin set its sessions share."""

    plugins: Plugins
    sessions: tuple[Session, ...]
    multi: stacks.MultiWorkspace | None = None

    @property
    def workspaces(self) -> tuple[Workspace, ...]:
        """The workspace, or every stack of the multi-stack root."""
        return tuple(s.workspace for s in self.sessions if s.workspace is not None)

    @property
    def workspace(self) -> Workspace:
        """The single workspace of a mono-stack target."""
        assert self.multi is None and self.sessions[0].workspace is not None
        return self.sessions[0].workspace


def open_target(root: Path, kind: Literal["workspace", "multi"], plugins: Plugins) -> Target:
    """Open `root` (as classified by `resolve_target_root`), printing the sessions' warnings."""
    if kind == "workspace":
        session = open_session(str(root), need_workspace=True, plugins=plugins, cwd=root)
        _announce((session,))
        return Target(session.plugins, (session,))
    loaded = stacks.load_multi_workspace(root, plugins)
    if loaded is None:
        raise UsageError(f"no valid stacks found in {root}")
    multi, sessions = loaded
    _announce(sessions)
    return Target(sessions[0].plugins, sessions, multi)


def combined_plugins(base: Plugins, targets: Sequence[Target]) -> Plugins:
    """The one plugin set a command over several opened targets goes through.

    The targets' own set when they share it, else `base` plus the in-repo plugins of all of them.
    Plugins the combined set drops (two targets bringing one plugin name, a failing `ow_settings`)
    are warned about, except what the targets' sessions already reported.
    """
    distinct = {id(target.plugins): target.plugins for target in targets}
    if not distinct:
        return base
    if len(distinct) == 1:
        return next(iter(distinct.values()))
    repo_dirs = [
        (repo.name, repo.path)
        for target in targets
        for ws in target.workspaces
        for repo in ws.repos
    ]
    combined, warnings = base.with_repo_plugins(repo_dirs)
    reported = {w for target in targets for session in target.sessions for w in session.warnings}
    for warning in warnings:
        if warning not in reported:
            term.warn(warning)
    return combined


def _announce(sessions: Sequence[Session]) -> None:
    for session in sessions:
        for warning in session.warnings:
            term.warn(warning)


def plugin_running(ws: Workspace, plugins: Plugins, *, report: bool = True) -> list[str]:
    """Running process/service descriptions that plugins report for `ws`.

    A plugin whose `ow_running` raises counts as not running; `report` warns about it.
    """
    found: list[str] = []
    for name, answer in plugins.call("ow_running", ws=ws, errors="collect"):
        if isinstance(answer, PluginError):
            if report:
                term.warn(f"{name}: {answer.cause}")
        elif isinstance(answer, list):
            found.extend(str(item) for item in answer if isinstance(item, str) and item)
    return found


def running_refusals(ws: Workspace, plugins: Plugins) -> list[tuple[str, str]]:
    """`(label, reason)` when `ws` still has a detached process or a plugin-reported one."""
    refusals: list[tuple[str, str]] = []
    proc = runtime.running_for_workspace(ws)
    if proc is not None:
        refusals.append(("runtime", f"running (pid {proc.pid}); run `ow stop` first"))
    items = plugin_running(ws, plugins)
    if items:
        refusals.append(("runtime", f"running ({', '.join(items)}); run `ow stop` first"))
    return refusals


def enclosing_multi(ws: Workspace, plugins: Plugins) -> stacks.MultiWorkspace | None:
    """The multi-stack root `ws` is a stack of, else None: the `multi` argument its hooks get."""
    return refresh._enclosing_multi(ws, plugins)  # type: ignore[return-value]


def scoped_answer(
    plugins: Plugins,
    hook: str,
    ws: Workspace | None,
    multi: stacks.MultiWorkspace | None,
    **kwargs: object,
) -> tuple[str, object] | None:
    """The first plugin answer `(plugin, answer)` of a hook taking `(ws, multi)` for one scope.

    These are `ow_runner`, `ow_stop` and `ow_logs`. With `ws` set the scope is that workspace or
    stack and `multi` its multi-stack root (None for a mono-stack workspace). With `ws` None it
    is the multi-stack root `multi` as a whole: which plugins are enabled, and what `answers`
    is bound to, follow its first stack.
    """
    if ws is not None:
        return plugins.first_with_name(hook, ws=ws, multi=multi, **kwargs)
    assert multi is not None
    scope = multi.stacks[0] if multi.stacks else None
    return plugins.first_with_name(hook, ws=None, scope=scope, multi=multi, **kwargs)


def running_probe(
    plugins: Plugins, workspaces: Sequence[Workspace], multi: stacks.MultiWorkspace | None
) -> Callable[[], bool]:
    """Whether any of `workspaces` still has a detached process or a plugin-reported one.

    Polled while following, so a failing `ow_running` is warned about on the first poll only.
    """
    first_poll = True

    def running() -> bool:
        nonlocal first_poll
        report, first_poll = first_poll, False
        if multi is not None and runtime.list_running(multi.root):
            return True
        for ws in workspaces:
            if runtime.running_for_workspace(ws) is not None:
                return True
            if plugin_running(ws, plugins, report=report):
                return True
        return False

    return running


EXIT_INTERRUPTED = 130
_TERMINATE_GRACE = 5.0


def exit_status(returncode: int) -> int:
    """A `Popen.returncode` as a shell exit status: death by signal `n` is `128 + n`."""
    return 128 - returncode if returncode < 0 else returncode


def default_shell(host_env: Mapping[str, str]) -> str:
    """Pick an executable interactive shell for a bare `ow exec` / `ow sandbox`."""
    for cand in (host_env.get("SHELL", "").strip(), "/bin/bash", "/usr/bin/bash", "/bin/sh"):
        if not cand:
            continue
        p = Path(cand)
        if p.is_absolute() and p.exists() and os.access(p, os.X_OK):
            return cand
    return "/bin/sh"


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM `proc`, SIGKILL it when it outlives the grace period (or on one more Ctrl+C)."""
    proc.terminate()
    try:
        proc.wait(timeout=_TERMINATE_GRACE)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        proc.kill()
        proc.wait()


def run_foreground(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    interactive: bool = False,
) -> int:
    """Run `argv` on the inherited terminal and return its exit code.

    The child's code passes through; death by signal `n` is `128 + n`. The child shares our
    process group, so Ctrl+C reaches it too: the first one only makes us keep waiting (the
    child may be tearing down), a second one terminates it (SIGTERM, then SIGKILL), and an
    interrupted run exits 130. With `interactive` (a shell) Ctrl+C belongs to the child and
    is ignored here.
    """
    if interactive:
        previous = signal.signal(signal.SIGINT, lambda *_: None)  # a handler, not SIG_IGN: exec resets it
    try:
        try:
            proc = subprocess.Popen(list(argv), cwd=cwd, env=env)
        except OSError as exc:
            raise UsageError(f"failed to execute {argv[0]!r}: {exc}") from exc
        interrupted = False
        while True:
            try:
                proc.wait()
                break
            except KeyboardInterrupt:
                if interrupted:
                    _terminate(proc)
                    break
                interrupted = True
                term.warn("interrupted; waiting for the process to exit (Ctrl+C again to terminate it)")
    finally:
        if interactive:
            signal.signal(signal.SIGINT, previous)
    return EXIT_INTERRUPTED if interrupted else exit_status(proc.returncode)


def run_command(cmd: RunCommand) -> int:
    """`run_foreground` for a plugin's `RunCommand`, with its `env` laid over ours."""
    return run_foreground(cmd.argv, cwd=cmd.cwd, env={**os.environ, **cmd.env})


def remove_workspace_root(ws: Workspace, plugins: Plugins) -> None:
    """Delete `ws.root` and leave what contained it consistent.

    A mono-stack workspace leaves the index. A stack leaves its multi-stack root's config and
    lock; the root's generated files are re-synced, or, when it was the last stack, removed
    with the root's runtime files, its config, its index entry and the root directory itself
    once empty.
    """
    is_stack = ws.is_stack
    parent = ws.root.parent.resolve()
    shutil.rmtree(ws.root)
    if not is_stack:
        index.remove(ws.root)
        return

    config.remove_stack(parent / ".ow" / "config.toml", ws.name)
    refresh.remove_lock_prefix(parent, ws.name)
    if stacks.detect_multi_workspace(parent) is not None:
        # Other stacks remain: the root keeps its config, even if one of them fails to open.
        remaining = stacks.load_multi_workspace(parent, plugins, discover_repo_plugins=False)
        if remaining is not None:
            stacks.sync_multi_workspace_files(remaining[0], plugins)
        return

    runtime.cleanup_runtime_dirs(parent)
    refresh.remove_owned_files(parent)
    cfg = parent / ".ow" / "config.toml"
    cfg.unlink(missing_ok=True)
    cfg.with_name(f".{cfg.name}.lock").unlink(missing_ok=True)
    try:
        (parent / ".ow").rmdir()
    except OSError:
        pass
    index.remove(parent)
    try:
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
