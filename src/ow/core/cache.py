"""Small file-backed TTL JSON cache under $XDG_CACHE_HOME/ow."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from ow.core.paths import cache_dir


def _entry_path(namespace: str, key: str) -> Path:
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
    if not isinstance(stored_at, (int, float)):
        return None
    if not allow_expired and (ttl <= 0 or (time.time() - stored_at) > ttl):
        return None
    return raw["value"]


def set(namespace: str, key: str, value: Any) -> None:
    """Atomically store `value` under `(namespace, key)`; errors are ignored."""
    path = _entry_path(namespace, key)
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"stored_at": time.time(), "value": value}, ensure_ascii=False)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".json"
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
        os.replace(temp_path, path)
    except OSError:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
