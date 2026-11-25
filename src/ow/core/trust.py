"""In-repo plugin discovery and location-based trust registry (`ow trust`)."""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import tomli_w

from ow.core.fs import atomic_write, sidecar_lock
from ow.core.paths import trusted_plugins_file
from ow.errors import UsageError


@dataclass(frozen=True)
class DiscoveredPlugin:
    """An in-repo plugin file found inside a git worktree."""

    repo_name: str
    path: Path  # resolved canonical path to the .py file


def discover_repo_plugin(repo_dir: Path, *, repo_name: str | None = None) -> DiscoveredPlugin | None:
    """Find an in-repo plugin in `repo_dir`: `[tool.ow] plugin = "..."` in `pyproject.toml`,
    falling back to `ow_plugin.py` at the repo root.

    Paths escaping `repo_dir` (e.g. via `..` or symlinks) are ignored.
    """
    try:
        resolved_repo = repo_dir.resolve()
    except OSError:
        return None
    if not resolved_repo.is_dir():
        return None

    name = repo_name or resolved_repo.name
    candidate: Path | None = None

    pyproject = resolved_repo / "pyproject.toml"
    if pyproject.is_file():
        try:
            with open(pyproject, "rb") as handle:
                data = tomllib.load(handle)
            tool = data.get("tool")
            ow_section = tool.get("ow") if isinstance(tool, dict) else None
            rel = ow_section.get("plugin") if isinstance(ow_section, dict) else None
            if isinstance(rel, str) and rel.strip():
                raw = Path(rel.strip())
                if not raw.is_absolute():
                    resolved_cand = (resolved_repo / raw).resolve()
                    if resolved_cand.is_relative_to(resolved_repo) and resolved_cand.is_file():
                        candidate = resolved_cand
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            pass

    if candidate is None:
        fallback = resolved_repo / "ow_plugin.py"
        if fallback.is_file():
            try:
                resolved_fb = fallback.resolve()
                if resolved_fb.is_relative_to(resolved_repo) and resolved_fb.is_file():
                    candidate = resolved_fb
            except OSError:
                pass

    if candidate is None:
        return None
    return DiscoveredPlugin(repo_name=name, path=candidate)


def discover_repo_plugins(
    repo_dirs: Sequence[tuple[str, Path] | Path],
) -> tuple[DiscoveredPlugin, ...]:
    """Discover unique in-repo plugins across `repo_dirs`, preserving order."""
    seen: set[Path] = set()
    found: list[DiscoveredPlugin] = []
    for entry in repo_dirs:
        if isinstance(entry, tuple):
            repo_name, repo_dir = entry
        else:
            repo_name, repo_dir = entry.name, entry
        discovered = discover_repo_plugin(repo_dir, repo_name=repo_name)
        if discovered is not None and discovered.path not in seen:
            seen.add(discovered.path)
            found.append(discovered)
    return tuple(found)


def _read_trusted_set(registry: Path) -> set[str]:
    """The trusted locations recorded in `registry`; empty when the file does not exist.

    An unreadable or malformed file is an error, never an empty set: a rewrite from an
    empty set would erase every other entry.
    """
    if not registry.is_file():
        return set()
    try:
        with open(registry, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise UsageError(
            f"cannot read the trust file {registry}: {exc}; fix or remove it (nothing was changed)"
        ) from exc
    items = data.get("trusted", [])
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise UsageError(
            f"the trust file {registry} must hold `trusted = [...]` as a list of paths; "
            "fix or remove it (nothing was changed)"
        )
    return {item for item in items if item}


def _write_trusted_set(registry: Path, paths: set[str]) -> None:
    payload = {"trusted": sorted(paths)}
    atomic_write(
        registry,
        lambda handle: tomli_w.dump(payload, handle),
        mode=0o600,
        suffix=".toml",
    )


def list_trusted() -> tuple[Path, ...]:
    """Return every trusted location (plugin files and directories), sorted."""
    return tuple(Path(p) for p in sorted(_read_trusted_set(trusted_plugins_file())))


def trusted_by(path: Path) -> Path | None:
    """Return the trusted location covering `path`: `path` itself or its nearest trusted ancestor directory."""
    try:
        canonical = path.resolve()
    except OSError:
        return None
    trusted = _read_trusted_set(trusted_plugins_file())
    for candidate in (canonical, *canonical.parents):
        if str(candidate) in trusted:
            return candidate
    return None


def is_trusted(path: Path) -> bool:
    """Whether `path` is trusted: recorded itself, or beneath a trusted directory.

    Trust is by location only: whatever sits at a trusted location, now or later, is trusted.
    """
    return trusted_by(path) is not None


def trust_path(path: Path) -> Path:
    """Record the canonical location of an existing file or directory as trusted.

    A trusted directory covers every plugin beneath it. Raises UsageError if `path` does not exist.
    """
    resolved = path.resolve()
    if not resolved.exists():
        raise UsageError(f"path not found: {resolved}")
    registry = trusted_plugins_file()
    with sidecar_lock(registry):
        trusted = _read_trusted_set(registry)
        trusted.add(str(resolved))
        _write_trusted_set(registry, trusted)
    return resolved


def untrust_path(path: Path) -> tuple[Path, bool]:
    """Remove the canonical location of `path` (file or directory) from the trust registry.

    Returns `(resolved, removed)`. Only the exact recorded location is removed: a file that a
    trusted directory still covers stays trusted (see `trusted_by`).
    """
    resolved = path.resolve()
    registry = trusted_plugins_file()
    with sidecar_lock(registry):
        trusted = _read_trusted_set(registry)
        existed = str(resolved) in trusted
        if existed:
            trusted.discard(str(resolved))
            _write_trusted_set(registry, trusted)
    return resolved, existed
