"""Stable, versioned public API for external and in-repo ow plugins.

Every external plugin must declare ``OW_API_VERSION = 1`` (matching ``PLUGIN_API_VERSION``)
and import hooks, data records, and helpers exclusively from this module rather than
internal ``ow.core.*`` or ``ow.plugins._*`` modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ow.core.plugins import (
    PLUGIN_API_VERSION,
    SUPPORTED_API_RANGE,
    Diagnostic,
    GeneratedFile,
    KnownRepo,
    PruneCandidate,
    ResolvedRepo,
    ResolvedTarget,
    RunCommand,
    SandboxMounts,
    Setting,
    StackInfo,
    StatusInfo,
    hookimpl,
)
from ow.core.stacks import MultiWorkspace
from ow.core.workspace import Repo, Workspace
from ow.plugins._containers import detect_engine as detect_container_engine
from ow.plugins._containers import is_port_free
from ow.specs.editors import DebugLaunch, EditorContribution
from ow.specs.python_env import PythonEnvironment, WorkspaceVenv, merge, workspace_venv
from ow.specs.services import DatabaseService, SmtpService

if TYPE_CHECKING:
    from ow.core.plugins import Plugins


def resolve_stack_python(ws: Workspace, plugins: Plugins | None = None) -> Path | None:
    """Return the resolved Python executable path for `ws` (e.g. its shared or local venv)."""
    merged: PythonEnvironment | None = None
    if plugins is not None:
        merged = merge(plugins.answers(ws)("python_environment"))
    else:
        from ow.plugins.odoo.files import python_environment as odoo_python_env
        from ow.plugins.odoo.release import get_core

        core, diags = get_core(ws)
        if core is not None and not diags:
            merged = odoo_python_env(core)

    if merged is None and not ws.settings.get("mise.python"):
        return None
    venv = workspace_venv(ws, merged)
    return venv.path / "bin" / "python"


__all__ = [
    "PLUGIN_API_VERSION",
    "SUPPORTED_API_RANGE",
    "DatabaseService",
    "DebugLaunch",
    "Diagnostic",
    "EditorContribution",
    "GeneratedFile",
    "KnownRepo",
    "MultiWorkspace",
    "PruneCandidate",
    "PythonEnvironment",
    "Repo",
    "ResolvedRepo",
    "ResolvedTarget",
    "RunCommand",
    "SandboxMounts",
    "Setting",
    "SmtpService",
    "StackInfo",
    "StatusInfo",
    "Workspace",
    "WorkspaceVenv",
    "detect_container_engine",
    "hookimpl",
    "is_port_free",
    "resolve_stack_python",
    "workspace_venv",
]
