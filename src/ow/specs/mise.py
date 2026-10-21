"""mise environment specification and merging."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Sequence

from ow.core.plugins import hookspec

if TYPE_CHECKING:
    from ow.core.workspace import Workspace


@dataclass(frozen=True)
class MiseEnvironment:
    """Environment contribution from mise."""

    env: Mapping[str, str] = field(default_factory=dict)
    path: tuple[str, ...] = ()
    python: tuple[tuple[int, int], tuple[int, int]] | None = None  # supported (min, max)
    postinstall: tuple[str, ...] = ()
    aliases: Mapping[str, str] = field(default_factory=dict)
    tasks: Mapping[str, Mapping[str, object]] = field(default_factory=dict)


class MiseSpec:
    """Hookspec for mise contributions."""

    @hookspec
    def mise_environment(self, ws: Workspace) -> MiseEnvironment | None:
        """Return mise environment contribution or None."""


def merge(answers: Sequence[MiseEnvironment]) -> MiseEnvironment | None:
    """Merge mise environment answers in tuple order.

    - path and postinstall are concatenated
    - env, aliases, and tasks: later answer wins per key
    - python: later non-None wins (P14)
    - Returns None when there are no answers
    """
    if not answers:
        return None

    merged_env = {}
    merged_path = []
    merged_postinstall = []
    merged_aliases = {}
    merged_tasks = {}
    merged_python = None

    for answer in answers:
        if answer is None:
            continue

        # Concatenate path and postinstall
        if answer.path:
            merged_path.extend(answer.path)
        if answer.postinstall:
            merged_postinstall.extend(answer.postinstall)

        # Later wins for env, aliases, tasks
        if answer.env:
            merged_env.update(answer.env)
        if answer.aliases:
            merged_aliases.update(answer.aliases)
        if answer.tasks:
            merged_tasks.update(answer.tasks)

        # Later non-None wins for python
        if answer.python is not None:
            merged_python = answer.python

    return MiseEnvironment(
        env=merged_env if merged_env else {},
        path=tuple(merged_path) if merged_path else (),
        python=merged_python,
        postinstall=tuple(merged_postinstall) if merged_postinstall else (),
        aliases=merged_aliases if merged_aliases else {},
        tasks=merged_tasks if merged_tasks else {},
    )
