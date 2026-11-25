"""Mise plugin: environment management and shared Python virtualenv support."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import tomli_w

from ow.plugin_api import (
    Diagnostic,
    GeneratedFile,
    SandboxMounts,
    Setting,
    Workspace,
    WorkspaceVenv,
    hookimpl,
)
from ow.specs.python_env import progress, resolve_workspace_python, run_bootstrap

_FRAGMENT = PurePosixPath("mise/conf.d/00-ow.toml")
_MISE_MIN_VERSION = (2026, 8, 13)
_MISE_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")
_MISE_WARNING = (
    f"mise ≥ {'.'.join(map(str, _MISE_MIN_VERSION))} not found; mise/conf.d/00-ow.toml won't be loaded; "
    "disable with `ow config set --global mise.enabled false`"
)
_CONFIG_ROOT = "{{config_root}}"


def _rooted(path: str) -> str:
    """Workspace-relative `path` as a mise template path."""
    return f"{_CONFIG_ROOT}/{path}"


@hookimpl
def ow_settings() -> list[Setting]:
    """Declare the two settings that moved to the `python.` namespace, so a config still using them is refused."""
    return [
        Setting(
            key="mise.python",
            type=str,
            default=None,
            help="Renamed to python.version",
            renamed_to="python.version",
        ),
        Setting(
            key="mise.shared_venv",
            type=bool,
            default=True,
            help="Renamed to python.shared_venv",
            renamed_to="python.shared_venv",
        ),
    ]


@hookimpl
def ow_generated_files(
    ws: Workspace, answers: Callable[[str], list]
) -> list[GeneratedFile | Diagnostic]:
    """Generate mise/conf.d/00-ow.toml when needed."""
    resolved = resolve_workspace_python(ws, answers)
    if resolved is None:
        return []
    merged, venv = resolved
    fragment: dict[str, object] = {}

    if venv.python_version:
        venv_spec = ".venv" if venv.is_local(ws) else str(venv.path)
        fragment["tools"] = {"python": venv.python_version}
        fragment["env"] = {"_": {"python": {"venv": {"path": venv_spec, "create": True}}}}

    if merged is not None:
        env_vars = {**merged.env, **{k: _rooted(v) for k, v in merged.env_paths.items()}}
        if env_vars:
            fragment.setdefault("env", {}).update(env_vars)

        if merged.path:
            fragment.setdefault("env", {}).setdefault("_", {})["path"] = [
                _rooted(p) for p in merged.path
            ]

        commands = []
        if merged.requirements:
            pip_args = " ".join(f'-r "{_rooted(r)}"' for r in merged.requirements)
            commands.append(f"python -m ensurepip --default-pip && pip install {pip_args}")
        commands.extend(merged.postinstall)
        if commands:
            fragment["hooks"] = {"postinstall": " && ".join(commands)}

        if merged.aliases:
            fragment["shell_alias"] = merged.aliases

    diagnostics = list(venv.diagnostics)
    if not fragment:
        return diagnostics

    content = tomli_w.dumps(fragment)

    return [
        GeneratedFile(
            path=_FRAGMENT,
            data=content.encode("utf-8"),
        )
    ] + diagnostics


def _mise_version() -> tuple[int, int, int] | None:
    """`mise --version` major/minor/patch, or None when it can't be read."""
    result = subprocess.run(
        ["mise", "--version"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None
    match = _MISE_VERSION_RE.search(result.stdout)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


@hookimpl
def ow_refreshed(ws: Workspace, owned: tuple[PurePosixPath, ...]) -> None:
    """Trust the fragment when mise knows how to load `conf.d` fragments."""
    from ow import term

    if _FRAGMENT not in owned:
        return

    try:
        version = _mise_version()
    except OSError:
        term.warn(_MISE_WARNING)
        return

    if version is None or version < _MISE_MIN_VERSION:
        term.warn(_MISE_WARNING)
        return

    try:
        result = subprocess.run(
            ["mise", "trust", str(ws.root / "mise" / "conf.d" / "00-ow.toml")],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            lines = result.stderr.strip().splitlines()
            detail = lines[0] if lines else f"exit {result.returncode}"
            term.warn(f"mise trust failed: {detail}")
    except OSError:
        pass


@hookimpl
def python_bootstrap(ws: Workspace, venv: WorkspaceVenv, log_to_stderr: bool) -> bool | None:
    """Install the Python version and let mise create the virtualenv (`[env._.python.venv] create`)."""
    if shutil.which("mise") is None:
        return None
    progress(f"# {ws.name}: preparing Python virtualenv at {venv.path}", log_to_stderr=log_to_stderr)
    run_bootstrap(["mise", "install"], cwd=ws.root, log_to_stderr=log_to_stderr)
    run_bootstrap(["mise", "exec", "--", "python", "-c", ""], cwd=ws.root, log_to_stderr=log_to_stderr)
    return True


@hookimpl
def python_wrap(argv: tuple[str, ...]) -> tuple[str, ...] | None:
    """Run `argv` under `mise exec` when mise is installed."""
    if shutil.which("mise") is None:
        return None
    return ("mise", "exec", "--", *argv)


@hookimpl
def ow_sandbox_mounts(ws: Workspace) -> SandboxMounts | None:
    """Expose mise installs/config read-only, keep mise caches inside `/tmp`, and trust the workspace fragment."""
    home = Path(os.environ.get("HOME") or Path.home()).resolve()
    xdg_data = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share")).resolve()
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config")).resolve()

    ro_paths = tuple(
        candidate
        for candidate in (xdg_data / "mise", xdg_config / "mise")
        if candidate.exists()
    )
    if not ro_paths:
        return None
    return SandboxMounts(
        ro_paths=ro_paths,
        env={
            "MISE_DATA_DIR": str(xdg_data / "mise"),
            "MISE_CONFIG_DIR": str(xdg_config / "mise"),
            "MISE_CACHE_DIR": "/tmp/ow-sandbox/mise-cache",
            "MISE_STATE_DIR": "/tmp/ow-sandbox/mise-state",
            # The sandbox's mise state is empty, so `mise trust` from the host does not apply there.
            "MISE_TRUSTED_CONFIG_PATHS": str(ws.root / _FRAGMENT),
        },
    )
