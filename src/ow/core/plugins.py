"""Plugin loader and hook specifications."""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Sequence

import pluggy

from ow.core import config
from ow.errors import PluginError, UsageError

if TYPE_CHECKING:
    from ow.core.facts import RepoFacts
    from ow.core.stacks import MultiWorkspace
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
    """A file a plugin contributes to the workspace or stack."""

    path: PurePosixPath  # workspace-relative or multi-stack-root-relative
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


@dataclass(frozen=True)
class ResolvedRepo:
    """One repo resolved from a target (PR, Runbot bundle, etc.)."""

    name: str
    url: str | None = None
    branch: str | None = None
    base: str | None = None
    remote: str | None = None
    fetch_ref: str | None = None  # e.g. "refs/pull/12345/head"
    commit: str | None = None  # optional exact SHA for detached checkout (e.g. Runbot base_match)
    explicit: bool = True  # False for auto-discovered companions/sisters that explicit targets override
    pr: str | None = None  # opaque PR identity, recorded in `branch.<b>.ow-pr` and compared as-is


@dataclass(frozen=True)
class ResolvedTarget:
    """A workspace creation plan resolved from an external target (PR, bundle, URL)."""

    name: str
    repos: tuple[ResolvedRepo, ...]
    source: str


@dataclass(frozen=True)
class StackInfo:
    """What a plugin tells users about a stack: coordinated ports and a one-line display note."""

    ports: Mapping[str, int] = field(default_factory=dict)
    note: str | None = None


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
    def ow_before_run(
        self, ws: Workspace, workspaces: Sequence[Workspace]
    ) -> list[str] | None:
        """Prepare shared local services before `ow run` starts workspaces; return info lines."""

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
    def ow_multi_workspace_files(self, multi: MultiWorkspace) -> list[GeneratedFile]:
        """Return generated files for a multi-stack workspace (written in `multi.root`)."""

    @hookspec
    def ow_stack_values(
        self,
        ws: Workspace | None,
        ws_values: Mapping[str, object],
        siblings: Sequence[Workspace],
        pending: Sequence[Mapping[str, object]],
        global_settings: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Return the settings to pin explicitly for a stack (e.g. database name, coordinated ports).

        `ws` is the existing workspace being promoted to a stack (None for a new stack),
        `ws_values` the stack's own explicit settings (which must not be overridden),
        `siblings` the stacks already on disk next to it, and `pending` the settings of stacks
        planned earlier in the same command, which may not exist on disk yet.
        """

    @hookspec
    def ow_stack_name(self, ws: Workspace) -> str | None:
        """Suggest a stack directory name for `ws` during promotion to multi-stack."""

    @hookspec
    def ow_stack_info(self, ws: Workspace) -> StackInfo | None:
        """Return the ports and display note users see for `ws` (`ow ls`, `ow status`, `ow run`)."""

    @hookspec
    def ow_is_base_branch(self, repo: str, branch: str) -> bool | None:
        """Return True if `branch` on `repo` is an upstream base series (checked out detached)."""


def _get_builtins() -> tuple[tuple[str, types.ModuleType], ...]:
    """Get all built-in plugin modules in order."""
    from ow.plugins import github, local, mailsink, mise, odoo, postgres, runbot, vscode, zed

    # Order matters: `Plugins.pipe` threads values in this order (odoo expands companion
    # repos before runbot pins them).
    return (
        ("mise", mise),
        ("odoo", odoo),
        ("postgres", postgres),
        ("mailsink", mailsink),
        ("github", github),
        ("runbot", runbot),
        ("vscode", vscode),
        ("zed", zed),
        ("local", local),
    )


class Plugins:
    """Manages plugin registry and hooks."""

    def __init__(self, pm: pluggy.PluginManager, names: tuple[str, ...]) -> None:
        self.pm = pm
        self.names = names

    def _global_enabled_flags(self) -> Mapping[str, object]:
        """Read `<name>.enabled` overrides from global config."""
        from ow.core.paths import global_config

        declared = {
            f"{name}.enabled": Setting(f"{name}.enabled", bool, True, "") for name in self.names
        }
        try:
            return config.read_global(global_config(), declared).values
        except (OSError, config.UsageError):
            return {}

    def is_enabled(self, name: str, ws: Workspace | None = None) -> bool:
        """Whether plugin `name` is enabled for `ws` (or globally when `ws` is None)."""
        key = f"{name}.enabled"
        if ws is not None:
            return bool(ws.settings.get(key, True))
        return bool(self._global_enabled_flags().get(key, True))

    def settings(self) -> tuple[Setting, ...]:
        """Return all settings from all plugins, in order.

        Includes a synthesized `<name>.enabled` (bool, default True) for each plugin.
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
                if impl_to_name[impl.plugin] != name:
                    continue
                try:
                    plugin_settings = impl.function()
                except PluginError:
                    raise
                except Exception as e:
                    raise PluginError(name, "ow_settings", e) from e
                if plugin_settings:
                    result.extend(plugin_settings)

        return tuple(result)

    def _ordered_impls(self, hook: str) -> list[tuple[str, Any]]:
        """Return `(plugin_name, hookimpl)` pairs ordered by `self.names`."""
        hook_method = getattr(self.pm.hook, hook, None)
        if hook_method is None:
            return []
        order = {name: idx for idx, name in enumerate(self.names)}
        pairs: list[tuple[str, Any]] = []
        for impl in hook_method.get_hookimpls():
            name = self.pm.get_name(impl.plugin)
            if name is not None:
                pairs.append((name, impl))
        pairs.sort(key=lambda item: order.get(item[0], len(order)))
        return pairs

    def _enabled_predicate(self, ws: Workspace | None) -> Callable[[str], bool]:
        """Return a predicate checking whether a plugin name is enabled for `ws` (or globally)."""
        if ws is not None:
            return lambda name: bool(ws.settings.get(f"{name}.enabled", True))
        enabled_flags = self._global_enabled_flags()
        return lambda name: bool(enabled_flags.get(f"{name}.enabled", True))

    @staticmethod
    def _bind_kwargs(
        impl: Any,
        ws: Workspace | None,
        kwargs: Mapping[str, Any],
        *,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        impl_kwargs: dict[str, Any] = {}
        for arg in (*impl.argnames, *impl.kwargnames):
            if extra and arg in extra:
                impl_kwargs[arg] = extra[arg]
            elif arg in kwargs:
                impl_kwargs[arg] = kwargs[arg]
            elif arg == "ws":
                impl_kwargs[arg] = ws
        return impl_kwargs

    @staticmethod
    def _invoke(name: str, impl: Any, hook: str, impl_kwargs: Mapping[str, Any]) -> object:
        """Run one implementation; a plain exception becomes a `PluginError` naming `name`."""
        try:
            return impl.function(**impl_kwargs)
        except (PluginError, UsageError):
            raise
        except Exception as e:
            raise PluginError(name, hook, e) from e

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
        With errors="raise" (default), raises on first PluginError (and propagates UsageError).
        With errors="collect", collects (name, PluginError(...)) and continues.
        Drops None results.
        """
        result: list[tuple[str, object]] = []
        is_enabled = self._enabled_predicate(ws)

        for name, impl in self._ordered_impls(hook):
            if not is_enabled(name):
                continue
            try:
                ret = self._invoke(name, impl, hook, self._bind_kwargs(impl, ws, kwargs))
            except (PluginError, UsageError) as e:
                if errors == "raise":
                    raise
                # Keep an existing PluginError unchanged (original plugin name and cause)
                result.append((name, e if isinstance(e, PluginError) else PluginError(name, hook, e)))
                continue
            if ret is not None:
                result.append((name, ret))

        return result

    def first(self, hook: str, ws: Workspace | None = None, **kwargs: Any) -> object | None:
        """Return the first non-None answer of `hook` among enabled plugins, in plugin order.

        Stops at that answer: later implementations do not run. Enablement and error
        semantics are those of `call` with errors="raise".
        """
        is_enabled = self._enabled_predicate(ws)

        for name, impl in self._ordered_impls(hook):
            if not is_enabled(name):
                continue
            ret = self._invoke(name, impl, hook, self._bind_kwargs(impl, ws, kwargs))
            if ret is not None:
                return ret
        return None

    def pipe(
        self,
        hook: str,
        arg_name: str,
        value: Any,
        ws: Workspace | None = None,
        **kwargs: Any,
    ) -> Any:
        """Pass `value` sequentially through enabled implementations of `hook` in plugin order.

        Each non-None return value replaces `value` for the next implementation.
        """
        is_enabled = self._enabled_predicate(ws)
        current = value

        for name, impl in self._ordered_impls(hook):
            if not is_enabled(name):
                continue
            impl_kwargs = self._bind_kwargs(impl, ws, kwargs, extra={arg_name: current})
            ret = self._invoke(name, impl, hook, impl_kwargs)
            if ret is not None:
                current = ret

        return current

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


def load_plugins(builtins: tuple[tuple[str, types.ModuleType], ...] | None = None) -> Plugins:
    """Load and register the built-in plugins and the shared specs.

    Raises PluginError when a plugin's hook implementations are invalid.
    """
    from ow.specs.editors import EditorsSpec
    from ow.specs.python_env import PythonEnvSpec
    from ow.specs.services import ServicesSpec

    if builtins is None:
        builtins = _get_builtins()

    pm = pluggy.PluginManager("ow")
    for spec in (CoreSpec, PythonEnvSpec, EditorsSpec, ServicesSpec):
        pm.add_hookspecs(spec)

    names = []
    for name, module in builtins:
        names.append(name)
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
