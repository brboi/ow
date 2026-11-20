"""Plugin loader and hook specifications."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping
import types

import pluggy

from ow.core import config
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
    def ow_generated_files(self, ws: Workspace, answers: Callable[[str], list]) -> list[GeneratedFile | Diagnostic]:
        """Return generated files and optional diagnostic messages."""

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
        """Event: refresh completed. owned is every path ow wrote or up-to-date."""


def _get_builtins() -> tuple[tuple[str, types.ModuleType], ...]:
    """Get all built-in plugin modules in order."""
    from ow.plugins import mise, odoo, vscode, zed, local

    return (
        ("mise", mise),
        ("odoo", odoo),
        ("vscode", vscode),
        ("zed", zed),
        ("local", local),
    )


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
        impls = self.pm.hook.ow_settings.get_hookimpls()
        impl_to_name = {impl.plugin: self.pm.get_name(impl.plugin) for impl in impls}

        for name in self.names:
            result.append(
                Setting(
                    key=f"{name}.enabled",
                    type=bool,
                    default=True,
                    help=f"Enable or disable the {name} plugin",
                )
            )

            for impl in impls:
                plugin_name = impl_to_name[impl.plugin]
                if plugin_name == name:
                    try:
                        plugin_settings = impl.function()
                        if plugin_settings:
                            for setting in plugin_settings:
                                if not setting.key.startswith(f"{name}."):
                                    raise PluginError(
                                        name,
                                        "ow_settings",
                                        f"setting key {setting.key!r} must start with {name!r}.",
                                    )
                            result.extend(plugin_settings)
                    except Exception as e:
                        raise PluginError(name, "ow_settings", e) from e

        return tuple(result)

    def call(
        self, hook: str, ws: Workspace | None = None, *,
        errors: Literal["raise", "collect"] = "raise", **kwargs: Any
    ) -> list[tuple[str, object]]:
        """Call a hook on all enabled plugins.

        Returns (plugin_name, result) tuples in tuple order.
        Skips disabled plugins (checks workspace settings or global layer).

        With errors="raise" (default), raises on first PluginError.
        With errors="collect", collects (name, PluginError(...)) and continues.
        Drops None results.
        """
        result = []
        hook_method = getattr(self.pm.hook, hook, None)
        if hook_method is None:
            return []
        impls = hook_method.get_hookimpls()
        impl_to_name = {impl.plugin: self.pm.get_name(impl.plugin) for impl in impls}

        # Enabled flags default to True; without a workspace they come from the global layer.
        enabled_flags: Mapping[str, object] = {}
        if ws is None:
            from ow.core.paths import global_config

            declared = {
                f"{name}.enabled": Setting(f"{name}.enabled", bool, True, "")
                for name in self.names
            }
            try:
                enabled_flags = config.read_global(global_config(), declared).values
            except (OSError, config.UsageError):
                enabled_flags = {}

        for impl in impls:
            name = impl_to_name[impl.plugin]
            enabled_key = f"{name}.enabled"
            if ws is not None:
                enabled = ws.settings.get(enabled_key, True)
            else:
                enabled = enabled_flags.get(enabled_key, True)

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

        impls = self.pm.hook.ow_commands.get_hookimpls()
        impl_to_name = {impl.plugin: self.pm.get_name(impl.plugin) for impl in impls}

        for impl in impls:
            name = impl_to_name[impl.plugin]
            sub_app = typer.Typer(name=name, no_args_is_help=True)

            try:
                impl.function(app=sub_app)
            except Exception as e:
                raise PluginError(name, "ow_commands", e) from e

            if sub_app.registered_commands or sub_app.registered_groups:
                app.add_typer(sub_app, name=name)


def load_plugins(builtins: tuple[tuple[str, types.ModuleType], ...] | None = None) -> Plugins:
    """Load and register all built-in plugins.

    Raises PluginError when a plugin's HOOKSPECS or hook implementations are invalid.
    """
    if builtins is None:
        builtins = _get_builtins()

    pm = pluggy.PluginManager("ow")
    pm.add_hookspecs(CoreSpec)

    # Avoid adding the same spec class twice
    added_specs = {CoreSpec}

    names = []
    for name, module in builtins:
        names.append(name)

        if hasattr(module, "HOOKSPECS"):
            hookspecs = module.HOOKSPECS
            if not isinstance(hookspecs, tuple):
                raise PluginError(name, "HOOKSPECS", "HOOKSPECS must be a tuple")

            for spec_class in hookspecs:
                if spec_class not in added_specs:
                    try:
                        pm.add_hookspecs(spec_class)
                        added_specs.add(spec_class)
                    except ValueError as e:
                        raise PluginError(name, "HOOKSPECS", str(e)) from e
                    except pluggy.PluginValidationError as e:
                        raise PluginError(pm.get_name(e.plugin) or name, "HOOKSPECS", str(e)) from e

        try:
            pm.register(module, name=name)
        except pluggy.PluginValidationError as e:
            raise PluginError(name, "register", str(e)) from e

    # Hookimpls whose name matches no spec surface here
    try:
        pm.check_pending()
    except pluggy.PluginValidationError as e:
        raise PluginError(pm.get_name(e.plugin), "check_pending", str(e)) from e

    return Plugins(pm, tuple(names))


