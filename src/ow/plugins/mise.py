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
from ow.specs.python_env import (
    PythonEnvSpec,
    merge,
    resolve_venv_path,
    select_python_version,
)

if TYPE_CHECKING:
    from ow.core.workspace import Workspace


HOOKSPECS = (PythonEnvSpec,)

_FRAGMENT = PurePosixPath("mise/conf.d/00-ow.toml")
_MISE_MIN_VERSION = (2026, 8, 13)
_MISE_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")
_MISE_WARNING = "mise ≥ 2026.8.13 not found; mise/conf.d/00-ow.toml won't be loaded; disable with `ow config set --global mise.enabled false`"


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
    answers_list = answers("python_environment")
    merged = merge(answers_list)

    python_setting = ws.settings.get("mise.python")
    shared_venv = bool(ws.settings.get("mise.shared_venv", True))

    if merged is None and not python_setting:
        return []

    bounds = merged.python if merged is not None else None
    python_value, diagnostics = select_python_version(
        bounds, python_setting, setting_name="mise.python"
    )

    fragment: dict[str, object] = {}

    if python_value:
        venv_key = merged.venv_key if merged is not None else None
        venv_path = resolve_venv_path(ws, python_value, venv_key, shared=shared_venv)
        venv_spec = ".venv" if venv_path == (ws.root / ".venv") else str(venv_path)
        fragment["tools"] = {"python": python_value}
        fragment["env"] = fragment.get("env", {})
        fragment["env"]["_"] = {
            "python": {"venv": {"path": venv_spec, "create": True}}
        }

    if merged is not None and merged.env:
        if "env" not in fragment:
            fragment["env"] = {}
        fragment["env"].update(merged.env)

    if merged is not None and merged.path:
        if "env" not in fragment:
            fragment["env"] = {}
        if "_" not in fragment["env"]:
            fragment["env"]["_"] = {}
        fragment["env"]["_"]["path"] = list(merged.path)

    if merged is not None and merged.postinstall:
        fragment["hooks"] = {
            "postinstall": " && ".join(merged.postinstall)
        }

    if merged is not None and merged.aliases:
        fragment["shell_alias"] = merged.aliases

    if merged is not None and merged.tasks:
        fragment["tasks"] = merged.tasks

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
