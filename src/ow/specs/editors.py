"""Editor contributions specification and merging."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from ow.core.plugins import hookspec

if TYPE_CHECKING:
    from ow.core.workspace import Workspace


@dataclass(frozen=True)
class DebugLaunch:
    """A debug launch configuration."""

    name: str
    program: Path  # absolute
    args: tuple[str, ...]
    cwd: Path  # absolute
    env: Mapping[str, str]


@dataclass(frozen=True)
class EditorContribution:
    """Editor contribution with launches, language servers, and excludes."""

    launches: tuple[DebugLaunch, ...] = ()
    language_servers: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    excludes: tuple[str, ...] = ()


class EditorsSpec:
    """Hookspec for editor contributions."""

    @hookspec
    def editors_contribution(self, ws: Workspace) -> EditorContribution | None:
        """Return editor contribution or None."""


def merge(answers: Sequence[EditorContribution]) -> EditorContribution:
    """Merge editor contributions in tuple order.

    - launches: concatenated
    - excludes: concatenated and deduplicated
    - language_servers: merged per server id and per key, later answer winning
    - Empty input returns EditorContribution()
    """
    merged_launches = []
    merged_excludes = []
    merged_language_servers = {}

    for answer in answers:
        if answer is None:
            continue

        # Concatenate launches
        if answer.launches:
            merged_launches.extend(answer.launches)

        # Concatenate excludes (deduplicate by converting to set and back)
        if answer.excludes:
            merged_excludes.extend(answer.excludes)

        # Merge language servers: per server id, per key, later wins
        if answer.language_servers:
            for server_id, settings in answer.language_servers.items():
                if server_id not in merged_language_servers:
                    merged_language_servers[server_id] = {}
                if isinstance(settings, Mapping):
                    merged_language_servers[server_id].update(settings)

    # Deduplicate excludes while preserving order
    seen = set()
    deduped_excludes = []
    for exclude in merged_excludes:
        if exclude not in seen:
            seen.add(exclude)
            deduped_excludes.append(exclude)

    return EditorContribution(
        launches=tuple(merged_launches),
        language_servers=merged_language_servers if merged_language_servers else {},
        excludes=tuple(deduped_excludes),
    )
