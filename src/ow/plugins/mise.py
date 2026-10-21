"""Mise plugin: environment management and Python version support."""

from __future__ import annotations

import re
import subprocess
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import tomli_w

from ow.core.plugins import (
    Diagnostic,
    GeneratedFile,
    Setting,
    hookimpl,
)
from ow.specs.mise import MiseSpec, MiseEnvironment, merge

if TYPE_CHECKING:
    from ow.core.workspace import Workspace


# Hook spec declaration for mise
HOOKSPECS = (MiseSpec,)

_FRAGMENT = PurePosixPath("mise/conf.d/00-ow.toml")
_MISE_MIN_VERSION = (2026, 8, 13)
_MISE_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")
_MISE_WARNING = "mise ≥ 2026.8.13 not found; mise/conf.d/00-ow.toml won't be loaded"


@hookimpl
def ow_settings() -> list[Setting]:
    """Provide mise settings: python version string."""
    return [
        Setting(
            key="mise.python",
            type=str,
            default=None,
            help="Python version for mise (e.g. '3.11', '3.12')",
        ),
    ]


@hookimpl
def ow_generated_files(
    ws: Workspace, answers: callable
) -> list[GeneratedFile | Diagnostic]:
    """Generate mise/conf.d/00-ow.toml when needed."""
    # Get answers from mise_environment hookspec
    answers_list = answers("mise_environment")
    merged = merge(answers_list)
    
    # Get the mise.python setting
    python_setting = ws.settings.get("mise.python")
    
    # No answer and no python setting means no file
    if merged is None and not python_setting:
        return []
    
    diagnostics: list[Diagnostic] = []
    python_value = python_setting
    
    # Handle Python version with range
    if merged is not None and merged.python is not None:
        python_min, python_max = merged.python
        
        if python_value is None:
            # No explicit value: use range max
            python_value = f"{python_max[0]}.{python_max[1]}"
        else:
            # Validate explicit value against range
            # Parse "3.MINOR" format
            match = re.match(r"^3\.(\d+)$", python_value)
            if match:
                minor = int(match.group(1))
                min_minor = python_min[1]
                max_minor = python_max[1]
                
                if minor < min_minor or minor > max_minor:
                    # Clamp and add info diagnostic
                    clamped_minor = max(min_minor, min(minor, max_minor))
                    diagnostics.append(
                        Diagnostic(
                            "info",
                            f"mise.python {python_value} clamped to range {python_min[0]}.{min_minor}-{max_minor}",
                        )
                    )
                    python_value = f"{python_max[0]}.{clamped_minor}"
            else:
                # Malformed: not "3.MINOR" format
                diagnostics.append(
                    Diagnostic(
                        "warning",
                        f"mise.python {python_value!r} malformed (expected 3.MINOR), using range max",
                    )
                )
                python_value = f"{python_max[0]}.{python_max[1]}"
    
    # Build the fragment
    fragment: dict[str, object] = {}
    
    # Add tools section with python if set
    if python_value:
        fragment["tools"] = {"python": python_value}
        # Add venv setup
        fragment["env"] = fragment.get("env", {})
        fragment["env"]["_"] = {
            "python": {"venv": {"path": ".venv", "create": True}}
        }
    
    # Add environment variables
    if merged is not None and merged.env:
        if "env" not in fragment:
            fragment["env"] = {}
        fragment["env"].update(merged.env)
    
    # Add PATH entries
    if merged is not None and merged.path:
        if "env" not in fragment:
            fragment["env"] = {}
        if "_" not in fragment["env"]:
            fragment["env"]["_"] = {}
        fragment["env"]["_"]["path"] = list(merged.path)
    
    # Add postinstall hook
    if merged is not None and merged.postinstall:
        fragment["hooks"] = {
            "postinstall": " && ".join(merged.postinstall)
        }
    
    # Add aliases (shell_alias in mise)
    if merged is not None and merged.aliases:
        fragment["shell_alias"] = merged.aliases
    # Add tasks
    if merged is not None and merged.tasks:
        fragment["tasks"] = merged.tasks
    
    # If nothing to contribute, return early
    if not fragment:
        return diagnostics
    
    # Serialize as TOML
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
    except (FileNotFoundError, OSError):
        term.warn(_MISE_WARNING)
        return

    if version is None or version < _MISE_MIN_VERSION:
        term.warn(_MISE_WARNING)
        return

    try:
        subprocess.run(
            ["mise", "trust", str(ws.root / "mise" / "conf.d" / "00-ow.toml")],
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        pass
