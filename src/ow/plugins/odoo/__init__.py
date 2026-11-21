"""Odoo plugin: project configuration and debugging support.

This module holds the hook implementations pluggy registers; each one delegates to the
module owning the behavior.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from ow.core.plugins import (
    Diagnostic,
    GeneratedFile,
    KnownRepo,
    ResolvedRepo,
    ResolvedTarget,
    RunCommand,
    SandboxMounts,
    Setting,
    StackInfo,
    hookimpl,
)
from ow.plugins._odoo_repos import ODOO_REPOS
from ow.plugins.odoo import files, runner, stacks, targets
from ow.plugins.odoo import settings as odoo_settings
from ow.plugins.odoo.release import get_core
from ow.specs.editors import EditorContribution
from ow.specs.python_env import PythonEnvironment

if TYPE_CHECKING:
    from ow.core.workspace import Workspace


@hookimpl
def ow_settings() -> list[Setting]:
    """Provide Odoo settings."""
    return odoo_settings.declarations()


@hookimpl
def ow_diagnostics(ws: Workspace) -> list[Diagnostic]:
    """Name the core problem, else validate the readable core's settings."""
    core, core_diagnostics = get_core(ws)
    if core_diagnostics:
        return core_diagnostics
    if core is None:
        return []
    return odoo_settings.validate_settings(ws)


@hookimpl
def ow_generated_files(
    ws: Workspace, answers: Callable[[str], list]
) -> list[GeneratedFile | Diagnostic]:
    """Generate Odoo configuration files."""
    # `ow_diagnostics` already reported a broken core; contribute nothing.
    core, _ = get_core(ws)
    if core is None:
        return []
    return files.generated_files(ws, core, answers=answers)


@hookimpl
def python_environment(ws: Workspace) -> PythonEnvironment | None:
    """Contribute tool-agnostic Python environment from Odoo."""
    core, core_diagnostics = get_core(ws)
    if core_diagnostics or not core:
        return None
    return files.python_environment(core)


@hookimpl
def editors_contribution(ws: Workspace) -> EditorContribution | None:
    """Contribute debug launches and settings for editors."""
    core, core_diagnostics = get_core(ws)
    if core_diagnostics or not core:
        return None
    return files.editor_contribution(ws, core)


@hookimpl
def ow_resolve_target(
    target: str,
    known_repos: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
) -> ResolvedTarget | None:
    """Resolve `@<base>` into `odoo.default_repos` detached on `<base>`."""
    return targets.resolve_target(target, known_repos, settings)


@hookimpl
def ow_is_base_branch(repo: str, branch: str) -> bool | None:
    """Recognize Odoo release/series branches (`18.0`, `saas-18.3`, `master`, `main`)."""
    return True if targets.is_base_branch(branch) else None


@hookimpl
def ow_expand_workspace_repos(
    repos: tuple[ResolvedRepo, ...],
    known_repos: Mapping[str, KnownRepo],
) -> tuple[ResolvedRepo, ...] | None:
    """Infer Odoo series bases for branch targets and ensure addon targets get an Odoo core repo."""
    return targets.expand_workspace_repos(repos, known_repos)


@hookimpl
def ow_stack_values(
    ws: Workspace | None,
    ws_values: Mapping[str, object],
    siblings: Sequence[Workspace],
    pending: Sequence[Mapping[str, object]],
    global_settings: Mapping[str, object],
) -> dict[str, object] | None:
    """Pin `odoo.http_port` and `odoo.gevent_port` (non-colliding), and `odoo.db_name` on promotion."""
    return stacks.stack_values(ws, ws_values, siblings, pending, global_settings)


@hookimpl
def ow_stack_name(ws: Workspace) -> str | None:
    """Suggest `odoo` when promoting a workspace containing an Odoo core checkout."""
    return stacks.stack_name(ws)


@hookimpl
def ow_stack_info(ws: Workspace) -> StackInfo | None:
    """Expose the HTTP and gevent ports."""
    return stacks.stack_info(ws)


@hookimpl
def ow_runner(ws: Workspace, args: tuple[str, ...]) -> RunCommand | None:
    """Run Odoo locally using the workspace's resolved Python virtualenv (or `mise exec`)."""
    core, core_diagnostics = get_core(ws)
    if core_diagnostics or core is None:
        return None
    return runner.run_command(ws, core, args)


@hookimpl
def ow_known_repos() -> list[KnownRepo]:
    """Provide known Odoo repositories."""
    return [KnownRepo(name=repo.name, url=repo.clone_url) for repo in ODOO_REPOS]


@hookimpl
def ow_sandbox_mounts(ws: Workspace) -> SandboxMounts | None:
    """Expose Odoo's shared Python virtualenv read-only and forward `ODOO_RC`."""
    core, core_diagnostics = get_core(ws)
    if core_diagnostics or core is None:
        return None
    venv = files.odoo_venv(ws, core)
    ro_paths = (venv.path,) if (not venv.is_local(ws) and venv.path.exists()) else ()
    return SandboxMounts(
        ro_paths=ro_paths,
        env={"ODOO_RC": str(ws.root / "odoorc")},
    )
