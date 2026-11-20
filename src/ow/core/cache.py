"""Small file-backed TTL JSON cache under $XDG_CACHE_HOME/ow."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any

from ow.core.fs import atomic_write
from ow.core.paths import cache_dir

_VALID_NAMESPACE = re.compile(r"^[A-Za-z0-9._-]+$")


def _entry_path(namespace: str, key: str) -> Path:
    if not _VALID_NAMESPACE.fullmatch(namespace) or namespace in (".", ".."):
        raise ValueError(f"invalid cache namespace: {namespace!r}")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return cache_dir() / namespace / f"{digest}.json"


def get(namespace: str, key: str, *, ttl: float, allow_expired: bool = False) -> Any | None:
    """Return cached payload if present and fresh (or even if stale when `allow_expired`)."""
    path = _entry_path(namespace, key)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or "stored_at" not in raw or "value" not in raw:
        return None
    stored_at = raw["stored_at"]
    if (
        not isinstance(stored_at, (int, float))
        or isinstance(stored_at, bool)
        or not math.isfinite(stored_at)
    ):
        return None
    if not allow_expired and (ttl <= 0 or (time.time() - stored_at) > ttl):
        return None
    return raw["value"]


def set(namespace: str, key: str, value: Any) -> None:
    """Atomically store `value` under `(namespace, key)`; OSErrors are ignored."""
    path = _entry_path(namespace, key)
    try:
        payload = json.dumps({"stored_at": time.time(), "value": value}, ensure_ascii=False)
        atomic_write(path, payload.encode("utf-8"), suffix=".json")
    except OSError:
        pass
