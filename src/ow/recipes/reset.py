"""The reset recipe: the followed ref, what the reset drops, and the dirty files (§7).

`ow reset [--hard]` is `git reset [--hard] <followed ref>`, mixed by default. The plan
carries the commits it will drop and that are on no remote, and — under `--hard` — the
modified paths the reset restores. HEAD already at the followed ref is a no-op, except
under `--hard` with a dirty tree, where the step still runs to restore the files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ow.core import git
from ow.core.facts import RepoFacts
from ow.core.plan import GitStep, RepoPlan
from ow.recipes.follow import followed_ref


@dataclass(frozen=True)
class ResetInputs:
    """Everything the reset plan needs beyond `RepoFacts` (git reads only)."""

    followed: str | None = None     # None when the repo has no ref to follow at all
    resolves: bool = False          # that ref resolves in this worktree
    at_followed: bool = False       # HEAD is already that commit
    dropped: int = 0                # commits the reset drops that no remote has


def gather_reset(facts: RepoFacts) -> ResetInputs:
    """Read the followed ref, whether HEAD is already it, and what the reset would drop."""
    followed = followed_ref(facts)
    if followed is None:
        return ResetInputs()

    resolved = _rev_parse(facts.path, followed)
    if resolved is None:
        return ResetInputs(followed=followed)

    return ResetInputs(
        followed=followed,
        resolves=True,
        at_followed=facts.head is not None and facts.head == resolved,
        dropped=_dropped(facts.path, followed),
    )


def plan_reset(facts: RepoFacts, inputs: ResetInputs, *, hard: bool) -> RepoPlan:
    """The reset step with its notes, or the no-op (pure)."""
    if inputs.followed is None:
        return RepoPlan(facts.name, refusal="no default base; run `ow init`")
    if not inputs.resolves:
        return RepoPlan(
            facts.name,
            refusal=f"{inputs.followed} does not resolve locally; run `ow fetch`",
        )

    notes: list[str] = []
    if inputs.dropped:
        notes.append(f"drops {inputs.dropped} commit(s) on no remote")
    if hard:
        # The tracked paths git would restore; `--hard` moves nothing else.
        notes.extend(f"modified: {path}" for path in facts.modified)

    if inputs.at_followed and not (hard and _dirty(facts)):
        return RepoPlan(facts.name, notes=tuple(notes))

    argv = ["git", "-C", str(facts.path), "reset"]
    if hard:
        argv.append("--hard")
    argv.append(inputs.followed)
    return RepoPlan(facts.name, steps=(GitStep(tuple(argv)),), notes=tuple(notes))


def _dirty(facts: RepoFacts) -> bool:
    """Whether the worktree has anything to restore — tracked changes or untracked files."""
    return bool(facts.modified or facts.untracked)


def _rev_parse(path: Path, ref: str) -> str | None:
    """The commit `ref` resolves to, or None when it does not resolve here."""
    result = git.run(
        ["git", "-C", str(path), "rev-parse", "--verify", "--quiet", ref]
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _dropped(path: Path, followed: str) -> int:
    """How many commits `<followed>..HEAD` holds that no remote-tracking ref reaches."""
    result = git.run(
        [
            "git",
            "-C",
            str(path),
            "rev-list",
            "--count",
            f"{followed}..HEAD",
            "--not",
            "--remotes",
        ]
    )
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return 0