"""Which container CLI (`docker` or `podman`) ow drives; shared by the service plugins and the plugin API."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def detect_engine() -> str | None:
    """Return the container CLI to invoke (`OW_CONTAINER_ENGINE`, else `docker` or `podman`), or None when absent."""
    override = os.environ.get("OW_CONTAINER_ENGINE", "").strip()
    if override:
        return override if shutil.which(override) else None
    for candidate in ("docker", "podman"):
        if shutil.which(candidate):
            return candidate
    return None


def detect_container_engine() -> str | None:
    """The engine's name (`docker`, `podman`), even when `OW_CONTAINER_ENGINE` is a path; None when absent."""
    engine = detect_engine()
    return None if engine is None else Path(engine).name
