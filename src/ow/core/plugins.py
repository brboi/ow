"""Plugin loader and hook specifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Sequence
import sys
import types

import pluggy

from ow.errors import PluginError

if TYPE_CHECKING:
    from ow.core.workspace import Workspace

# Hook markers for the "ow" project
hookspec = pluggy.HookspecMarker("ow")
hookimpl = pluggy.HookimplMarker("ow")


@dataclass(frozen=True)
class Setting:
    """A named, typed setting a plugin declares."""

    key: str  # "<plugin>.<name>"
    type: type  # str | int | bool | list
    default: object | Callable[[Workspace], object] | None
    help: str


@dataclass(frozen=True)
class GeneratedFile:
    """A file a plugin contributes to the workspace."""

    path: PurePosixPath  # workspace-relative
    data: bytes
    mode: int = 0o644

    def __post_init__(self) -> None:
        # Convert str to PurePosixPath if needed
        if isinstance(self.path, str):
            object.__setattr__(self, "path", PurePosixPath(self.path))


@dataclass(frozen=True)
class Diagnostic:
    """A diagnostic message from a plugin."""

    level: Literal["info", "warning", "error"]
    text: str


@dataclass(frozen=True)
class KnownRepo:
    """A repo a plugin knows about by name."""

    name: str
    url: str


class CoreSpec:
    """Core hookspecs declared by ow."""

    @hookspec
    def ow_settings(self) -> list[Setting]:
        """Return this plugin's settings."""

    @hookspec
    def ow_generated_files(self, ws: Workspace, answers: Callable[[str], list]) -> list[GeneratedFile]:
        """Return generated files (P1: answers helper lets owners collect dependencies)."""

    @hookspec
    def ow_diagnostics(self, ws: Workspace) -> list[Diagnostic]:
        """Return diagnostic messages."""

    @hookspec
    def ow_known_repos(self) -> list[KnownRepo]:
        """Return known repos this plugin provides."""

    @hookspec
    def ow_commands(self, app: object) -> None:
        """Add commands to the plugin's Typer subapp."""

    @hookspec
    def ow_workspace_created(self, ws: Workspace) -> None:
        """Event: workspace was created."""

    @hookspec
    def ow_refreshed(self, ws: Workspace, owned: tuple[PurePosixPath, ...]) -> None:
        """Event: refresh completed. owned is every path ow wrote or up-to-date (P2)."""


def _make_builtins() -> tuple[tuple[str, types.ModuleType], ...]:
    """Lazy loader for built-in plugins."""

    def __getattr__(name: str) -> tuple[tuple[str, types.ModuleType], ...]:
        if name == "BUILTINS":
            # Import all five plugin modules in order
            from ow.plugins import mise, odoo, vscode, zed, local

            return (
                ("mise", mise),
                ("odoo", odoo),
                ("vscode", vscode),
                ("zed", zed),
                ("local", local),
            )
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    return __getattr__("BUILTINS")


# Lazy access via module __getattr__ (set below)
BUILTINS: tuple[tuple[str, types.ModuleType], ...] | None = None


class Plugins:
    """Manages plugin registry and hooks."""

    def __init__(self, pm: pluggy.PluginManager, names: tuple[str, ...]) -> None:
        self.pm = pm
        self.names = names

    def settings(self) -> tuple[Setting, ...]:
        """Return all settings from all plugins, in order.

        Includes a synthesized "<name>.enabled" (bool, True) for each plugin.
        """
        result = []
        
        # Get all impls once to iterate them
        try:
            impls = self.pm.hook.ow_settings.get_hookimpls()
        except AttributeError:
            impls = []
        
        # Build a map of plugin name from impls
        impl_to_name = {impl.plugin: self.pm.get_name(impl.plugin) for impl in impls}
        
        for name in self.names:
            # Add the synthesized enabled setting
            result.append(
                Setting(
                    key=f"{name}.enabled",
                    type=bool,
                    default=True,
                    help=f"Enable or disable the {name} plugin",
                )
            )
            
            # Add plugin's own settings
            for impl in impls:
                plugin_name = impl_to_name[impl.plugin]
                if plugin_name == name:
                    try:
                        plugin_settings = impl.function()
                        if plugin_settings:
                            for setting in plugin_settings:
                                # Validate that key starts with "<plugin>."
                                if not setting.key.startswith(f"{name}."):
                                    raise PluginError(
                                        name,
                                        "ow_settings",
                                        f"setting key {setting.key!r} must start with {name!r}.",
                                    )
                            result.extend(plugin_settings)
                    except PluginError:
                        raise
                    except Exception as e:
                        raise PluginError(name, "ow_settings", e) from e

        return tuple(result)

    def call(
        self, hook: str, ws: Workspace | None = None, *,
        errors: Literal["raise", "collect"] = "raise", **kwargs: Any
    ) -> list[tuple[str, object]]:
        """Call a hook on all enabled plugins.

        Returns (plugin_name, result) tuples in tuple order.
        Skips disabled plugins when ws is given.
        
        With errors="raise" (default), raises on first PluginError.
        With errors="collect", yields (name, PluginError(...)) and continues.
        Drops None results.
        """
        result = []
        hook_method = getattr(self.pm.hook, hook, None)
        if hook_method is None:
            return []
        impls = hook_method.get_hookimpls()

        # Build a map of impl.plugin to plugin name
        impl_to_name = {impl.plugin: self.pm.get_name(impl.plugin) for impl in impls}

        for impl in impls:
            name = impl_to_name[impl.plugin]

            # Skip disabled plugins when ws is given
            if ws is not None:
                enabled = ws.settings.get(f"{name}.enabled", True)
                if not enabled:
                    continue

            # Build kwargs for this implementation (only pass args it declares)
            impl_kwargs = {}
            for arg in impl.argnames:
                if arg in kwargs:
                    impl_kwargs[arg] = kwargs[arg]
                elif arg == "ws" and ws is not None:
                    impl_kwargs[arg] = ws

            try:
                ret = impl.function(**impl_kwargs)
                if ret is not None:
                    result.append((name, ret))
            except PluginError as e:
                # Keep an existing PluginError unchanged (original plugin name and cause)
                if errors == "raise":
                    raise
                result.append((name, e))
            except Exception as e:
                error = PluginError(name, hook, e)
                if errors == "raise":
                    raise error from e
                result.append((name, error))

        return result

    def answers(self, ws: Workspace) -> Callable[[str], list]:
        """Return a function that collects answers to a hook, skipping disabled plugins.

        The returned function takes a hook name and returns a list of results in tuple order.
        It skips disabled plugins and turns raises into PluginError naming the answering plugin.
        """
        def answer_hook(hook_name: str) -> list:
            results = []
            for _name, result in self.call(hook_name, ws=ws):
                results.append(result)
            return results

        return answer_hook

    def add_commands(self, app: object) -> None:
        """Register command groups for plugins that define ow_commands.

        Never filtered by enabled. Each plugin gets its own typer.Typer subapp,
        added to root app only if it registered at least one command.
        """
        import typer

        # Get all impls once
        try:
            impls = self.pm.hook.ow_commands.get_hookimpls()
        except AttributeError:
            impls = []
        
        impl_to_name = {impl.plugin: self.pm.get_name(impl.plugin) for impl in impls}

        for impl in impls:
            name = impl_to_name[impl.plugin]

            # Create a sub-app for this plugin
            sub_app = typer.Typer(name=name, no_args_is_help=True)

            # Call the plugin's ow_commands to register commands
            try:
                impl.function(app=sub_app)
            except Exception as e:
                raise PluginError(name, "ow_commands", e) from e

            # Only add the sub-app if it has commands/groups
            if sub_app.registered_commands or sub_app.registered_groups:
                app.add_typer(sub_app, name=name)


def load_plugins(builtins: tuple[tuple[str, types.ModuleType], ...] | None = None) -> Plugins:
    """Load and register all built-in plugins.

    If builtins is None, uses the lazily-loaded BUILTINS tuple.
    Raises PluginError if HOOKSPECS is missing or if check_pending fails.
    """
    if builtins is None:
        builtins = _make_builtins()

    # Create plugin manager
    pm = pluggy.PluginManager("ow")

    # Add core spec
    pm.add_hookspecs(CoreSpec)

    # Track which specs we've already added (avoid adding the same spec twice)
    added_specs = {CoreSpec}

    # Register plugins and add their specs
    names = []
    for name, module in builtins:
        names.append(name)

        # Add specs declared by this plugin's HOOKSPECS
        if hasattr(module, "HOOKSPECS"):
            hookspecs = module.HOOKSPECS
            if not isinstance(hookspecs, tuple):
                raise PluginError(name, "check_pending", "HOOKSPECS must be a tuple")

            for spec_class in hookspecs:
                if spec_class not in added_specs:
                    try:
                        pm.add_hookspecs(spec_class)
                        added_specs.add(spec_class)
                    except ValueError as e:
                        raise PluginError(name, "check_pending", e) from e

        # Register the plugin module
        pm.register(module, name=name)

    # Check for pending hookimpls (misspelled implementations)
    try:
        pm.check_pending()
    except pluggy.PluginValidationError as e:
        # Extract plugin name from the exception
        plugin_name = pm.get_name(e.plugin)
        raise PluginError(plugin_name, "check_pending", str(e)) from e

    return Plugins(pm, tuple(names))


# Set up module-level __getattr__ for lazy BUILTINS access
def __getattr__(name: str) -> Any:
    if name == "BUILTINS":
        return _make_builtins()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
