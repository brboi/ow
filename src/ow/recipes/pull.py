"""The pull recipe: only fast-forward when the ref is strictly ahead."""

from __future__ import annotations

from dataclasses import dataclass

from ow.core import git
from ow.core.facts import RepoFacts
from ow.core.plan import GitStep, RepoPlan
from ow.recipes.follow import followed_ref


@dataclass(frozen=True)
class PullInputs:
    """How HEAD stands against the followed ref (git reads only)."""

    followed: str | None = None   # None when the repo has no ref to follow at all
    resolves: bool = False        # the ref resolves in this worktree
    ahead: int = 0                # commits HEAD has that the ref does not
    behind: int = 0               # commits the ref has that HEAD does not


def gather_pull(facts: RepoFacts) -> PullInputs:
    """Read the followed ref and the `rev-list --left-right --count` pair."""
    followed = followed_ref(facts)
    if followed is None:
        return PullInputs()

    result = git.run(
        [
            "git",
            "-C",
            str(facts.path),
            "rev-list",
            "--left-right",
            "--count",
            f"{followed}...HEAD",
        ]
    )
    if result.returncode != 0:
        return PullInputs(followed=followed)

    parts = result.stdout.split()
    try:
        # `--left-right` prints the left side (the ref's own commits) first: behind, ahead.
        behind, ahead = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return PullInputs(followed=followed)

    return PullInputs(followed=followed, resolves=True, ahead=ahead, behind=behind)


def plan_pull(facts: RepoFacts, inputs: PullInputs) -> RepoPlan:
    """The fast-forward step, a divergence refusal, or nothing at all (pure)."""
    if not inputs.resolves:
        return RepoPlan(
            facts.name,
            refusal=f"{inputs.followed} does not resolve locally; run `ow fetch`",
        )
    if inputs.ahead and inputs.behind:
        return RepoPlan(
            facts.name,
            refusal=f"diverged from {inputs.followed}; run `ow rebase`",
        )
    if inputs.ahead:
        # HEAD already contains the ref: an ff-only merge would do nothing.
        return RepoPlan(facts.name)
    if inputs.behind:
        return RepoPlan(
            facts.name,
            steps=(
                GitStep(
                    (
                        "git",
                        "-C",
                        str(facts.path),
                        "merge",
                        "--ff-only",
                        inputs.followed,
                    )
                ),
            ),
        )
    return RepoPlan(facts.name)