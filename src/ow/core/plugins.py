"""Plugin loader and hook specifications."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
import types
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

import pluggy

from ow.core import config
from ow.errors import PluginError, UsageError

if TYPE_CHECKING:
    from ow.core.facts import RepoFacts
    from ow.core.stacks import MultiWorkspace
    from ow.core.workspace import Workspace

# Public contract version for external / in-repo plugins (`OW_API_VERSION`).
PLUGIN_API_VERSION = 1
SUPPORTED_API_RANGE: tuple[int, int] = (1, 1)

# Hook markers for the "ow" project
hookspec = pluggy.HookspecMarker("ow")
hookimpl = pluggy.HookimplMarker("ow")


@dataclass(frozen=True, kw_only=True)
class Setting:
    """A named, typed setting a plugin declares."""

    key: str  # "<plugin>.<name>"
    type: type  # str | int | bool | list
    default: object | Callable[[Workspace], object] | None
    help: str
    # The value must differ across workspaces: when a new workspace's resolved value is still at
    # its default and equals another indexed workspace's, `ow init`/`ow add` write `<value>-N`
    # into its config. Only meaningful for `str` settings.
    unique: bool = False


@dataclass(frozen=True, kw_only=True)
class GeneratedFile:
    """A file a plugin contributes to the workspace or stack."""

    path: PurePosixPath  # workspace-relative or multi-stack-root-relative
    data: bytes
    mode: int = 0o644

    def __post_init__(self) -> None:
        if isinstance(self.path, str):
            object.__setattr__(self, "path", PurePosixPath(self.path))


@dataclass(frozen=True, kw_only=True)
class Diagnostic:
    """A diagnostic message from a plugin."""

    level: Literal["info", "warning", "error"]
    text: str


@dataclass(frozen=True, kw_only=True)
class KnownRepo:
    """A repo a plugin knows about by name."""

    name: str
    url: str


@dataclass(frozen=True, kw_only=True)
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


@dataclass(frozen=True, kw_only=True)
class ResolvedSpec:
    """A workspace creation plan resolved from a spec (PR, bundle, URL, `@<base>`)."""

    name: str
    repos: tuple[ResolvedRepo, ...]
    source: str


@dataclass(frozen=True, kw_only=True)
class StackInfo:
    """What a plugin tells users about a stack: coordinated ports and a one-line display note."""

    ports: Mapping[str, int] = field(default_factory=dict)
    note: str | None = None


@dataclass(frozen=True, kw_only=True)
class RunCommand:
    """Command a plugin provides to execute a workspace via `ow run` or `ow stop`."""

    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(default_factory=dict)
    self_detaching: bool = False  # True when the command itself starts background services and exits


@dataclass(frozen=True, kw_only=True)
class StatusInfo:
    """External or service status contribution for `ow status` (e.g. GitHub PR, Runbot, Postgres)."""

    plugin: str
    scope: Literal["workspace", "repo", "external", "service"]
    subject: str  # what the line is about: a repo, PR, service or workspace name
    summary: str


@dataclass(frozen=True, kw_only=True)
class PruneCandidate:
    """A workspace a plugin recommends pruning (e.g. its PR was merged/closed)."""

    workspace: Path
    reason: str


@dataclass(frozen=True, kw_only=True)
class SandboxMounts:
    """Extra paths and environment variables a plugin needs inside `ow sandbox`."""

    ro_paths: tuple[Path, ...] = ()
    rw_paths: tuple[Path, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)


class CoreSpec:
    """Core hookspecs declared by ow.

    Error policy, stated per hook as **Propagates** or **Collected**:

    - **Propagates**: a raise aborts the command as a `PluginError` naming the plugin
      (exit 1); a `UsageError` raised by the hook keeps its own exit code (2).
    - **Collected**: a raise becomes a warning naming the plugin and the command
      continues with the other plugins' answers.

    Hooks that return `None` mean "nothing to say"; the first non-None answer wins for
    `first`-style hooks (`ow_runner`, `ow_stop`, ...), every answer is gathered otherwise.
    """

    @hookspec
    def ow_settings(self) -> list[Setting]:
        """Return this plugin's settings; every key must start with `<plugin name>.`.

        An off-prefix or duplicate key is dropped with a warning (the plugin still loads),
        so a plugin can never shadow another plugin's setting. A raise aborts the command
        for a built-in plugin; for an in-repo plugin it is collected: that plugin alone is
        skipped with a warning and the others still load.
        """

    @hookspec
    def ow_generated_files(
        self,
        ws: Workspace,
        answers: Answers,
        multi: MultiWorkspace | None,
    ) -> list[GeneratedFile | Diagnostic]:
        """Return generated files and optional diagnostic messages. Propagates."""

    @hookspec
    def ow_diagnostics(
        self, ws: Workspace, multi: MultiWorkspace | None
    ) -> list[Diagnostic]:
        """Return diagnostic messages. Collected: a raise becomes a warning, never an abort."""

    @hookspec
    def ow_known_repos(self) -> list[KnownRepo]:
        """Return known repos this plugin provides. Propagates."""

    @hookspec
    def ow_workspace_created(self, ws: Workspace) -> None:
        """Event: workspace was created. Propagates."""

    @hookspec
    def ow_refreshed(self, ws: Workspace, owned: tuple[PurePosixPath, ...]) -> None:
        """Event: refresh completed. owned is every path ow wrote or up-to-date. Propagates."""

    @hookspec
    def ow_resolve_spec(
        self,
        spec: str,
        known_repos: Mapping[str, KnownRepo],
        settings: Mapping[str, object],
    ) -> ResolvedSpec | None:
        """Resolve a spec string (e.g. `enterprise#123`, `@18.0`, a Runbot URL) to a workspace plan. Propagates."""

    @hookspec
    def ow_expand_workspace_repos(
        self,
        repos: tuple[ResolvedRepo, ...],
        known_repos: Mapping[str, KnownRepo],
        settings: Mapping[str, object],
    ) -> tuple[ResolvedRepo, ...] | None:
        """Optionally expand resolved repos with implicit companion repos (e.g. Odoo core). Propagates."""

    @hookspec
    def ow_runner(
        self, ws: Workspace, args: tuple[str, ...], detach: bool, answers: Answers
    ) -> RunCommand | None:
        """Return the command to run this workspace via `ow run`, or None. Propagates."""

    @hookspec
    def ow_running(self, ws: Workspace) -> list[str] | None:
        """Return active background process/service descriptions for `ws` if managed by this plugin.

        Collected: a raise becomes a warning and the plugin counts as not running.
        """

    @hookspec
    def ow_stop(self, ws: Workspace) -> RunCommand | list[str] | None:
        """Stop background processes managed by this plugin for `ws` (`ow stop`). Propagates."""

    @hookspec
    def ow_stop_services(self, ws: Workspace | None) -> list[str] | None:
        """Stop shared global services managed by this plugin (`ow stop --services` / `--all`).

        Collected: a failing plugin is reported and the others are still stopped.
        """

    @hookspec
    def ow_logs(self, ws: Workspace) -> list[tuple[str, Path]] | None:
        """Return the `(label, log_path)` files `ow logs` shows or follows for `ws`. Propagates."""

    @hookspec
    def ow_before_run(
        self, ws: Workspace, workspaces: Sequence[Workspace]
    ) -> list[str] | None:
        """Prepare shared local services before `ow run` starts workspaces; return info lines.

        Collected: a failure warns without blocking the run.
        """

    @hookspec
    def ow_reset_run(
        self, ws: Workspace, answers: Answers
    ) -> list[str] | None:
        """Reset local runtime state (e.g. database, filestore) before `ow run --reset`; return info lines.

        Propagates. A plugin implementing this without `ow_reset_preview` is listed as `<plugin>: local state` in the confirmation prompt.
        """

    @hookspec
    def ow_reset_preview(self, ws: Workspace, answers: Answers) -> list[str] | None:
        """Describe what `ow_reset_run` would remove, one line each (e.g. `database odoo@localhost:5433/rev`, `filestore <path>`); called before the confirmation prompt.

        Propagates: a failing preview aborts `--reset` rather than letting the user confirm blind. Return None or an empty list when there is nothing to remove.
        """

    @hookspec
    def ow_status(
        self,
        ws: Workspace | None,
        facts: Sequence[RepoFacts],
        spec: str | None,
        offline: bool,
        known_repos: Mapping[str, KnownRepo],
        settings: Mapping[str, object],
    ) -> list[StatusInfo]:
        """Return external status contributions for a workspace or an external spec (PR, Runbot URL).

        Collected: a raise becomes a warning and the plugin contributes nothing.
        """

    @hookspec
    def ow_prune_candidates(
        self,
        workspaces: Sequence[Workspace],
        offline: bool,
    ) -> list[PruneCandidate]:
        """Return workspaces recommended for pruning (e.g. merged/closed PRs).

        Collected: a raise becomes a warning and the plugin recommends nothing.
        """

    @hookspec
    def ow_multi_workspace_files(
        self,
        multi: MultiWorkspace,
        answers: Answers,
    ) -> list[GeneratedFile | Diagnostic]:
        """Return generated files for a multi-stack workspace (written in `multi.root`).

        `answers` is bound to the first stack of `multi`; ask about another with
        `answers.for_workspace(stack)`. Not called when `multi` has no stack. Propagates.
        """

    @hookspec
    def ow_multi_runner(
        self,
        multi: MultiWorkspace,
        args: tuple[str, ...],
        detach: bool,
    ) -> RunCommand | None:
        """Return a single workspace-wide command for `ow run` at a multi-stack root, or None. Propagates."""

    @hookspec
    def ow_multi_stop(
        self,
        multi: MultiWorkspace,
    ) -> RunCommand | list[str] | None:
        """Stop a multi-stack workspace managed by this plugin (`ow stop`), or None. Propagates."""

    @hookspec
    def ow_multi_logs(self, multi: MultiWorkspace) -> list[tuple[str, Path]] | None:
        """Return the `(label, log_path)` files `ow logs` shows or follows for a multi-stack workspace. Propagates."""

    @hookspec
    def ow_stack_values(
        self,
        ws: Workspace | None,
        ws_values: Mapping[str, object],
        siblings: Sequence[Workspace],
        pending: Sequence[Mapping[str, object]],
        global_settings: Mapping[str, object],
        repo_paths: Sequence[Path] | None,
        repo_names: Sequence[str] | None,
    ) -> dict[str, object] | None:
        """Return the settings to pin explicitly for a stack (e.g. database name, coordinated ports). Propagates.

        `ws` is the existing workspace being promoted to a stack (None for a new stack),
        `ws_values` the stack's own explicit settings (which must not be overridden),
        `siblings` the stacks already on disk next to it, and `pending` the settings of stacks
        planned earlier in the same command, which may not exist on disk yet.
        """

    @hookspec
    def ow_stack_name(self, ws: Workspace) -> str | None:
        """Suggest a stack directory name for `ws` during promotion to multi-stack. Propagates."""

    @hookspec
    def ow_stack_info(self, ws: Workspace) -> StackInfo | None:
        """Return the ports and display note users see for `ws` (`ow ls`, `ow status`, `ow run`). Propagates."""

    @hookspec
    def ow_is_base_branch(self, repo: str, branch: str) -> bool | None:
        """Return True if `branch` on `repo` is an upstream base series (checked out detached). Propagates."""

    @hookspec
    def ow_sandbox_mounts(self, ws: Workspace, answers: Answers) -> SandboxMounts | None:
        """Return extra sandbox mounts and environment variables needed for `ws`, or None. Propagates."""


def _get_builtins() -> tuple[tuple[str, types.ModuleType], ...]:
    """Get all built-in plugin modules in order."""
    from ow.plugins import (
        github,
        local,
        mailsink,
        mise,
        odoo,
        postgres,
        runbot,
        sandbox,
        vscode,
        zed,
    )

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
        ("sandbox", sandbox),
    )


class Plugins:
    """Manages plugin registry and hooks."""

    def __init__(
        self,
        pm: pluggy.PluginManager,
        names: tuple[str, ...],
        *,
        builtins: tuple[tuple[str, types.ModuleType], ...],
        external_plugins: tuple[tuple[str, Path], ...] = (),
    ) -> None:
        self.pm = pm
        self.names = names
        self._builtins = builtins
        self.external_plugins = external_plugins

    def with_repo_plugins(
        self,
        repo_dirs: Sequence[tuple[str, Path] | Path],
    ) -> tuple[Plugins, tuple[str, ...]]:
        """Return `(plugins_with_in_repo_plugins, warnings)` for `repo_dirs`.

        Discovers `[tool.ow] plugin` (in `pyproject.toml`) or `ow_plugin.py` in each repo dir.
        Untrusted files are skipped with a warning pointing to `ow trust`. Trusted files must
        declare an integer `OW_API_VERSION` within `SUPPORTED_API_RANGE`.
        """
        from ow.core import git, trust

        discovered = trust.discover_repo_plugins(repo_dirs)
        if not discovered:
            return self, ()

        warnings: list[str] = []
        extra_modules: list[tuple[str, types.ModuleType, Path]] = []
        seen_names = {name for name, _mod in self._builtins}

        for item in discovered:
            shown = git.display_path(item.path)
            if not trust.is_trusted(item.path):
                warnings.append(
                    f"untrusted plugin {shown}; run `ow trust` to enable it"
                )
                continue

            digest = hashlib.sha256(str(item.path).encode("utf-8")).hexdigest()[:12]
            mod_name = f"ow_repo_plugin_{item.repo_name}_{digest}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, item.path)
                if spec is None or spec.loader is None:
                    warnings.append(f"skipping plugin {shown}: cannot load module spec")
                    continue
                module = importlib.util.module_from_spec(spec)
                # Never litter the plugin's repo with `__pycache__`: the repo is the user's checkout.
                previous = sys.dont_write_bytecode
                sys.dont_write_bytecode = True
                try:
                    spec.loader.exec_module(module)
                finally:
                    sys.dont_write_bytecode = previous
            except Exception as exc:
                warnings.append(f"skipping plugin {shown}: failed to import ({exc})")
                continue

            api_ver = getattr(module, "OW_API_VERSION", None)
            if (
                not isinstance(api_ver, int)
                or isinstance(api_ver, bool)
                or not (SUPPORTED_API_RANGE[0] <= api_ver <= SUPPORTED_API_RANGE[1])
            ):
                min_v, max_v = SUPPORTED_API_RANGE
                supported = f"v{min_v}" if min_v == max_v else f"v{min_v}-v{max_v}"
                warnings.append(
                    f"skipping plugin {shown}: incompatible OW_API_VERSION={api_ver!r} "
                    f"(ow supports {supported})"
                )
                continue

            raw_name = getattr(module, "PLUGIN_NAME", None) or item.repo_name
            plugin_name = str(raw_name).strip()
            if not _valid_plugin_name(plugin_name) or plugin_name in seen_names:
                warnings.append(
                    f"skipping plugin {shown}: plugin name {plugin_name!r} is invalid or already registered"
                )
                continue
            seen_names.add(plugin_name)
            extra_modules.append((plugin_name, module, item.path))

        if not extra_modules:
            return self, tuple(warnings)

        base_builtins = self._builtins
        # A plugin whose registration or `ow_settings` fails is dropped alone; the others still load.
        while extra_modules:
            combined = tuple(base_builtins) + tuple((name, mod) for name, mod, _p in extra_modules)
            try:
                loaded = load_plugins(combined)
                enriched = Plugins(
                    loaded.pm,
                    loaded.names,
                    builtins=base_builtins,
                    external_plugins=tuple((name, p) for name, _m, p in extra_modules),
                )
                _settings, setting_warnings = enriched._collect_settings()
            except PluginError as exc:
                remaining = [item for item in extra_modules if item[0] != exc.plugin]
                warnings.append(f"skipping in-repo plugin ({exc})")
                if len(remaining) == len(extra_modules):
                    return self, tuple(warnings)
                extra_modules = remaining
                continue
            warnings.extend(setting_warnings)
            return enriched, tuple(warnings)
        return self, tuple(warnings)

    def _global_enabled_flags(self) -> Mapping[str, object]:
        """Read `<name>.enabled` overrides from global config."""
        from ow.core.paths import global_config

        declared = {
            f"{name}.enabled": Setting(key=f"{name}.enabled", type=bool, default=True, help="")
            for name in self.names
        }
        try:
            return config.read_global(global_config(), declared).values
        except (OSError, config.UsageError):
            return {}

    def is_enabled(self, name: str, ws: Workspace | None = None) -> bool:
        """Whether plugin `name` is enabled for `ws` (or globally when `ws` is None)."""
        return self._enabled_predicate(ws)(name)

    def settings(self) -> tuple[Setting, ...]:
        """Return all settings from all plugins, in order.

        Includes a synthesized `<name>.enabled` (bool, default True) for each plugin. A setting
        whose key is not prefixed `<plugin name>.`, or whose key an earlier plugin already
        declared, is dropped: a plugin can never shadow another plugin's setting.
        `with_repo_plugins` reports the dropped settings of in-repo plugins as warnings.
        """
        return self._collect_settings()[0]

    def _collect_settings(self) -> tuple[tuple[Setting, ...], tuple[str, ...]]:
        """Return `(settings, warnings)`: the settings `settings()` lists and why others were dropped."""
        result: list[Setting] = []
        warnings: list[str] = []
        seen: set[str] = set()
        impls = self.pm.hook.ow_settings.get_hookimpls()
        impl_to_name = {impl.plugin: self.pm.get_name(impl.plugin) for impl in impls}

        for name in self.names:
            enabled = Setting(
                key=f"{name}.enabled",
                type=bool,
                default=True,
                help=f"Enable or disable the {name} plugin",
            )
            result.append(enabled)
            seen.add(enabled.key)
            prefix = f"{name}."

            for impl in impls:
                if impl_to_name[impl.plugin] != name:
                    continue
                try:
                    plugin_settings = impl.function()
                except PluginError:
                    raise
                except Exception as e:
                    raise PluginError(name, "ow_settings", e) from e
                for setting in plugin_settings or ():
                    if not setting.key.startswith(prefix) or len(setting.key) == len(prefix):
                        warnings.append(
                            f"plugin {name}: ignoring setting {setting.key!r} "
                            f"(keys must start with {prefix!r})"
                        )
                    elif setting.key in seen:
                        warnings.append(
                            f"plugin {name}: ignoring setting {setting.key!r} (already declared)"
                        )
                    else:
                        seen.add(setting.key)
                        result.append(setting)

        return tuple(result), tuple(warnings)

    def diagnostics(
        self, ws: Workspace, multi: MultiWorkspace | None = None
    ) -> tuple[tuple[tuple[str, tuple[Diagnostic, ...]], ...], tuple[str, ...]]:
        """Collect `ow_diagnostics` as `(per-plugin diagnostics, warnings)`; a plugin that fails never aborts.

        `ow_diagnostics` is a *collected* hook: a raise, or an answer that is not a list of
        `Diagnostic`, becomes one warning naming the plugin and the other plugins still answer.
        Plugins that report nothing are omitted. Every caller (session, refresh) goes through
        here so the policy is the same everywhere.
        """
        found: list[tuple[str, tuple[Diagnostic, ...]]] = []
        warnings: list[str] = []
        for name, result in self.call("ow_diagnostics", ws, errors="collect", multi=multi):
            if isinstance(result, PluginError):
                warnings.append(f"{name}: ow_diagnostics failed: {result.cause}")
            elif not isinstance(result, list) or not all(isinstance(d, Diagnostic) for d in result):
                warnings.append(
                    f"{name}: ow_diagnostics failed: expected a list of Diagnostic, "
                    f"got {type(result).__name__}"
                )
            elif result:
                found.append((name, tuple(result)))
        return tuple(found), tuple(warnings)

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

    def _enabled_impls(self, hook: str, ws: Workspace | None) -> Iterator[tuple[str, Any]]:
        """Yield `(plugin_name, hookimpl)` for the plugins enabled for `ws` (or globally), in plugin order."""
        is_enabled = self._enabled_predicate(ws)
        for name, impl in self._ordered_impls(hook):
            if is_enabled(name):
                yield name, impl

    def _bind_kwargs(
        self,
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
            elif arg == "answers" and ws is not None:
                impl_kwargs[arg] = Answers(self, ws)
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

        for name, impl in self._enabled_impls(hook, ws):
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

    def implementers(self, hook: str, ws: Workspace | None = None) -> tuple[str, ...]:
        """Names of the plugins enabled for `ws` (or globally) that implement `hook`, in plugin order."""
        return tuple(name for name, _impl in self._enabled_impls(hook, ws))

    def first_with_name(
        self, hook: str, ws: Workspace | None = None, **kwargs: Any
    ) -> tuple[str, object] | None:
        """Return `(plugin_name, answer)` for the first non-None answer of `hook`, else None."""
        for name, impl in self._enabled_impls(hook, ws):
            ret = self._invoke(name, impl, hook, self._bind_kwargs(impl, ws, kwargs))
            if ret is not None:
                return name, ret
        return None

    def first(self, hook: str, ws: Workspace | None = None, **kwargs: Any) -> object | None:
        """Return the first non-None answer of `hook` among enabled plugins, in plugin order.

        Stops at that answer: later implementations do not run. Enablement and error
        semantics are those of `call` with errors="raise".
        """
        hit = self.first_with_name(hook, ws=ws, **kwargs)
        return hit[1] if hit is not None else None

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
        current = value

        for name, impl in self._enabled_impls(hook, ws):
            impl_kwargs = self._bind_kwargs(impl, ws, kwargs, extra={arg_name: current})
            ret = self._invoke(name, impl, hook, impl_kwargs)
            if ret is not None:
                current = ret

        return current

    def answers(self, ws: Workspace) -> Answers:
        """Return the `answers` callable for `ws`: collects answers to a hook, skipping disabled plugins.

        The returned callable takes a hook name and returns a list of results in tuple order.
        It skips disabled plugins and turns raises into PluginError naming the answering plugin.
        """
        return Answers(self, ws)


class Answers:
    """What a plugin gets as `answers`: `answers("hook")` lists every enabled plugin's answer for one workspace.

    `for_workspace(other)` is the same callable bound to another workspace of the same
    session (e.g. a sibling stack), so plugin helpers can ask about it.
    """

    def __init__(self, plugins: Plugins, ws: Workspace) -> None:
        self._plugins = plugins
        self._ws = ws

    def __call__(self, hook_name: str) -> list:
        return [result for _name, result in self._plugins.call(hook_name, ws=self._ws)]

    def for_workspace(self, ws: Workspace) -> Answers:
        """Return the same callable bound to `ws`."""
        return Answers(self._plugins, ws)


_PLUGIN_NAME = re.compile(r"[A-Za-z0-9_-]+")


def _valid_plugin_name(name: str) -> bool:
    """A plugin name is a bare TOML key (it prefixes the plugin's settings and its config table)."""
    return _PLUGIN_NAME.fullmatch(name) is not None


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
    for name, _module in builtins:
        if not _valid_plugin_name(name):
            raise PluginError(name, "register", "plugin name must match [A-Za-z0-9_-]+")
    for name, module in builtins:
        names.append(name)
        try:
            pm.register(module, name=name)
        except (pluggy.PluginValidationError, ValueError) as e:
            raise PluginError(name, "register", str(e)) from e

    # Hookimpls whose name matches no spec surface here
    try:
        pm.check_pending()
    except pluggy.PluginValidationError as e:
        raise PluginError(pm.get_name(e.plugin), "check_pending", str(e)) from e

    return Plugins(pm, tuple(names), builtins=builtins)
