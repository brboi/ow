"""Canonical JSON serialization helpers for `--json` output (schema v1)."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path, PurePath
from typing import Any, Mapping

SCHEMA_VERSION = 1


def to_json_value(obj: Any) -> Any:
    """Recursively convert dataclasses, Paths, tuples, sets, and mappings to JSON-safe values."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, PurePath):
        return obj.as_posix() if not isinstance(obj, Path) else str(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_json_value(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): to_json_value(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_json_value(item) for item in obj]
    return str(obj)


def envelope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap a command payload with `schema_version`."""
    data = {"schema_version": SCHEMA_VERSION}
    data.update({k: to_json_value(v) for k, v in payload.items()})
    return data
