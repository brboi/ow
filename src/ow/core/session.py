"""Session: the immutable state every command starts from."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ow.core import bases, git, stacks
from ow.core.config import (
    Layer,
    as_int,
    read_global,
    read_multi_config,
    read_workspace_config,
    resolve_settings,
)
from ow.core.paths import global_config, repos_dir
from ow.core.plugins import Diagnostic, Plugins, load_plugins
from ow.core.workspace import (
    RepoLocation,
    Workspace,
    build_workspace,
    derive_repos,
    resolve_root,
    stray_checkouts,
)
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.plugins import Setting


_EMPTY_LAYER = Layer(values={}, unknown={}, wrong_type=())


@dataclass(frozen=True)
class Session:
    """Immutable session state for every command."""

    plugins: Plugins
    declared: Mapping[str, Setting]
    workspace: Workspace | None
    locations: tuple[RepoLocation, ...]
    origins: Mapping[str, Literal["default", "global", "workspace", "stack"]]
    warnings: tuple[str, ...]
    diagnostics: tuple[tuple[str, Diagnostic], ...]
    config_path: Path | None
    rejected_bases: Mapping[str, str]
    accepted_bases: Mapping[str, str]
    global_layer: Layer
    workspace_layer: Layer
    stack_name: str | None = None
    stack_layer: Layer = _EMPTY_LAYER

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


def global_settings(plugins: Plugins) -> dict[str, object]:
    """Resolve effective global settings (defaults + global layer) without a workspace."""
    declared = {s.key: s for s in plugins.settings()}
    layer = read_global(global_config(), declared)
    values, _origins = resolve_settings(
        declared,
        layer,
        Layer(values={}, unknown={}, wrong_type=()),
        ws_for_defaults=None,
    )
    return values


def _workspace_repo_dirs(root: Path, *, is_stack: bool) -> list[tuple[str, Path]]:
    """Return `(repo_name, path)` pairs of git worktrees under `root` (or all sibling stacks)."""
    roots_to_scan: list[Path]
    if is_stack:
        parent = root.parent.resolve()
        stack_paths = stacks.detect_multi_workspace(parent)
        roots_to_scan = list(stack_paths) if stack_paths else [root]
    else:
        roots_to_scan = [root]

    found: list[tuple[str, Path]] = []
    for scan_root in roots_to_scan:
        for loc in derive_repos(scan_root):
            found.append((loc.name, loc.path))
    return found


def workspace_plugins(root: Path, plugins: Plugins) -> tuple[Plugins, tuple[str, ...]]:
    """`plugins` plus the trusted in-repo plugins of the workspace at `root` (every sibling stack for a stack).

    The set every session on `root` goes through; the warnings name untrusted or broken plugins.
    """
    repo_dirs = _workspace_repo_dirs(root, is_stack=stacks.is_stack_dir(root))
    return plugins.with_repo_plugins(repo_dirs)


def open_session(
    workspace: str | None = None,
    *,
    need_workspace: bool,
    plugins: Plugins | None = None,
    cwd: Path | None = None,
    ignore_global: Collection[str] = (),
    ignore_workspace: Collection[str] = (),
    discover_repo_plugins: bool = True,
) -> Session:
    """Open a session for a command to run.

    An explicit `-w` that cannot be resolved always raises; an implicit lookup falls
    back to a workspace-less session when `need_workspace` is False.
    """
    if plugins is None:
        plugins = load_plugins()

    if cwd is None:
        cwd = Path.cwd()

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
        declared_list = plugins.settings()
        declared = {s.key: s for s in declared_list}
        global_layer = read_global(global_config(), declared)
        ws_layer_empty = Layer(values={}, unknown={}, wrong_type=())

        values, origins = resolve_settings(
            declared, global_layer, ws_layer_empty, ws_for_defaults=None
        )

        warnings = _collect_warnings(
            global_layer,
            ws_layer_empty,
            declared,
            {},
            ignore_global=ignore_global,
            ignore_workspace=ignore_workspace,
        )
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

    is_stack = stacks.is_stack_dir(root)
    repo_plugin_warnings: tuple[str, ...] = ()
    if discover_repo_plugins:
        plugins, repo_plugin_warnings = workspace_plugins(root, plugins)

    declared_list = plugins.settings()
    declared = {s.key: s for s in declared_list}

    config_path = (root.parent / ".ow" / "config.toml") if is_stack else (root / ".ow" / "config.toml")
    stack_name = root.name if is_stack else None
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

    if is_stack:
        assert stack_name is not None
        workspace_layer, stacks_cfg = read_multi_config(
            config_path, declared, remotes_of=remotes_of
        )
        stack_cfg = stacks_cfg.get(stack_name)
        if stack_cfg is not None:
            stack_layer = stack_cfg.layer
            accepted_bases = stack_cfg.accepted_repos
            rejected_bases = stack_cfg.rejected_repos
        else:
            stack_layer = _EMPTY_LAYER
            accepted_bases = {}
            rejected_bases = {}
    else:
        workspace_layer, accepted_bases, rejected_bases = read_workspace_config(
            config_path, declared, remotes_of=remotes_of
        )
        stack_layer = _EMPTY_LAYER

    locations = derive_repos(root, accepted_bases)

    # Callable defaults see the real repos but no settings, so resolution never
    # depends on the values it is computing.
    defaults_workspace = build_workspace(root, locations, {}, is_stack=is_stack)
    values, origins = resolve_settings(
        declared,
        global_layer,
        workspace_layer,
        ws_for_defaults=defaults_workspace,
        stack_layer=stack_layer if is_stack else None,
    )

    workspace = build_workspace(root, locations, values, is_stack=is_stack, origins=origins)

    warnings = repo_plugin_warnings + _collect_warnings(
        global_layer,
        workspace_layer,
        declared,
        rejected_bases,
        stack_layer=stack_layer if is_stack else None,
        stack_name=stack_name,
        ignore_global=ignore_global,
        ignore_workspace=ignore_workspace,
    )

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
        stack_name=stack_name,
        stack_layer=stack_layer,
    )


def _collect_warnings(
    global_layer: Layer,
    workspace_layer: Layer,
    declared: Mapping[str, object],
    rejected_bases: Mapping[str, str],
    *,
    stack_layer: Layer | None = None,
    stack_name: str | None = None,
    ignore_global: Collection[str] = (),
    ignore_workspace: Collection[str] = (),
) -> tuple[str, ...]:
    """One warning per config file, naming every unknown key and rejected base."""
    warnings = []

    global_parts = [
        f"unknown setting {key} in {global_config()}; "
        f"remove it with `ow config unset --global {key}`"
        for key in global_layer.unknown
        if key not in ignore_global
    ]
    global_parts += [
        _wrong_type_note(key, global_config(), declared)
        for key in global_layer.wrong_type
        if key not in ignore_global
    ]
    if global_parts:
        warnings.append("; ".join(global_parts))

    ws_unknown = {key: value for key, value in workspace_layer.unknown.items() if key not in ignore_workspace}
    legacy_keys = [key for key, value in ws_unknown.items() if is_legacy_key(key, value, declared)]
    ws_parts = [
        f"unknown setting {key} in .ow/config.toml; "
        f"remove it with `ow config unset {key}`"
        for key in ws_unknown
        if key not in legacy_keys
    ]
    if legacy_keys:
        ws_parts.append(
            f".ow/config.toml is in an older ow format ({', '.join(legacy_keys)}); "
            "run `ow init` to migrate it"
        )
    ws_parts += [
        _wrong_type_note(key, ".ow/config.toml", declared)
        for key in workspace_layer.wrong_type
        if key not in ignore_workspace
    ]
    if stack_layer is not None:
        where = f".ow/config.toml [stacks.{stack_name}]"
        ws_parts += [
            f"unknown setting {key} in {where}; "
            f"remove it with `ow config unset {key}`"
            for key in stack_layer.unknown
            if key not in ignore_workspace
        ]
        ws_parts += [
            _wrong_type_note(key, where, declared)
            for key in stack_layer.wrong_type
            if key not in ignore_workspace
        ]
    ws_parts += [_rejected_base_note(name, raw) for name, raw in rejected_bases.items()]
    if ws_parts:
        warnings.append("; ".join(ws_parts))

    return tuple(warnings)


LEGACY_ROOT_KEYS = ("version", "templates")


def _fits(value: object, expected: type) -> bool:
    """Whether a TOML `value` has the type a setting declares (an int is not a bool)."""
    if expected is int:
        return as_int(value) is not None
    if expected is list:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return isinstance(value, expected) and not (expected is not bool and isinstance(value, bool))


def vars_target(key: str, value: object, declared: Mapping[str, Any]) -> str | None:
    """The `odoo.<name>` setting that the `[vars]` key `vars.<name>` moves to, or None.

    `ow init` moves a ow 2.x `[vars]` key only when `odoo.<name>` is declared, still current
    and of the value's type; any other `[vars]` key is an unknown key like the rest.
    """
    if not key.startswith("vars."):
        return None
    target = f"odoo.{key.removeprefix('vars.')}"
    setting = declared.get(target)
    if setting is None or setting.renamed_to or not _fits(value, setting.type):
        return None
    return target


def is_legacy_key(key: str, value: object, declared: Mapping[str, Any]) -> bool:
    """Whether `ow init` migrates `key`: `version`, `templates`, or a `[vars]` key it moves."""
    return key in LEGACY_ROOT_KEYS or vars_target(key, value, declared) is not None


def _rejected_base_note(name: str, raw: object) -> str:
    """Why a `[repos]` entry is ignored; the ow 3.0.0a1 `<base>..<feature>` format has its remedy."""
    if isinstance(raw, str) and ".." in raw:
        try:
            bases.split_range(raw)
        except bases.BaseError:
            pass
        else:
            return (
                f"ignoring {name} = {raw!r} in .ow/config.toml: the ow 3.0.0a1 format; "
                "run `ow init` to migrate it"
            )
    return f"ignoring default base {raw} for {name} in .ow/config.toml: invalid base"


def _wrong_type_note(key: str, where: Path | str, declared: Mapping[str, object]) -> str:
    """A key whose TOML value has the wrong type keeps its default."""
    setting = declared.get(key)
    kind = getattr(setting.type, "__name__", str(setting.type)) if setting else "?"
    return f"{key} in {where} must be {kind}; using its default"


def _call_diagnostics(
    plugins: Plugins,
    workspace: Workspace | None,
) -> tuple[tuple[str, ...], tuple[tuple[str, Diagnostic], ...]]:
    """Collect ow_diagnostics from every plugin; a failing plugin becomes a warning, never an abort."""
    if workspace is None:
        return (), ()

    found, warnings = plugins.diagnostics(workspace)
    diagnostics = tuple((name, diag) for name, diags in found for diag in diags)
    return (
        warnings + tuple(f"{name}: {diag.text}" for name, diag in diagnostics if diag.level == "warning"),
        diagnostics,
    )
