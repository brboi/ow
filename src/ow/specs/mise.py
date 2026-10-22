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
    - python: later non-None wins
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
        env=merged_env,
        path=tuple(merged_path),
        python=merged_python,
        postinstall=tuple(merged_postinstall),
        aliases=merged_aliases,
        tasks=merged_tasks,
    )
