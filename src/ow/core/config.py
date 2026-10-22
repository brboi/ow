"""Configuration model: layers, type checking, and setting resolution."""

from __future__ import annotations

import difflib
import fcntl
import os
import tempfile
import tomllib
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w

from ow.errors import UsageError


@dataclass(frozen=True)
class Layer:
    """A configuration layer. `unknown` holds the dotted keys ow does not declare, and
    `wrong_type` those whose TOML type does not match the declared one."""
    values: Mapping[str, object]
    unknown: Mapping[str, object]
    wrong_type: tuple[str, ...]


def _read_toml(path: Path) -> dict[str, Any]:
    """The TOML data of `path`, or `{}`; a UsageError when unreadable or invalid UTF-8/TOML."""
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (UnicodeDecodeError, OSError) as e:
        raise UsageError(f"cannot read {path}: {e}")
    except tomllib.TOMLDecodeError as e:
        raise UsageError(f"Invalid TOML in {path}: {e}")


def read_global(path: Path, declared: Mapping[str, Any]) -> Layer:
    """Read the global config file; an absent file is an empty layer."""
    return _parse_layer(_read_toml(path), declared)


def read_workspace(
    path: Path,
    declared: Mapping[str, Any],
    remotes_of: Callable[[str], Collection[str]] = lambda n: ("origin",),
) -> tuple[Layer, dict[str, str], dict[str, str]]:
    """Read the workspace config file into (layer, accepted_repos, rejected_repos), where
    an accepted repo maps to its qualified base and a rejected one keeps the raw value."""
    return _layer_and_repos(_read_toml(path), declared, remotes_of)


def _layer_and_repos(
    data: Mapping[str, Any],
    declared: Mapping[str, Any],
    remotes_of: Callable[[str], Collection[str]],
) -> tuple[Layer, dict[str, str], dict[str, str]]:
    """Split parsed TOML into a Layer and the accepted/rejected `[repos]` entries."""
    from ow.core.bases import parse_base, BaseError

    repos = data.get("repos")
    if not isinstance(repos, dict):
        # A missing or non-table `repos` is not a repo set; the layer keeps it as-is.
        return _parse_layer(dict(data), declared), {}, {}

    layer = _parse_layer({k: v for k, v in data.items() if k != "repos"}, declared)
    accepted: dict[str, str] = {}
    rejected: dict[str, str] = {}
    for name, base_ref in repos.items():
        if not isinstance(base_ref, str):
            rejected[name] = base_ref
            continue
        remotes = set(remotes_of(name)) or {"origin"}
        try:
            accepted[name] = parse_base(base_ref, lambda segment: segment in remotes)
        except BaseError:
            rejected[name] = base_ref
    return layer, accepted, rejected


def _parse_layer(data: dict[str, Any], declared: Mapping[str, Any]) -> Layer:
    """Split parsed TOML into known typed values, unknown keys, and wrong-typed keys; a
    scalar at the top level, or any key ow does not declare, becomes unknown."""
    values: dict[str, object] = {}
    unknown: dict[str, object] = {}
    wrong_type: list[str] = []
    for table, table_data in data.items():
        if not isinstance(table_data, dict):
            unknown[table] = table_data
            continue
        for key_short, value in table_data.items():
            full_key = f"{table}.{key_short}"
            setting = declared.get(full_key)
            if setting is None:
                unknown[full_key] = value
            elif _check_type(value, setting.type):
                values[full_key] = value
            else:
                wrong_type.append(full_key)
    return Layer(values=values, unknown=unknown, wrong_type=tuple(wrong_type))


def _check_type(value: Any, expected_type: type) -> bool:
    """Whether `value` matches `expected_type`; an int is not a bool, a list means str items."""
    if expected_type is str:
        return isinstance(value, str)
    if expected_type is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type is bool:
        return isinstance(value, bool)
    if expected_type is list:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return False


def resolve_settings(
    declared: Mapping[str, Any],
    global_layer: Layer,
    ws_layer: Layer,
    ws_for_defaults: Any | None,
) -> tuple[dict[str, object], dict[str, str]]:
    """Resolve every declared key to its value and origin (workspace > global > default);
    a callable default is called with `ws_for_defaults` and is unset when that is None."""
    values: dict[str, object] = {}
    origins: dict[str, str] = {}
    for key, setting in declared.items():
        if key in ws_layer.values:
            values[key] = ws_layer.values[key]
            origins[key] = "workspace"
        elif key in global_layer.values:
            values[key] = global_layer.values[key]
            origins[key] = "global"
        else:
            default = setting.default
            if callable(default):
                values[key] = default(ws_for_defaults) if ws_for_defaults is not None else None
            else:
                values[key] = default
            origins[key] = "default"
    return values, origins


def _with_lock(path: Path, fn: Callable[[], Mapping[str, object]]) -> Mapping[str, object]:
    """Run fn while holding an exclusive lock on path's sidecar lock file."""
    lock_path = path.parent / f".{path.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _values_by_table(
    values: Mapping[str, object], declared: Mapping[str, Any]
) -> dict[str, object]:
    """Group declared values into their plugin tables, in declared order, unset skipped."""
    tables: dict[str, dict[str, object]] = {}
    for key in declared:
        if key not in values or values[key] is None or "." not in key:
            continue
        table, short = key.split(".", 1)
        tables.setdefault(table, {})[short] = values[key]
    return tables


def _dropped_keys(
    values: Mapping[str, object],
    layer: Layer,
    raw: Mapping[str, Any],
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """The keys a rewrite drops: the layer's unknown keys, any wrong-typed key it does
    not re-set (with its raw old value), and `extra`."""
    dropped = dict(layer.unknown)
    if extra:
        dropped.update(extra)
    for key in layer.wrong_type:
        if key in values:
            continue
        table, short = key.split(".", 1)
        table_data = raw.get(table)
        if isinstance(table_data, dict) and short in table_data:
            dropped[key] = table_data[short]
    return dropped


def update_global(
    path: Path,
    mutate: Callable[[dict[str, object]], Mapping[str, object]],
    *,
    declared: Mapping[str, Any],
) -> Mapping[str, object]:
    """Rewrite the global config after applying `mutate` to its known values, under an
    exclusive lock so concurrent runs cannot lose an update; returns the dropped keys."""
    def _do_write() -> Mapping[str, object]:
        raw = _read_toml(path)
        layer = _parse_layer(raw, declared)
        values = dict(mutate(dict(layer.values)))
        dropped = _dropped_keys(values, layer, raw)
        _atomic_write_toml(path, _values_by_table(values, declared))
        return dropped

    return _with_lock(path, _do_write)


def update_workspace(
    path: Path,
    repos: Mapping[str, str],
    mutate: Callable[[dict[str, object]], Mapping[str, object]],
    *,
    declared: Mapping[str, Any],
    remotes_of: Callable[[str], Collection[str]] = lambda n: ("origin",),
) -> Mapping[str, object]:
    """Rewrite the workspace config after applying `mutate` to its known values, under an
    exclusive lock; `repos` is the `[repos]` table, and an empty table is left out."""
    def _do_write() -> Mapping[str, object]:
        raw = _read_toml(path)
        layer, _accepted, rejected = _layer_and_repos(raw, declared, remotes_of)
        values = dict(mutate(dict(layer.values)))
        extra = {f"repos.{name}": value for name, value in rejected.items()}
        dropped = _dropped_keys(values, layer, raw, extra)
        tables: dict[str, object] = {"repos": dict(repos)} if repos else {}
        tables.update(_values_by_table(values, declared))
        _atomic_write_toml(path, tables)
        return dropped

    return _with_lock(path, _do_write)


def write_workspace(
    path: Path,
    repos: Mapping[str, str],
    values: Mapping[str, object],
    *,
    declared: Mapping[str, Any],
    remotes_of: Callable[[str], Collection[str]] = lambda n: ("origin",),
) -> Mapping[str, object]:
    """Rewrite the workspace config file with exactly `values`; returns dropped keys."""
    return update_workspace(
        path, repos, lambda _current: values, declared=declared, remotes_of=remotes_of
    )


def _atomic_write_toml(path: Path, data: dict[str, Any]) -> None:
    """Write `data` to `path` atomically via a 0600 temp file beside it; that temp file is
    removed when anything goes wrong, including Ctrl-C."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, delete=False, suffix=".toml"
        ) as handle:
            temp_path = Path(handle.name)
            tomli_w.dump(data, handle)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    except BaseException:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise


def parse_cli_value(setting: Any, args: list[str]) -> object:
    """Parse one setting's CLI arguments: a str takes one value, an int is parsed, a bool is
    only `true`/`false`, and a list takes one item per argument (none yields an empty list)."""
    setting_type = setting.type
    setting_key = setting.key
    
    if setting_type is str:
        if len(args) != 1:
            raise UsageError(f"Expected exactly one value for {setting_key}, got {len(args)}")
        return args[0]
    
    elif setting_type is int:
        if len(args) != 1:
            raise UsageError(f"Expected exactly one value for {setting_key}, got {len(args)}")
        try:
            return int(args[0])
        except ValueError:
            raise UsageError(f"Expected an integer for {setting_key}, got {args[0]!r}")
    
    elif setting_type is bool:
        if len(args) != 1:
            raise UsageError(f"Expected exactly one value for {setting_key}, got {len(args)}")
        val = args[0].lower()
        if val == "true":
            return True
        elif val == "false":
            return False
        else:
            raise UsageError(f"Expected 'true' or 'false' for {setting_key}, got {args[0]!r}")
    
    elif setting_type is list:
        return list(args)
    
    else:
        raise UsageError(f"Unknown type for {setting_key}: {setting_type}")


def closest_key(key: str, declared: Mapping[str, Any]) -> str | None:
    """Find the closest key in declared settings using difflib.
    
    Returns the single closest match with cutoff 0.6, or None if no match.
    """
    matches = difflib.get_close_matches(key, declared.keys(), n=1, cutoff=0.6)
    return matches[0] if matches else None
