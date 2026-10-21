"""Configuration model: layers, type checking, and setting resolution."""

from __future__ import annotations

import difflib
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
    """A configuration layer (global or workspace).
    
    values: known keys, type-checked
    unknown: dotted key -> raw value (printed when dropped by rewrite)
    wrong_type: keys whose TOML value has the wrong type (P4)
    """
    values: Mapping[str, object]
    unknown: Mapping[str, object]
    wrong_type: tuple[str, ...]


def read_global(path: Path, declared: Mapping[str, Any]) -> Layer:
    """Read the global config file.
    
    Raises UsageError on invalid TOML.
    Returns empty layer if file doesn't exist.
    """
    if not path.exists():
        return Layer(values={}, unknown={}, wrong_type=())
    
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise UsageError(f"Invalid TOML in {path}: {e}")
    
    return _parse_layer(data, declared, is_workspace=False)


def read_workspace(
    path: Path,
    declared: Mapping[str, Any],
    remotes_of: Callable[[str], Collection[str]] = lambda n: ("origin",),
) -> tuple[Layer, dict[str, str], dict[str, str]]:
    """Read the workspace config file.
    
    Returns (layer, accepted_repos, rejected_repos).
    - accepted_repos: repo_name -> fully qualified base
    - rejected_repos: repo_name -> raw value (rejected by parse_base)
    
    Raises UsageError on invalid TOML.
    Returns empty layer + empty dicts if file doesn't exist.
    """
    from ow.core.bases import parse_base, BaseError
    
    if not path.exists():
        return Layer(values={}, unknown={}, wrong_type=()), {}, {}
    
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise UsageError(f"Invalid TOML in {path}: {e}")
    
    # Extract [repos] separately
    repos_raw = data.pop("repos", {})
    
    # Parse the rest as a normal layer
    layer = _parse_layer(data, declared, is_workspace=True)
    
    # Process [repos] through parse_base
    accepted_repos = {}
    rejected_repos = {}
    
    if not isinstance(repos_raw, dict):
        # [repos] must be a table, add to unknown as repos.<name> flattened
        pass  # Non-dict repos won't be processable
    else:
        for repo_name, base_ref in repos_raw.items():
            if not isinstance(base_ref, str):
                # Wrong type, reject it
                rejected_repos[repo_name] = base_ref
            else:
                # Validate through parse_base
                try:
                    remotes_list = remotes_of(repo_name)
                    # Create a checker function for parse_base
                    remotes_set = set(remotes_list) if remotes_list else {"origin"}
                    remotes_checker = lambda r: r in remotes_set
                    qualified_base = parse_base(base_ref, remotes_checker)
                    accepted_repos[repo_name] = qualified_base
                except BaseError:
                    # parse_base rejected it, save raw value
                    rejected_repos[repo_name] = base_ref
    
    # Rejected [repos] values are reported apart (rejected bases), not as unknown keys.
    unknown_dict = dict(layer.unknown)
    if not isinstance(repos_raw, dict):
        unknown_dict["repos"] = repos_raw
    
    # Create new Layer with updated unknown dict
    layer = Layer(
        values=layer.values,
        unknown=unknown_dict,
        wrong_type=layer.wrong_type,
    )
    
    return layer, accepted_repos, rejected_repos


def _parse_layer(
    data: dict[str, Any],
    declared: Mapping[str, Any],
    is_workspace: bool,
) -> Layer:
    """Parse a TOML dict into a Layer.
    
    Splits data into known typed values, unknown keys, and wrong-typed values.
    Top-level scalars and unknown tables (including [repos]) become unknown keys.
    """
    values = {}
    unknown = {}
    wrong_type = []
    
    # Extract plugin tables and top-level scalars
    for plugin_table_name, plugin_data in data.items():
        if not isinstance(plugin_data, dict):
            # Top-level scalar: add to unknown
            unknown[plugin_table_name] = plugin_data
            continue
        
        # Check if this is a known plugin table (has declared keys)
        has_declared_keys = any(
            f"{plugin_table_name}.{key}" in declared 
            for key in plugin_data.keys()
        )
        
        if not has_declared_keys:
            # Unknown table, flatten it (including [repos])
            for key_short, value in plugin_data.items():
                unknown[f"{plugin_table_name}.{key_short}"] = value
            continue
        
        # Process each key in the declared plugin table
        for key_short, value in plugin_data.items():
            full_key = f"{plugin_table_name}.{key_short}"
            
            if full_key not in declared:
                # Unknown key
                unknown[full_key] = value
                continue
            
            # Type check the value
            setting = declared[full_key]
            expected_type = setting.type
            
            if _check_type(value, expected_type):
                values[full_key] = value
            else:
                wrong_type.append(full_key)
    
    return Layer(
        values=values,
        unknown=unknown,
        wrong_type=tuple(wrong_type),
    )


def _check_type(value: Any, expected_type: type) -> bool:
    """Check if a value matches the expected type.
    
    - str: exact str
    - int: int, but not bool (P4: int excludes bool)
    - bool: exact bool
    - list: list of str
    """
    if expected_type is str:
        return isinstance(value, str)
    elif expected_type is int:
        # int excludes bool
        return isinstance(value, int) and not isinstance(value, bool)
    elif expected_type is bool:
        return isinstance(value, bool)
    elif expected_type is list:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return False


def resolve_settings(
    declared: Mapping[str, Any],
    global_layer: Layer,
    ws_layer: Layer,
    ws_for_defaults: Any | None,
) -> tuple[dict[str, object], dict[str, str]]:
    """Resolve settings across layers with precedence.
    
    Precedence: built-in default < global < workspace
    
    Returns (values, origins):
    - values: dict of every declared key to its final value
    - origins: dict of every declared key to its origin ("default", "global", or "workspace")
    
    A callable default is called with ws_for_defaults (None -> value None).
    """
    values = {}
    origins = {}
    
    for key, setting in declared.items():
        # Try workspace layer first
        if key in ws_layer.values:
            values[key] = ws_layer.values[key]
            origins[key] = "workspace"
        # Try global layer
        elif key in global_layer.values:
            values[key] = global_layer.values[key]
            origins[key] = "global"
        # Use default
        else:
            default = setting.default
            if callable(default):
                # P5: callable default receives ws_for_defaults (None -> value None)
                values[key] = default(ws_for_defaults) if ws_for_defaults is not None else None
            else:
                values[key] = default
            origins[key] = "default"
    
    return values, origins


def write_global(
    path: Path,
    values: Mapping[str, object],
    *,
    declared: Mapping[str, Any],
) -> Mapping[str, object]:
    """Write global config file.
    
    Returns dropped keys (unknown keys from the old file that won't be written,
    and wrong-type keys not present in values, with their raw old values).
    Atomic: uses temp file + os.replace.
    Mode 0600 on the temp file.
    No 'version' key is written.
    """
    # Re-read the old file to find dropped keys
    dropped = {}
    old_layer = read_global(path, declared)
    
    # Get raw TOML data for wrong-type values
    raw_data = {}
    if path.exists():
        try:
            with open(path, "rb") as f:
                raw_data = tomllib.load(f)
        except tomllib.TOMLDecodeError:
            pass
    
    # Add unknown keys from old file
    dropped.update(old_layer.unknown)
    
    # Add wrong-type keys that aren't in the new values (they're being dropped)
    for wrong_key in old_layer.wrong_type:
        if wrong_key not in values:
            # Get the raw value
            plugin_name, key_short = wrong_key.split(".", 1)
            if plugin_name in raw_data and isinstance(raw_data[plugin_name], dict):
                if key_short in raw_data[plugin_name]:
                    dropped[wrong_key] = raw_data[plugin_name][key_short]
    
    # Build the structure to write
    # Group values by plugin name, in declared order, skipping None values
    to_write = {}
    for key in declared:
        if key not in values or values[key] is None:
            continue
        if "." not in key:
            continue
        plugin_name, key_short = key.split(".", 1)
        if plugin_name not in to_write:
            to_write[plugin_name] = {}
        to_write[plugin_name][key_short] = values[key]
    
    # Remove empty tables
    to_write = {k: v for k, v in to_write.items() if v}
    
    # Write atomically
    _atomic_write_toml(path, to_write)
    
    return dropped


def write_workspace(
    path: Path,
    repos: Mapping[str, str],
    values: Mapping[str, object],
    *,
    declared: Mapping[str, Any],
    remotes_of: Callable[[str], Collection[str]] = lambda n: ("origin",),
) -> Mapping[str, object]:
    """Write workspace config file.
    
    Returns dropped keys (unknown keys from the old file that won't be written,
    rejected repos with repos.<name> format, and wrong-type keys).
    Atomic: uses temp file + os.replace.
    Mode 0600 on the temp file.
    [repos] table comes first in the workspace file.
    Nothing is written for a table with no values.
    """
    # Re-read the old file to find dropped keys
    dropped = {}
    
    # Get raw TOML data for wrong-type values
    raw_data = {}
    if path.exists():
        try:
            with open(path, "rb") as f:
                raw_data = tomllib.load(f)
        except tomllib.TOMLDecodeError:
            pass
        
        old_layer, old_repos_accepted, old_repos_rejected = read_workspace(
            path, declared, remotes_of=remotes_of
        )
        dropped.update(old_layer.unknown)
        
        # Add rejected repos with repos.<name> format
        for repo_name, value in old_repos_rejected.items():
            dropped[f"repos.{repo_name}"] = value
        
        # Add wrong-type keys that aren't in the new values
        for wrong_key in old_layer.wrong_type:
            if wrong_key not in values:
                # Get the raw value
                plugin_name, key_short = wrong_key.split(".", 1)
                if plugin_name in raw_data and isinstance(raw_data[plugin_name], dict):
                    if key_short in raw_data[plugin_name]:
                        dropped[wrong_key] = raw_data[plugin_name][key_short]
    
    # Build the structure to write, [repos] first
    to_write = {}
    if repos:
        to_write["repos"] = dict(repos)
    
    # Group values by plugin name, in declared order, skipping None values
    for key in declared:
        if key not in values or values[key] is None:
            continue
        if "." not in key:
            continue
        plugin_name, key_short = key.split(".", 1)
        if plugin_name not in to_write:
            to_write[plugin_name] = {}
        to_write[plugin_name][key_short] = values[key]
    
    # Remove empty tables
    to_write = {k: v for k, v in to_write.items() if v}
    
    # Write atomically
    _atomic_write_toml(path, to_write)
    
    return dropped


def _atomic_write_toml(path: Path, data: dict[str, Any]) -> None:
    """Write a TOML file atomically.
    
    Creates temp file in the same directory, sets mode 0600, then replaces.
    Creates parent directories as needed.
    Cleans up temp file on failure.
    """
    # Create parent dirs
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # Write to temp file in same directory
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            delete=False,
            suffix=".toml",
        ) as f:
            temp_path = Path(f.name)
            tomli_w.dump(data, f)
        
        # Set mode before replacing
        os.chmod(temp_path, 0o600)
        
        # Atomic replace
        os.replace(temp_path, path)
    except Exception:
        # Clean up temp file on failure
        if temp_path and temp_path.exists():
            temp_path.unlink()
        raise


def parse_cli_value(setting: Any, args: list[str]) -> object:
    """Parse CLI arguments for a setting.
    
    - str: exactly one arg
    - int: one arg parsed as int
    - bool: 'true' or 'false' only (refuse '1', 'yes')
    - list: list(args) (zero args allowed = empty list)
    
    Raises UsageError on parse failure.
    """
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
        # list: one item per arg, zero args allowed = empty list
        return list(args)
    
    else:
        raise UsageError(f"Unknown type for {setting_key}: {setting_type}")


def closest_key(key: str, declared: Mapping[str, Any]) -> str | None:
    """Find the closest key in declared settings using difflib.
    
    Returns the single closest match with cutoff 0.6, or None if no match.
    """
    matches = difflib.get_close_matches(key, declared.keys(), n=1, cutoff=0.6)
    return matches[0] if matches else None
