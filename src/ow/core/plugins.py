"""Plugin loader and hook specifications."""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Sequence

import pluggy

from ow.core import config
from ow.errors import PluginError

if TYPE_CHECKING:
    from ow.core.facts import RepoFacts
    from ow.core.tasks import Task
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
    """A file a plugin contributes to the workspace or task."""

    path: PurePosixPath  # workspace-relative or task-parent-relative
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
    """A repo a plugin knows about by name (and optional short aliases)."""

    name: str
    url: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedRepo:
    """One repo resolved from a target (PR, Runbot bundle, etc.)."""

    name: str
    url: str | None = None
    branch: str | None = None
    base: str | None = None
    remote: str | None = None
    fetch_ref: str | None = None  # e.g. "refs/pull/12345/head"
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedTarget:
    """A workspace creation plan resolved from an external target (PR, bundle, URL)."""

    name: str
    repos: tuple[ResolvedRepo, ...]
    source: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RunCommand:
    """Command a plugin provides to execute a workspace via `ow run`."""

    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StatusInfo:
    """External status contribution for `ow status` (e.g. GitHub PR, Runbot build)."""

    plugin: str
    scope: Literal["workspace", "repo", "external"]
    target: str
    summary: str
    data: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PruneCandidate:
    """A workspace a plugin recommends pruning (e.g. its PR was merged/closed)."""

    workspace: Path
    reason: str
    data: Mapping[str, object] = field(default_factory=dict)


class CoreSpec:
    """Core hookspecs declared by ow."""

    @hookspec
    def ow_settings(self) -> list[Setting]:
        """Return this plugin's settings."""

    @hookspec
    def ow_generated_files(
        self, ws: Workspace, answers: Callable[[str], list]
    ) -> list[GeneratedFile | Diagnostic]:
        """Return generated files and optional diagnostic messages."""

    @hookspec
    def ow_diagnostics(self, ws: Workspace) -> list[Diagnostic]:
        """Return diagnostic messages."""

    @hookspec
    def ow_known_repos(self) -> list[KnownRepo]:
        """Return known repos this plugin provides."""

    @hookspec
    def ow_commands(self, app: object, plugins: Plugins) -> None:
        """Add commands to the plugin's Typer subapp (or root app when TOP_LEVEL_COMMANDS=True)."""

    @hookspec
    def ow_workspace_created(self, ws: Workspace) -> None:
        """Event: workspace was created."""

    @hookspec
    def ow_refreshed(self, ws: Workspace, owned: tuple[PurePosixPath, ...]) -> None:
        """Event: refresh completed. owned is every path ow wrote or up-to-date."""

    @hookspec
    def ow_resolve_target(
        self,
        target: str,
        known_repos: Mapping[str, KnownRepo],
        settings: Mapping[str, object],
    ) -> ResolvedTarget | None:
        """Resolve an external target string (e.g. PR or Runbot bundle) to a workspace plan."""

    @hookspec
    def ow_expand_workspace_repos(
        self,
        repos: tuple[ResolvedRepo, ...],
        known_repos: Mapping[str, KnownRepo],
        settings: Mapping[str, object],
    ) -> tuple[ResolvedRepo, ...] | None:
        """Optionally expand resolved repos with implicit companion repos (e.g. Odoo core)."""

    @hookspec
    def ow_runner(
        self, ws: Workspace, args: tuple[str, ...]
    ) -> RunCommand | None:
        """Return the command to run this workspace via `ow run`, or None."""

    @hookspec
    def ow_status(
        self,
        ws: Workspace | None,
        facts: Sequence[RepoFacts],
        target: str | None,
        offline: bool,
        known_repos: Mapping[str, KnownRepo],
        settings: Mapping[str, object],
    ) -> list[StatusInfo]:
        """Return external status contributions for a workspace or external target."""

    @hookspec
    def ow_prune_candidates(
        self,
        workspaces: Sequence[Workspace],
        offline: bool,
    ) -> list[PruneCandidate]:
        """Return workspaces recommended for pruning (e.g. merged/closed PRs)."""

    @hookspec
    def ow_task_files(self, task: Task) -> list[GeneratedFile]:
        """Return generated files for a multi-workspace task (written in common parent)."""


def _get_builtins() -> tuple[tuple[str, types.ModuleType], ...]:
    """Get all built-in plugin modules in order."""
    from ow.plugins import local, mise, odoo, vscode, zed

    return (
        ("mise", mise),
        ("odoo", odoo),
        ("vscode", vscode),
        ("zed", zed),
        ("local", local),
    )


def _plugin_default_enabled(plugin: object) -> bool:
    """Whether `plugin` is enabled by default (True unless DEFAULT_ENABLED = False)."""
    return bool(getattr(plugin, "DEFAULT_ENABLED", True))


class Plugins:
    """Manages plugin registry and hooks."""

    def __init__(self, pm: pluggy.PluginManager, names: tuple[str, ...]) -> None:
        self.pm = pm
        self.names = names

    def default_enabled(self, name: str) -> bool:
        """Default enabled state for plugin `name`."""
        plugin = self.pm.get_plugin(name)
        return _plugin_default_enabled(plugin) if plugin is not None else True

    def _global_enabled_flags(self) -> Mapping[str, object]:
        """Read `<name>.enabled` overrides from global config."""
        from ow.core.paths import global_config

        declared = {
            f"{name}.enabled": Setting(f"{name}.enabled", bool, self.default_enabled(name), "")
            for name in self.names
        }
        try:
            return config.read_global(global_config(), declared).values
        except (OSError, config.UsageError):
            return {}

    def is_enabled(self, name: str, ws: Workspace | None = None) -> bool:
        """Whether plugin `name` is enabled for `ws` (or globally when `ws` is None)."""
        key = f"{name}.enabled"
        default = self.default_enabled(name)
        if ws is not None:
            return bool(ws.settings.get(key, default))
        return bool(self._global_enabled_flags().get(key, default))

    def settings(self) -> tuple[Setting, ...]:
        """Return all settings from all plugins, in order.

        Includes a synthesized `<name>.enabled` (bool, default `DEFAULT_ENABLED`) for each plugin.
        """
        result = []
        impls = self.pm.hook.ow_settings.get_hookimpls()
        impl_to_name = {impl.plugin: self.pm.get_name(impl.plugin) for impl in impls}

        for name in self.names:
            result.append(
                Setting(
                    key=f"{name}.enabled",
                    type=bool,
                    default=self.default_enabled(name),
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
                                if setting.key == f"{name}.enabled":
                                    raise PluginError(
                                        name,
                                        "ow_settings",
                                        f"setting key {setting.key!r} is reserved by core",
                                    )
                            result.extend(plugin_settings)
                    except PluginError:
                        raise
                    except Exception as e:
                        raise PluginError(name, "ow_settings", e) from e

        return tuple(result)

    def call(
        self,
        hook: str,
        ws: Workspace | None = None,
        *,
        errors: Literal["raise", "collect"] = "raise",
        **kwargs: Any,
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

        enabled_flags: Mapping[str, object] = {} if ws is not None else self._global_enabled_flags()

        for impl in impls:
            name = impl_to_name[impl.plugin]
            enabled_key = f"{name}.enabled"
            default = self.default_enabled(name)
            if ws is not None:
                enabled = ws.settings.get(enabled_key, default)
            else:
                enabled = enabled_flags.get(enabled_key, default)

            if not enabled:
                continue

            # Build kwargs for this implementation (only pass args it declares)
            impl_kwargs = {}
            for arg in (*impl.argnames, *impl.kwargnames):
                if arg in kwargs:
                    impl_kwargs[arg] = kwargs[arg]
                elif arg == "ws":
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
        """Register CLI commands for enabled plugins that define `ow_commands`.

        Respects global `<name>.enabled` (falling back to `DEFAULT_ENABLED`).
        Plugins with `TOP_LEVEL_COMMANDS = True` register directly on the root
        Typer app; others get their own named `typer.Typer` subapp.
        """
        import typer

        impls = self.pm.hook.ow_commands.get_hookimpls()
        impl_to_name = {impl.plugin: self.pm.get_name(impl.plugin) for impl in impls}
        enabled_flags = self._global_enabled_flags()

        for impl in impls:
            name = impl_to_name[impl.plugin]
            enabled = enabled_flags.get(f"{name}.enabled", self.default_enabled(name))
            if not enabled:
                continue

            if getattr(impl.plugin, "TOP_LEVEL_COMMANDS", False):
                try:
                    cmd_kwargs: dict[str, Any] = {"app": app}
                    if "plugins" in (*impl.argnames, *impl.kwargnames):
                        cmd_kwargs["plugins"] = self
                    impl.function(**cmd_kwargs)
                except Exception as e:
                    raise PluginError(name, "ow_commands", e) from e
                continue

            sub_app = typer.Typer(name=name, no_args_is_help=True, rich_markup_mode=None)

            try:
                cmd_kwargs = {"app": sub_app}
                if "plugins" in (*impl.argnames, *impl.kwargnames):
                    cmd_kwargs["plugins"] = self
                impl.function(**cmd_kwargs)
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

    _RESERVED_PLUGIN_NAMES = frozenset({"repos", "workspace"})
    names = []
    for name, module in builtins:
        if name in _RESERVED_PLUGIN_NAMES:
            raise PluginError(name, "register", f"plugin name {name!r} is reserved")
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
                        raise PluginError(
                            pm.get_name(e.plugin) or name, "HOOKSPECS", str(e)
                        ) from e

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
