"""Session: the immutable state every command starts from."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping

from ow.core import git
from ow.core.config import Layer, read_global, read_workspace, resolve_settings
from ow.core.paths import global_config, repos_dir
from ow.core.plugins import Diagnostic, Plugins, load_plugins
from ow.core.workspace import (
    Workspace,
    RepoLocation,
    derive_repos,
    build_workspace,
    resolve_root,
    stray_checkouts,
)
from ow.errors import PluginError, UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Setting


@dataclass(frozen=True)
class Session:
    """Immutable session state for every command."""

    plugins: Plugins
    declared: Mapping[str, Setting]
    workspace: Workspace | None
    locations: tuple[RepoLocation, ...]
    origins: Mapping[str, Literal["default", "global", "workspace"]]
    warnings: tuple[str, ...]
    diagnostics: tuple[tuple[str, Diagnostic], ...]
    config_path: Path | None
    rejected_bases: Mapping[str, str]
    accepted_bases: Mapping[str, str]
    global_layer: Layer
    workspace_layer: Layer

    def reload(self) -> Session:
        """Re-open the same workspace with fresh data, to pick up branch changes."""
        if self.workspace is None:
            raise UsageError("not inside an ow workspace")
        
        return open_session(
            str(self.workspace.root),
            need_workspace=True,
            plugins=self.plugins,
            cwd=self.workspace.root,
        )


def open_session(
    workspace: str | None = None,
    *,
    need_workspace: bool,
    plugins: Plugins | None = None,
    cwd: Path | None = None,
) -> Session:
    """Open a session for a command to run.

    An explicit `-w` that cannot be resolved always raises; an implicit lookup falls
    back to a workspace-less session when `need_workspace` is False.
    """
    if plugins is None:
        plugins = load_plugins()

    if cwd is None:
        cwd = Path.cwd()

    declared_list = plugins.settings()
    declared = {s.key: s for s in declared_list}

    # Only the implicit lookup may fall through to a workspace-less session.
    root: Path | None
    if workspace is None:
        try:
            root = resolve_root(None, cwd=cwd)
        except UsageError:
            if need_workspace:
                raise
            root = None
    else:
        root = resolve_root(workspace, cwd=cwd)

    if root is None:
        global_layer = read_global(global_config(), declared)
        ws_layer_empty = Layer(values={}, unknown={}, wrong_type=())

        values, origins = resolve_settings(
            declared, global_layer, ws_layer_empty, ws_for_defaults=None
        )

        warnings = _collect_warnings(global_layer, ws_layer_empty, declared, {})
        diag_warnings, diag_list = _call_diagnostics(plugins, None)
        warnings = warnings + diag_warnings

        return Session(
            plugins=plugins,
            declared=declared,
            workspace=None,
            locations=(),
            origins=origins,
            warnings=warnings,
            diagnostics=diag_list,
            config_path=None,
            rejected_bases={},
            accepted_bases={},
            global_layer=global_layer,
            workspace_layer=ws_layer_empty,
        )

    config_path = root / ".ow" / "config.toml"
    global_layer = read_global(global_config(), declared)

    # remotes_of validates [repos] values: read the bare's remotes, cached per bare,
    # falling back to ("origin",) when the bare is missing.
    remotes_cache: dict[Path, tuple[str, ...]] = {}

    def remotes_of(repo_name: str) -> tuple[str, ...]:
        bare = (repos_dir() / f"{repo_name}.git").resolve()
        if bare in remotes_cache:
            return remotes_cache[bare]
        remotes = git.remotes(bare)
        if not remotes:
            remotes = ("origin",)
        remotes_cache[bare] = remotes
        return remotes

    workspace_layer, accepted_bases, rejected_bases = read_workspace(
        config_path, declared, remotes_of=remotes_of
    )

    locations = derive_repos(root, accepted_bases)

    # Callable defaults see the real repos but no settings, so resolution never
    # depends on the values it is computing.
    defaults_workspace = build_workspace(root, locations, {})
    values, origins = resolve_settings(
        declared, global_layer, workspace_layer, ws_for_defaults=defaults_workspace
    )

    workspace = build_workspace(root, locations, values)

    warnings = _collect_warnings(global_layer, workspace_layer, declared, rejected_bases)

    strays = stray_checkouts(root, locations)
    if strays:
        names = ", ".join(str(p.relative_to(root)) for p in strays)
        warnings += (
            f"stray checkouts not managed by ow: {names}; "
            f"delete them or move them out of the workspace",
        )

    diag_warnings, diag_list = _call_diagnostics(plugins, workspace)
    warnings += diag_warnings

    return Session(
        plugins=plugins,
        declared=declared,
        workspace=workspace,
        locations=locations,
        origins=origins,
        warnings=warnings,
        diagnostics=diag_list,
        config_path=config_path,
        rejected_bases=rejected_bases,
        accepted_bases=accepted_bases,
        global_layer=global_layer,
        workspace_layer=workspace_layer,
    )


def _collect_warnings(
    global_layer: Layer,
    workspace_layer: Layer,
    declared: Mapping[str, object],
    rejected_bases: Mapping[str, str],
) -> tuple[str, ...]:
    """One warning per config file, naming every unknown key and rejected base."""
    warnings = []

    global_parts = [
        f"unknown setting {key} in {global_config()}; "
        f"remove it with `ow config unset --global {key}`"
        for key in global_layer.unknown
    ]
    global_parts += [
        _wrong_type_note(key, global_config(), declared)
        for key in global_layer.wrong_type
    ]
    if global_parts:
        warnings.append("; ".join(global_parts))

    ws_parts = [
        f"unknown setting {key} in .ow/config.toml; "
        f"remove it with `ow config unset {key}`"
        for key in workspace_layer.unknown
    ]
    ws_parts += [
        _wrong_type_note(key, ".ow/config.toml", declared)
        for key in workspace_layer.wrong_type
    ]
    ws_parts += [
        f"ignoring default base {raw} for {name} in .ow/config.toml: invalid base"
        for name, raw in rejected_bases.items()
    ]
    if ws_parts:
        warnings.append("; ".join(ws_parts) + "; run `ow init`")

    return tuple(warnings)


def _wrong_type_note(key: str, where: Path | str, declared: Mapping[str, object]) -> str:
    """A key whose TOML value has the wrong type keeps its default."""
    setting = declared.get(key)
    kind = getattr(setting.type, "__name__", str(setting.type)) if setting else "?"
    return f"{key} in {where} must be {kind}; using its default"


def _call_diagnostics(
    plugins: Plugins,
    workspace: Workspace | None,
) -> tuple[tuple[str, ...], tuple[tuple[str, Diagnostic], ...]]:
    """Call ow_diagnostics on every plugin; a raise becomes a warning, never an abort."""
    if workspace is None:
        return (), ()

    warnings = []
    diagnostics = []

    results = plugins.call("ow_diagnostics", ws=workspace, errors="collect")

    for plugin_name, result in results:
        if isinstance(result, PluginError):
            warnings.append(f"{plugin_name}: ow_diagnostics failed: {result.cause}")
        elif isinstance(result, list):
            for diag in result:
                if isinstance(diag, Diagnostic):
                    diagnostics.append((plugin_name, diag))
                    if diag.level == "warning":
                        warnings.append(f"{plugin_name}: {diag.text}")

    return tuple(warnings), tuple(diagnostics)
