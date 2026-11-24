"""Editor contributions specification and merging."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ow.core.plugins import hookspec

if TYPE_CHECKING:
    from ow.core.plugins import Answers
    from ow.core.workspace import Workspace


@dataclass(frozen=True, kw_only=True)
class DebugLaunch:
    """A debug launch configuration."""

    name: str
    program: Path  # absolute
    args: tuple[str, ...]
    cwd: Path  # absolute
    env: Mapping[str, str]


@dataclass(frozen=True, kw_only=True)
class EditorContribution:
    """Editor contribution with launches, language servers, excludes, and optional Python interpreter."""

    launches: tuple[DebugLaunch, ...] = ()
    language_servers: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    excludes: tuple[str, ...] = ()
    python: Path | None = None


class EditorsSpec:
    """Hookspec for editor contributions."""

    @hookspec
    def editors_contribution(self, ws: Workspace, answers: Answers) -> EditorContribution | None:
        """Return editor contribution or None."""


def merge(answers: Sequence[EditorContribution]) -> EditorContribution:
    """Merge editor contributions in tuple order.

    - launches: concatenated
    - excludes: concatenated and deduplicated in order
    - language_servers: merged per server id and per key, later answer winning
    - python: later non-None wins
    - Empty input returns EditorContribution()
    """
    merged_launches = []
    merged_excludes = []
    merged_language_servers = {}
    merged_python: Path | None = None

    for answer in answers:
        # Concatenate launches
        if answer.launches:
            merged_launches.extend(answer.launches)

        # Concatenate excludes (deduplicate later)
        if answer.excludes:
            merged_excludes.extend(answer.excludes)

        # Merge language servers: per server id, per key, later wins
        if answer.language_servers:
            for server_id, settings in answer.language_servers.items():
                if not isinstance(settings, Mapping):
                    raise ValueError(
                        f"language server {server_id!r} settings must be a mapping, not {type(settings).__name__}"
                    )
                if server_id not in merged_language_servers:
                    merged_language_servers[server_id] = {}
                merged_language_servers[server_id].update(settings)

        if answer.python is not None:
            merged_python = answer.python

    # Deduplicate excludes while preserving order
    seen = set()
    deduped_excludes = []
    for exclude in merged_excludes:
        if exclude not in seen:
            seen.add(exclude)
            deduped_excludes.append(exclude)

    return EditorContribution(
        launches=tuple(merged_launches),
        language_servers=merged_language_servers,
        excludes=tuple(deduped_excludes),
        python=merged_python,
    )


BASE_EXCLUDES: tuple[str, ...] = (
    "**/.git",
    "**/.svn",
    "**/.hg",
    "**/.jj",
    "**/CVS",
    "**/.DS_Store",
    "**/Thumbs.db",
    "**/.classpath",
    "**/.settings",
    "**/.venv",
)


def default_python(ws: Workspace) -> Path:
    """The workspace venv interpreter, used when no contribution names one."""
    return ws.root / ".venv/bin/python"


def debugpy_fields(launch: DebugLaunch) -> dict[str, object]:
    """The debugpy launch keys shared by every editor, in on-disk order."""
    return {
        "program": str(launch.program),
        "args": list(launch.args),
        "cwd": str(launch.cwd),
        "justMyCode": False,
        "pythonArgs": ["-Xfrozen_modules=off"],
        "env": dict(launch.env),
    }
