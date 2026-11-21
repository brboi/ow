"""`ow exec` — run a command or interactive shell inside a stack's virtualenv and environment."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ow import term
from ow.core import stacks
from ow.core.session import open_session
from ow.core.workspace import Workspace, resolve_target_root
from ow.errors import UsageError
from ow.specs.python_env import (
    PythonEnvironment,
    WorkspaceVenv,
    ensure_venv_ready,
    merge,
    workspace_venv,
)

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _default_shell(host_env: Mapping[str, str]) -> str:
    """Pick an executable interactive shell for bare `ow exec`."""
    candidates = [host_env.get("SHELL", "").strip(), "/bin/bash", "/usr/bin/bash", "/bin/sh"]
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)
        if p.is_absolute() and p.exists() and os.access(p, os.X_OK):
            return cand
    return "/bin/sh"


def _open_ws(path: Path, plugins: Plugins) -> Workspace:
    session = open_session(str(path), need_workspace=True, plugins=plugins, cwd=path)
    for warning in session.warnings:
        term.warn(warning)
    assert session.workspace is not None
    return session.workspace


def resolve_python_env(
    ws: Workspace, plugins: Plugins
) -> tuple[PythonEnvironment | None, WorkspaceVenv] | None:
    """Return `(merged_env, venv)` when `ws` has a Python environment, else None."""
    merged = merge(plugins.answers(ws)("python_environment"))
    if merged is None and not ws.settings.get("mise.python"):
        return None
    return merged, workspace_venv(ws, merged)


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
    return resolved_cwd if _is_within(resolved_cwd, ws_root) else ws_root


def _resolve_workspace(
    lookup: str | None,
    plugins: Plugins,
    *,
    base_cwd: Path,
    positional_target: str | None,
) -> Workspace:
    try:
        root, kind = resolve_target_root(lookup, cwd=base_cwd)
    except UsageError as exc:
        if positional_target is not None:
            raise UsageError(
                f"{exc}; to run a command, put it after `--` (e.g. `ow exec -- {positional_target}`)"
            ) from exc
        raise

    if kind == "workspace":
        return _open_ws(root, plugins)

    loaded = stacks.load_multi_workspace(root, plugins)
    if loaded is None:
        raise UsageError(f"no valid stacks found in {root}")
    multi, sessions = loaded
    for sess in sessions:
        for warning in sess.warnings:
            term.warn(warning)

    py_stacks = [ws for ws in multi.stacks if resolve_python_env(ws, plugins) is not None]
    if len(py_stacks) == 1:
        return py_stacks[0]
    if len(py_stacks) > 1:
        names = ", ".join(ws.name for ws in py_stacks)
        raise UsageError(
            f"multiple Python stacks in {multi.name!r} ({names}); "
            "specify one with `ow exec <stack> -- ...`"
        )
    names = ", ".join(ws.name for ws in multi.stacks)
    raise UsageError(f"no stack with a Python environment in {multi.name!r} ({names})")


def run(
    plugins: Plugins,
    *,
    target: str | None = None,
    workspace: str | None = None,
    command: Sequence[str] = (),
    cwd: Path | None = None,
) -> int:
    """Run `command` (or an interactive shell) inside a workspace/stack's Python environment."""
    if target is not None and workspace is not None:
        raise UsageError("pass either WS or -w/--workspace, not both")

    base_cwd = (cwd or Path.cwd()).resolve()
    lookup = target if target is not None else workspace
    ws = _resolve_workspace(
        lookup,
        plugins,
        base_cwd=base_cwd,
        positional_target=target,
    )

    resolved = resolve_python_env(ws, plugins)
    if resolved is None:
        raise UsageError(f"no Python environment configured for workspace {ws.name!r}")
    merged, venv = resolved

    for diag in venv.diagnostics:
        if diag.level == "warning":
            term.warn(diag.text)

    requirements = merged.requirements if merged is not None else ()
    ensure_venv_ready(ws, venv.path, requirements=requirements, log_to_stderr=True)

    venv_python = venv.path / "bin" / "python"
    if not venv_python.exists():
        raise UsageError(
            f"virtualenv not found at {venv.path}; ensure mise is installed or create the virtualenv first"
        )

    env = build_exec_env(ws, merged, venv)
    exec_cwd = resolve_exec_cwd(ws, base_cwd)

    if command:
        argv = tuple(command)
    else:
        argv = (_default_shell(env),)
        kind_label = "local" if venv.is_local(ws) else "shared"
        print(f"# {ws.name}: venv {venv.path} ({kind_label})", file=sys.stderr)

    try:
        proc = subprocess.run(argv, cwd=exec_cwd, env=env)
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        raise UsageError(f"failed to execute {argv[0]!r}: {exc}") from exc

    if proc.returncode < 0:
        return 128 + abs(proc.returncode)
    return proc.returncode
