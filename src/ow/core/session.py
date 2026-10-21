"""Session: the immutable state every command starts from."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping

from ow.core import git
from ow.core.config import Layer, read_global, read_workspace, resolve_settings
from ow.core.index import read as read_index
from ow.core.paths import global_config, repos_dir
from ow.core.plugins import Diagnostic, Plugins, load_plugins
from ow.core.workspace import (
    Workspace,
    RepoLocation,
    find_root,
    derive_repos,
    build_workspace,
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
    global_layer: Layer
    workspace_layer: Layer

    def reload(self) -> Session:
        """Re-open the same workspace with fresh data.
        
        Called before the post-batch refresh to pick up branch changes.
        """
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
    
    Args:
        workspace: workspace name/path (None = find via find_root)
        need_workspace: if True and no workspace found, raise UsageError
        plugins: Plugins instance (None = load_plugins())
        cwd: working directory (None = Path.cwd())
    
    Returns:
        Session instance
    
    Raises:
        UsageError: on workspace resolution errors
        PluginError: from plugin hooks
    """
    if plugins is None:
        plugins = load_plugins()

    if cwd is None:
        cwd = Path.cwd()

    # Build declared settings map
    declared_list = plugins.settings()
    declared = {s.key: s for s in declared_list}

    # Resolve workspace root
    root: Path | None = None
    config_path: Path | None = None

    if workspace is None:
        # Use find_root from cwd
        root = find_root(cwd)
    else:
        # Resolve workspace argument
        workspace_str = workspace
        
        # A value containing "/", or "."/"..", or an existing directory is a path;
        # anything else is a workspace name matched against index basenames (§10).
        candidate = Path(workspace_str)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        is_path = (
            "/" in workspace_str
            or workspace_str in (".", "..")
            or candidate.is_dir()
        )
        if is_path:
            candidate = candidate.resolve()
            config_file = candidate / ".ow" / "config.toml"
            if not config_file.exists():
                raise UsageError(f"not an ow workspace; no .ow/config.toml in {candidate}")
            root = candidate
        else:
            # Treat as a name: search the index
            index_entries = read_index()
            matches = []
            for entry in index_entries:
                if entry.name == workspace_str:
                    matches.append(entry)
            
            if len(matches) == 0:
                raise UsageError(f"workspace '{workspace_str}' not found")
            elif len(matches) > 1:
                paths = ", ".join(str(m) for m in matches)
                raise UsageError(f"workspace '{workspace_str}' is ambiguous; matches: {paths}")
            else:
                root = matches[0]

    # If no workspace found and need_workspace is False, allow None
    if root is None:
        if need_workspace:
            raise UsageError(
                "not inside an ow workspace; pass -w <name|path> or run `ow init`"
            )
        # No workspace: build a minimal session from global config only
        global_layer = read_global(global_config(), declared)
        ws_layer_empty = Layer(values={}, unknown={}, wrong_type=())
        
        values, origins = resolve_settings(
            declared, global_layer, ws_layer_empty, ws_for_defaults=None
        )
        
        warnings = _collect_warnings(global_layer, ws_layer_empty, declared)
        
        # No workspace means no ow_diagnostics(ws); _call_diagnostics yields nothing.
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
            global_layer=global_layer,
            workspace_layer=ws_layer_empty,
        )

    # Workspace found
    config_path = root / ".ow" / "config.toml"
    
    # Read global config
    global_layer = read_global(global_config(), declared)
    
    # Read workspace config. remotes_of validates [repos] values (§10): read the bare's
    # remotes, cached per bare, falling back to ("origin",) when the bare is missing.
    remotes_cache: dict[Path, tuple[str, ...]] = {}

    def remotes_of(repo_name: str) -> tuple[str, ...]:
        bare = (repos_dir() / f"{repo_name}.git").resolve()
        if bare in remotes_cache:
            return remotes_cache[bare]
        if not bare.is_dir():
            remotes_cache[bare] = ("origin",)
            return remotes_cache[bare]
        try:
            output = git.read(["git", "-C", str(bare), "remote"])
        except git.GitFailed:
            remotes = ("origin",)
        else:
            remotes = tuple(output.split("\n")) if output else ("origin",)
        remotes_cache[bare] = remotes
        return remotes
    
    workspace_layer, accepted_repos, rejected_bases = read_workspace(
        config_path, declared, remotes_of=remotes_of
    )
    
    # Resolve settings with empty workspace for defaults (P5)
    empty_workspace = build_workspace(root, (), {})
    
    values_temp, _ = resolve_settings(
        declared, global_layer, workspace_layer, ws_for_defaults=empty_workspace
    )
    
    # Now derive repos with the accepted bases
    locations = derive_repos(root, accepted_repos)
    
    # Build real workspace
    workspace = build_workspace(root, locations, values_temp)
    
    # Resolve settings again with real workspace
    values, origins = resolve_settings(
        declared, global_layer, workspace_layer, ws_for_defaults=workspace
    )
    
    workspace = build_workspace(root, locations, values)
    
    # Collect warnings
    warnings = _collect_warnings(global_layer, workspace_layer, declared)
    
    # Add warnings for rejected bases
    for repo_name, raw_value in rejected_bases.items():
        warnings = warnings + (
            f"ignoring default base {raw_value} for {repo_name} in .ow/config.toml: "
            f"invalid base; run `ow init`",
        )
    
    # Call ow_diagnostics on all plugins
    diag_warnings, diag_list = _call_diagnostics(plugins, workspace)
    warnings = warnings + diag_warnings
    
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
        global_layer=global_layer,
        workspace_layer=workspace_layer,
    )


def _collect_warnings(
    global_layer: Layer,
    workspace_layer: Layer,
    declared: Mapping[str, object],
) -> tuple[str, ...]:
    """Collect warnings from config layers.
    
    Returns warnings for:
    - unknown keys in global and workspace layers
    - wrong-type keys in both layers
    """
    warnings = []
    
    # Global layer unknown keys
    for key in global_layer.unknown:
        warnings.append(
            f"unknown setting {key} in {global_config()}; "
            f"remove it with `ow config unset --global {key}`"
        )
    
    # Workspace layer unknown keys
    if workspace_layer.unknown:
        for key in workspace_layer.unknown:
            warnings.append(
                f"unknown setting {key} in .ow/config.toml; "
                f"remove it with `ow config unset {key}`, or run `ow init`"
            )
    
    # Wrong-type keys (global)
    for key in global_layer.wrong_type:
        setting = declared.get(key)
        if setting:
            type_name = getattr(setting.type, '__name__', str(setting.type))
            warnings.append(
                f"{key} in {global_config()} must be {type_name}; using its default"
            )
    
    # Wrong-type keys (workspace)
    for key in workspace_layer.wrong_type:
        setting = declared.get(key)
        if setting:
            type_name = getattr(setting.type, '__name__', str(setting.type))
            warnings.append(
                f"{key} in .ow/config.toml must be {type_name}; using its default"
            )
    
    return tuple(warnings)


def _call_diagnostics(
    plugins: Plugins,
    workspace: Workspace | None,
) -> tuple[tuple[str, ...], tuple[tuple[str, Diagnostic], ...]]:
    """Call ow_diagnostics on all plugins.

    Returns (warnings_tuple, diagnostics_tuple) where each warning or diagnostic
    is prefixed with the plugin name.

    Per contract §11/P13: a raise becomes a warning naming the plugin,
    never aborts the session (aborts only inside a refresh).
    ow_diagnostics(ws) needs a workspace (§10), so a workspace-less session reports none.
    """
    if workspace is None:
        return (), ()

    warnings = []
    diagnostics = []

    # errors="collect" so one failing plugin still becomes its own warning and the
    # other plugins' diagnostics are reported too.
    results = plugins.call("ow_diagnostics", ws=workspace, errors="collect")
    
    for plugin_name, result in results:
        # Check if this result is a PluginError (from error collection)
        if isinstance(result, PluginError):
            # P13: a plugin error in ow_diagnostics becomes a warning
            warnings.append(f"{plugin_name}: ow_diagnostics failed: {result.cause}")
        elif isinstance(result, list):
            # Process the diagnostics
            for diag in result:
                if isinstance(diag, Diagnostic):
                    diagnostics.append((plugin_name, diag))
                    if diag.level == "warning":
                        warnings.append(f"{plugin_name}: {diag.text}")
    
    return tuple(warnings), tuple(diagnostics)
