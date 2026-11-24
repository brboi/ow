"""Stable, versioned public API for external and in-repo ow plugins.

A plugin declares ``OW_API_VERSION = 1`` and imports hooks, data records, errors and helpers
exclusively from this module, never from internal ``ow.core.*``, ``ow.specs.*`` or
``ow.plugins.*`` modules. ow loads a plugin whose ``OW_API_VERSION`` lies within
``SUPPORTED_API_RANGE`` (currently ``(1, 1)``) and skips the others with a warning.

Compatibility policy: API v1 is frozen through every 3.x release. 3.x only *adds* to it
(new hooks, new optional fields, new exports); anything slated for removal first emits a
deprecation warning and is removed in 4.0, which raises ``OW_API_VERSION``.

Which hooks abort a command when they raise and which only warn is documented per hook in
``ow.core.plugins.CoreSpec``.
"""

from __future__ import annotations

from pathlib import Path

from ow.core.facts import RepoFacts
from ow.core.plugins import (
    PLUGIN_API_VERSION,
    SUPPORTED_API_RANGE,
    Answers,
    Diagnostic,
    GeneratedFile,
    KnownRepo,
    PruneCandidate,
    ResolvedRepo,
    ResolvedSpec,
    RunCommand,
    SandboxMounts,
    Setting,
    StackInfo,
    StatusInfo,
    hookimpl,
)
from ow.core.runtime import follow_files as follow_log_files
from ow.core.stacks import MultiWorkspace
from ow.core.workspace import Repo, Workspace
from ow.errors import PluginError, UsageError
from ow.plugins._containers import detect_container_engine, is_port_free
from ow.specs.editors import DebugLaunch, EditorContribution
from ow.specs.python_env import PythonEnvironment, WorkspaceVenv, resolve_workspace_python, workspace_venv
from ow.specs.services import DatabaseService, SmtpService


def resolve_stack_python(ws: Workspace, answers: Answers) -> Path | None:
    """Return the Python executable of `ws` (its shared or local venv), or None when it has none.

    `answers` is the callable ow hands to hooks such as `ow_generated_files`; the python
    environment is whatever the plugins enabled for `ws` answer to `python_environment`, so `ws`
    may be a sibling stack of the workspace the hook runs for.
    """
    resolved = resolve_workspace_python(ws, answers.for_workspace(ws))
    return resolved[1].path / "bin" / "python" if resolved is not None else None


__all__ = [
    "PLUGIN_API_VERSION",
    "SUPPORTED_API_RANGE",
    "Answers",
    "DatabaseService",
    "DebugLaunch",
    "Diagnostic",
    "EditorContribution",
    "GeneratedFile",
    "KnownRepo",
    "MultiWorkspace",
    "PluginError",
    "PruneCandidate",
    "PythonEnvironment",
    "Repo",
    "RepoFacts",
    "ResolvedRepo",
    "ResolvedSpec",
    "RunCommand",
    "SandboxMounts",
    "Setting",
    "SmtpService",
    "StackInfo",
    "StatusInfo",
    "UsageError",
    "Workspace",
    "WorkspaceVenv",
    "detect_container_engine",
    "follow_log_files",
    "hookimpl",
    "is_port_free",
    "resolve_stack_python",
    "workspace_venv",
]
