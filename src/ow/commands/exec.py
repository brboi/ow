"""`ow exec` — run a command or interactive shell inside a stack's virtualenv and environment."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.commands._runtime import Target, default_shell, open_target, run_foreground
from ow.core.workspace import Workspace, resolve_target_root
from ow.errors import UsageError
from ow.specs.python_env import (
    PythonEnvironment,
    WorkspaceVenv,
    ensure_venv_ready,
    resolve_workspace_python,
    wrap_command,
)

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def build_exec_env(
    ws: Workspace,
    merged: PythonEnvironment | None,
    venv: WorkspaceVenv,
    *,
    host_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the subprocess environment with `venv`, plugin `PATH`, and plugin env vars."""
    env = dict(os.environ if host_env is None else host_env)
    env.pop("PYTHONHOME", None)
    env["VIRTUAL_ENV"] = str(venv.path)
    env["VIRTUAL_ENV_PROMPT"] = ws.name
    env["OW_WORKSPACE"] = ws.name

    path_entries: list[str] = [str(venv.path / "bin")]
    if merged is not None:
        for rel in merged.path:
            path_entries.append(str(ws.root / rel))
        for key, val in merged.env.items():
            env[key] = val
        for key, rel in merged.env_paths.items():
            env[key] = str(ws.root / rel)

    existing_path = env.get("PATH", "")
    if existing_path:
        path_entries.append(existing_path)
    env["PATH"] = os.pathsep.join(path_entries)
    return env


def resolve_exec_cwd(ws: Workspace, base_cwd: Path) -> Path:
    """Keep `base_cwd` when already inside `ws.root`, otherwise switch to `ws.root`."""
    ws_root = ws.root.resolve()
    resolved_cwd = base_cwd.resolve()
    return resolved_cwd if resolved_cwd.is_relative_to(ws_root) else ws_root


def _resolve_workspace(
    lookup: str | None,
    plugins: Plugins,
    *,
    base_cwd: Path,
) -> tuple[Target, Workspace]:
    """Open the target `lookup` names and pick the one workspace or stack to run in.

    The returned `Target` carries the plugin set (built-ins plus the target's in-repo plugins)
    every later hook of the command goes through.
    """
    try:
        root, kind = resolve_target_root(lookup, cwd=base_cwd)
    except UsageError as exc:
        if lookup is not None:
            raise UsageError(
                f"{exc}; to run a command, put it after `--` (e.g. `ow exec -- {lookup}`)"
            ) from exc
        raise

    target = open_target(root, kind, plugins)
    if target.multi is None:
        return target, target.workspace
    multi = target.multi

    py_stacks = [ws for ws in multi.stacks if resolve_workspace_python(ws, target.plugins.answers(ws)) is not None]
    if len(py_stacks) == 1:
        return target, py_stacks[0]
    if len(py_stacks) > 1:
        names = ", ".join(ws.name for ws in py_stacks)
        raise UsageError(
            f"multiple Python stacks in {multi.name!r} ({names}); "
            "specify one with `ow exec <stack> -- ...`"
        )
    if len(multi.stacks) == 1:
        return target, multi.stacks[0]
    names = ", ".join(ws.name for ws in multi.stacks)
    raise UsageError(f"no stack with a Python environment in {multi.name!r} ({names}); specify one with `ow exec <stack> -- ...`")


def run(
    plugins: Plugins,
    *,
    ws_name: str | None = None,
    command: Sequence[str] = (),
    cwd: Path | None = None,
) -> int:
    """Run `command` (or an interactive shell) inside a workspace/stack's environment."""
    base_cwd = (cwd or Path.cwd()).resolve()
    target, ws = _resolve_workspace(ws_name, plugins, base_cwd=base_cwd)
    answers = target.plugins.answers(ws)

    exec_cwd = resolve_exec_cwd(ws, base_cwd)
    resolved = resolve_workspace_python(ws, answers)
    if resolved is None:
        env = dict(os.environ)
        for stale in ("PYTHONHOME", "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT"):
            env.pop(stale, None)
        env["OW_WORKSPACE"] = ws.name
        # A plugin managing environments may run the command in the one it manages.
        if command:
            argv = wrap_command(answers, command) or tuple(command)
        else:
            shell = default_shell(env)
            wrapped = wrap_command(answers, (shell,))
            argv = wrapped or (shell,)
            mode_label = "managed environment" if wrapped else "shell"
            print(f"# {ws.name}: {mode_label} (no Python venv)", file=sys.stderr)
    else:
        merged, venv = resolved

        for diag in venv.diagnostics:
            if diag.level == "warning":
                term.warn(diag.text)

        requirements = merged.requirements if merged is not None else ()
        ensure_venv_ready(ws, venv, answers, requirements=requirements, log_to_stderr=True)

        venv_python = venv.path / "bin" / "python"
        if not venv_python.exists():
            raise UsageError(
                f"virtualenv not found at {venv.path}; create it, or enable a plugin that builds Python environments"
            )

        env = build_exec_env(ws, merged, venv)
        if command:
            argv = tuple(command)
        else:
            argv = (default_shell(env),)
            kind_label = "local" if venv.is_local(ws) else "shared"
            print(f"# {ws.name}: venv {venv.path} ({kind_label})", file=sys.stderr)

    return run_foreground(argv, cwd=exec_cwd, env=env, interactive=not command)
