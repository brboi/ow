"""The guarded rebase recipe: read the reflogs, then plan purely.

A shared branch takes a colleague's commits in without rewriting them: by
`cherry-pick --ff` when the branch has no local commits (a fast-forward), else
by replaying the local commits on the upstream with `--fork-point`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ow.core import git
from ow.core.facts import RepoFacts
from ow.core.plan import GitStep, RepoPlan, Step

AUTOSTASH_MESSAGE = "ow rebase --autostash"


@dataclass(frozen=True)
class RebaseInputs:
    """What the rebase decision needs beyond `RepoFacts` (git reads only)."""

    # The upstream is an ancestor of some value the branch had: nothing to take in.
    upstream_in_branch_past: bool = False
    # The newest upstream reflog entry whose patches are all in HEAD.
    last_taken: str | None = None
    # That entry is an ancestor of the current upstream (a fast-forward, not a rewrite).
    last_taken_fast_forward: bool = False
    # Upstream commits whose patch is not already in HEAD.
    new_upstream_commits: int = 0
    # The base is already an ancestor of HEAD, so a plain rebase would be a no-op.
    base_is_ancestor: bool = True
    # The base is an ancestor of the upstream, so the upstream step lands it too.
    base_in_upstream: bool = False
    # `last_taken..upstream` holds a merge, which a cherry-pick cannot replay.
    has_merge_in_range: bool = False
    # HEAD holds commits of its own: neither on the base nor already in the upstream.
    has_local_commits: bool = False
    # HEAD already contains the new base, so only the config write is left.
    new_base_is_ancestor: bool = False
    # Detached HEAD with no commits above `facts.base` (e.g. Runbot-pinned or clean detached).
    detached_clean_base: bool = False
    # Detached HEAD already sits on `new_base`'s exact commit.
    head_equals_new_base: bool = False


def gather_rebase(facts: RepoFacts, *, new_base: str | None = None) -> RebaseInputs:
    """Read the reflogs and the ancestry a plan for `facts` needs.

    HEAD is prepended to the branch reflog: a reflog can be missing.
    """
    path = facts.path
    upstream = _upstream(facts, new_base)

    branch_values: tuple[str, ...] = ()
    if facts.branch is not None:
        values = _reflog(path, f"refs/heads/{facts.branch}")
        if facts.head is not None and facts.head not in values:
            values = (facts.head, *values)
        branch_values = values

    upstream_in_branch_past = False
    last_taken: str | None = None
    last_taken_fast_forward = False
    new_upstream_commits = 0
    has_merge_in_range = False
    has_local_commits = False

    if upstream is not None:
        upstream_values = _reflog(path, f"refs/remotes/{upstream}")
        upstream_in_branch_past = any(
            _is_ancestor(path, upstream, value) for value in branch_values
        )
        # The newest upstream value whose patches are all in HEAD: a previous
        # fast-forward left copies, not ancestors, so ancestry alone would stall.
        for value in upstream_values:
            if _unpatched(path, value) == 0:
                last_taken = value
                break
        if last_taken is not None:
            last_taken_fast_forward = _is_ancestor(path, last_taken, upstream)
            if last_taken_fast_forward:
                has_merge_in_range = _has_merge(path, f"{last_taken}..{upstream}")
        if last_taken is not None and last_taken_fast_forward and not has_merge_in_range:
            new_upstream_commits = _unpatched(path, upstream) or 0
            if new_upstream_commits > 0:
                has_local_commits = _local_commits(path, upstream, facts.base) > 0

    base_is_ancestor = True
    if facts.base is not None:
        base_is_ancestor = _is_ancestor(path, facts.base, "HEAD")

    base_in_upstream = (
        facts.base is not None
        and upstream is not None
        and _is_ancestor(path, facts.base, upstream)
    )

    new_base_is_ancestor = (
        new_base is not None
        and _is_ancestor(path, new_base, "HEAD")
        and (
            facts.base is None
            or not base_is_ancestor
            or _is_ancestor(path, facts.base, new_base)
        )
    )

    detached_clean_base = False
    head_equals_new_base = False
    if facts.branch is None and new_base is not None:
        if facts.base is not None:
            detached_clean_base = _is_ancestor(path, "HEAD", facts.base)
        elif facts.head is not None:
            from ow.recipes.removal import orphan_commits

            detached_clean_base = orphan_commits(facts.bare, facts.head) == 0
        head_sha = git.read(["git", "-C", str(path), "rev-parse", "--verify", "HEAD"], check=False)
        target_sha = git.read(["git", "-C", str(path), "rev-parse", "--verify", new_base], check=False)
        head_equals_new_base = bool(head_sha and target_sha and head_sha == target_sha)

    return RebaseInputs(
        upstream_in_branch_past=upstream_in_branch_past,
        last_taken=last_taken,
        last_taken_fast_forward=last_taken_fast_forward,
        new_upstream_commits=new_upstream_commits,
        base_is_ancestor=base_is_ancestor,
        base_in_upstream=base_in_upstream,
        has_merge_in_range=has_merge_in_range,
        has_local_commits=has_local_commits,
        new_base_is_ancestor=new_base_is_ancestor,
        detached_clean_base=detached_clean_base,
        head_equals_new_base=head_equals_new_base,
    )


def plan_rebase(
    facts: RepoFacts,
    inputs: RebaseInputs,
    *,
    new_base: str | None,
    autostash: bool,
) -> RepoPlan:
    """Turn facts and inputs into the exact steps to run (pure).

    When the base is an ancestor of the upstream, the upstream step lands it too,
    so the base step is left out: rebasing onto the base afterwards would replay
    the upstream's own commits as new copies, the rewrite the guard prevents.

    A new base always runs `--onto`: the branch already holds its old base,
    and once it holds the new one only the config write is left.
    """
    path = str(facts.path)
    steps: list[GitStep] = []

    upstream = _upstream(facts, new_base)
    if facts.branch is not None and upstream is not None:
        if not inputs.upstream_in_branch_past:
            if (
                inputs.last_taken is not None
                and inputs.last_taken_fast_forward
                and not inputs.has_merge_in_range
                and not inputs.has_local_commits
            ):
                if inputs.new_upstream_commits > 0:
                    # `--ff` fast-forwards when the branch has no local commits,
                    # so it lands on the upstream exactly instead of copying it.
                    steps.append(
                        GitStep(
                            (
                                "git",
                                "-C",
                                path,
                                "cherry-pick",
                                "--ff",
                                "--empty=drop",
                                f"{inputs.last_taken}..{upstream}",
                            ),
                            comment=(
                                f"{inputs.last_taken[:10]} = {upstream}"
                                " as this branch last took it in"
                            ),
                        )
                    )
            else:
                steps.append(
                    GitStep(("git", "-C", path, "rebase", "--fork-point", upstream))
                )

    # Only the upstream step can be in `steps` yet, and it lands the upstream:
    # with the base an ancestor of it, the branch holds the base by then.
    base_is_taken_in = bool(steps) and inputs.base_in_upstream
    needs_base_step = not inputs.base_is_ancestor and not base_is_taken_in

    changing_base = new_base is not None and new_base != facts.base
    if changing_base:
        if facts.branch is None and inputs.detached_clean_base:
            if not inputs.head_equals_new_base:
                steps.append(GitStep(("git", "-C", path, "switch", "--detach", new_base)))
        elif not inputs.new_base_is_ancestor:
            if facts.base is None:
                # Nothing recorded to bound the replay: the native rebase picks
                # the merge-base with the new base.
                steps.append(GitStep(("git", "-C", path, "rebase", new_base)))
            else:
                steps.append(
                    GitStep(("git", "-C", path, "rebase", "--onto", new_base, facts.base))
                )
        if facts.branch is not None:
            steps.append(
                GitStep(
                    (
                        "git",
                        "-C",
                        path,
                        "config",
                        f"branch.{facts.branch}.ow-base",
                        new_base,
                    )
                )
            )
    elif facts.base is not None and needs_base_step:
        steps.append(GitStep(("git", "-C", path, "rebase", facts.base)))

    if not steps:
        return RepoPlan(facts.name)

    if autostash and facts.modified:
        # The stash does not move the branch, so it must not be the successful
        # step that triggers the post-batch refresh on a failed rebase.
        steps.insert(
            0,
            GitStep(
                ("git", "-C", path, "stash", "push", "-m", AUTOSTASH_MESSAGE),
                mutating=False,
            ),
        )
        steps.append(GitStep(("git", "-C", path, "stash", "pop"), mutating=False))

    return RepoPlan(facts.name, steps=tuple(steps))


def plan_is_fast_forward(facts: RepoFacts, plan: RepoPlan) -> bool:
    """Whether every step only moves the branch to a commit that already holds HEAD.

    A plain `rebase` replays local commits, so the prompt still guards it.
    """
    current = "HEAD"
    for step in plan.steps:
        destination = _destination(step)
        if destination is not None:
            if not _is_ancestor(facts.path, current, destination):
                return False
            current = destination
    return True


def _destination(step: Step) -> str | None:
    """The commit a plan step moves the branch to; None for a step that does not."""
    if not isinstance(step, GitStep):
        return None
    argv = step.argv
    if len(argv) < 4:
        return None
    if argv[3] == "cherry-pick":
        return argv[-1].split("..", 1)[-1]
    if argv[3] == "rebase":
        if "--onto" in argv:
            return argv[argv.index("--onto") + 1]
        return argv[-1]
    if argv[3] == "switch":
        return argv[-1]
    return None


def _upstream(facts: RepoFacts, new_base: str | None) -> str | None:
    """The upstream that gets its own step: none when it is the base or the new base."""
    if facts.upstream is None or facts.upstream in (facts.base, new_base):
        return None
    return facts.upstream


def _reflog(path: Path, ref: str) -> tuple[str, ...]:
    """A ref's reflog SHAs, newest first; empty when the ref or its log is missing."""
    result = git.run(["git", "-C", str(path), "reflog", "show", "--format=%H", ref])
    if result.returncode != 0:
        return ()
    return tuple(line for line in result.stdout.splitlines() if line)


def _is_ancestor(path: Path, ancestor: str, descendant: str) -> bool:
    """`git merge-base --is-ancestor`; any failure (a missing ref) counts as no."""
    result = git.run(
        ["git", "-C", str(path), "merge-base", "--is-ancestor", ancestor, descendant]
    )
    return result.returncode == 0


def _unpatched(path: Path, upstream: str) -> int | None:
    """Commits of `HEAD...upstream` with no patch-equivalent in HEAD; None on error."""
    result = git.run(
        [
            "git",
            "-C",
            str(path),
            "rev-list",
            "--cherry-pick",
            "--right-only",
            "--count",
            f"HEAD...{upstream}",
        ]
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip() or "0")
    except ValueError:
        return None


def _local_commits(path: Path, upstream: str, base: str | None) -> int:
    """HEAD's commits that are neither reachable from `base` nor patch-equivalent to the upstream's.

    A failure counts as local commits: the caller then replays instead of fast-forwarding.
    """
    argv = ["git", "-C", str(path), "rev-list", "--cherry-pick", "--left-only", "--count"]
    argv.append(f"HEAD...{upstream}")
    if base is not None:
        argv.append(f"^{base}")
    result = git.run(argv)
    try:
        return int(result.stdout.strip()) if result.returncode == 0 else 1
    except ValueError:
        return 1


def _has_merge(path: Path, range_: str) -> bool:
    """Whether a commit range contains a merge commit."""
    result = git.run(["git", "-C", str(path), "rev-list", "--merges", range_])
    if result.returncode != 0:
        return False
    return bool(result.stdout.strip())
