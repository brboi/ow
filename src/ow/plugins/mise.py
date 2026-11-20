"""Mise plugin: environment management and shared Python virtualenv support."""

from __future__ import annotations

import re
import subprocess
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Callable

import tomli_w

from ow.core.plugins import (
    Diagnostic,
    GeneratedFile,
    Setting,
    hookimpl,
)
from ow.specs.python_env import merge, workspace_venv

if TYPE_CHECKING:
    from ow.core.workspace import Workspace


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
    """Provide mise settings: python version override and shared virtualenv toggle."""
    return [
        Setting(
            key="mise.python",
            type=str,
            default=None,
            help="Python version for mise (e.g. '3.11', '3.12')",
        ),
        Setting(
            key="mise.shared_venv",
            type=bool,
            default=True,
            help="Share Python virtualenvs by version and series under $XDG_DATA_HOME/ow/venvs",
        ),
    ]


@hookimpl
def ow_generated_files(
    ws: Workspace, answers: Callable[[str], list]
) -> list[GeneratedFile | Diagnostic]:
    """Generate mise/conf.d/00-ow.toml when needed."""
    merged = merge(answers("python_environment"))

    if merged is None and not ws.settings.get("mise.python"):
        return []

    venv = workspace_venv(ws, merged)
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
