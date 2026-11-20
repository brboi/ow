"""The "make a repo usable here" flow that `ow repo add` and `ow add` both run."""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

from ow import term
from ow.core import bases, git
from ow.core.git import remotes_of
from ow.core.paths import repos_dir
from ow.core.plan import GitStep
from ow.errors import UsageError
from ow.recipes.removal import branch_is_pushed


def tracking_steps(path: str, branch: str, remote: str) -> tuple[GitStep, GitStep]:
    """The `branch.<b>.remote` and `merge` writes that make `<remote>/<b>` the upstream."""
    return (
        GitStep(("git", "-C", path, "config", f"branch.{branch}.remote", remote)),
        GitStep(("git", "-C", path, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")),
    )


def origin_has(bare: Path, branch: str) -> bool:
    """Whether `origin` in `bare` has `branch`."""
    try:
        return git.remote_has_branch(bare, "origin", branch)
    except git.GitFailed:
        return False


def origin_head(bare: Path) -> str | None:
    """The branch `origin/HEAD` points at in `bare`, or None when it is unset."""
    result = git.run(["git", "-C", str(bare), "symbolic-ref", "refs/remotes/origin/HEAD"])
    if result.returncode != 0:
        return None
    ref = result.stdout.strip()
    prefix = "refs/remotes/origin/"
    if ref.startswith(prefix) and len(ref) > len(prefix):
        return ref[len(prefix):]
    return None


def parse_base_for(value: str, name: str) -> str:
    """`parse_base` a value, with `name`'s bare remotes as the known set."""
    remotes = set(remotes_of(name)) or {"origin"}
    return bases.parse_base(value, lambda segment: segment in remotes)


def resolve_base(
    name: str,
    base: str | None,
    others: Mapping[str, str],
) -> tuple[str, str]:
    """The default base of `name` and the source it came from (`--base` wins)."""
    bare = repos_dir() / f"{name}.git"

    def has(branch: str) -> bool:
        return origin_has(bare, branch)

    if base is not None:
        return parse_base_for(base, name), "--base"
    inferred = bases.infer_default_base(others, has, origin_head(bare))
    if inferred is None:
        raise UsageError(f"no default base for {name}; pass --base")
    return inferred


def base_for(name: str, base: str | None, recorded: str | None, others: Mapping[str, str]) -> str:
    """The base to add `name` at: `--base`, its recorded value, or the inferred default."""
    if base is not None:
        return parse_base_for(base, name)
    if recorded is not None:
        return recorded
    resolved, source = resolve_base(name, None, others)
    term.out(f"# {name}: default base {resolved} ({source})")
    return resolved


def _run_line(argv: Sequence[str], *, live: bool = False) -> str:
    """Print a git line, run it, and return its stdout; a failure raises `GitFailed`."""
    term.out(git.show(argv, None))
    result = git.run(argv, live=live)
    if result.returncode != 0:
        raise git.GitFailed(argv, result.returncode, result.stderr, result.stdout)
    return result.stdout.rstrip("\n")


def _default_branch(output: str) -> str | None:
    """The `ref: refs/heads/<b>` target in `ls-remote --symref` output, or None."""
    prefix = "refs/heads/"
    for line in output.splitlines():
        if not line.startswith("ref: "):
            continue
        ref = line[len("ref: "):].split("\t", 1)[0].strip()
        if ref.startswith(prefix) and len(ref) > len(prefix):
            return ref[len(prefix):]
    return None


def _config(bare: Path, key: str) -> str | None:
    """A bare's config value, or None when the key is unset."""
    result = git.run(["git", "-C", str(bare), "config", "--get", key])
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _fetch_default(bare: Path, name: str, *, live: bool) -> None:
    """Fetch origin's default branch alone, and point `origin/HEAD` at it."""
    output = _run_line(("git", "-C", str(bare), "ls-remote", "--symref", "origin", "HEAD"))
    default = _default_branch(output)
    if default is None:
        raise UsageError(f"{name}: origin has no default branch; check the URL")
    _run_line(
        (
            "git", "-C", str(bare), "fetch", "--filter=blob:none", "origin",
            f"+refs/heads/{default}:refs/remotes/origin/{default}",
        ),
        live=live,
    )
    _run_line(("git", "-C", str(bare), "remote", "set-head", "origin", default))


def ensure_bare(name: str, url: str) -> Path:
    """The bare repo of `name`, created with the recipe when it does not exist yet.

    A failed or interrupted creation removes the bare it made, so a retry starts clean.
    """
    bare = repos_dir() / f"{name}.git"
    if bare.is_dir():
        return bare
    try:
        _run_line(("git", "init", "--bare", str(bare)))
        _run_line(("git", "-C", str(bare), "config", "remote.origin.url", url))
        _run_line(("git", "-C", str(bare), "config", "remote.origin.tagOpt", "--no-tags"))
        _run_line(("git", "-C", str(bare), "config", "core.logAllRefUpdates", "true"))
        _fetch_default(bare, name, live=True)
    except BaseException:
        shutil.rmtree(bare, ignore_errors=True)
        raise
    return bare


def repair_bare(bare: Path) -> None:
    """Repair a bare adopted from ow 2.x: origin's refspec, ref logs, and `origin/HEAD`.

    Only `origin` is touched, and no step fetches every branch.
    """
    has_origin = "origin" in git.remotes(bare)
    if _config(bare, "remote.origin.fetch") == "+refs/heads/*:refs/remotes/origin/*":
        _run_line(("git", "-C", str(bare), "config", "--unset-all", "remote.origin.fetch"))
    if has_origin and _config(bare, "remote.origin.tagOpt") is None:
        _run_line(("git", "-C", str(bare), "config", "remote.origin.tagOpt", "--no-tags"))
    if _config(bare, "core.logAllRefUpdates") != "true":
        _run_line(("git", "-C", str(bare), "config", "core.logAllRefUpdates", "true"))
    if has_origin and origin_head(bare) is None:
        _fetch_default(bare, bare.stem, live=False)


def _has_ref(bare: Path, ref: str) -> bool:
    """Whether `ref` exists in `bare`."""
    argv = ["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", ref]
    return git.run(argv).returncode == 0


def _branch_on_one_remote(bare: Path, branch: str) -> tuple[str | None, bool]:
    """The remote holding `branch` when exactly one does, and whether it is cached."""
    remotes = tuple(remotes_of(bare.stem))
    cached = [remote for remote in remotes if _has_ref(bare, f"refs/remotes/{remote}/{branch}")]
    if len(cached) == 1:
        return cached[0], True
    if len(cached) > 1:
        return None, False
    found: list[str] = []
    for remote in remotes:
        try:
            if git.remote_has_branch(bare, remote, branch):
                found.append(remote)
        except git.GitFailed:
            pass
    if len(found) == 1:
        return found[0], False
    return None, False


def _fetch_refspec(bare: Path, base: str) -> tuple[str, str]:
    """The (remote, branch) a base fetch names: the base's own remote when it has one."""
    if "/" in base:
        head, rest = base.split("/", 1)
        if head in tuple(remotes_of(bare.stem)):
            return head, rest
    return "origin", base.split("/", 1)[-1]


def _stale_registration(bare: Path, target: Path) -> bool:
    """Whether `bare` still registers a worktree at `target` whose directory is gone."""
    result = git.run(["git", "-C", str(bare), "worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        registered = Path(line[len("worktree "):].strip())
        if registered.resolve() == target.resolve() and not registered.exists():
            return True
    return False


_PULL_REF_RE = re.compile(r"^refs/pull/(?P<num>\d+)/head$")


def pr_local_ref(fetch_ref: str, branch: str | None) -> tuple[str, str | None]:
    """Return `(refs/ow/pr/<id>, pr_number_or_None)` for `fetch_ref`."""
    match = _PULL_REF_RE.match(fetch_ref)
    if match:
        num = match.group("num")
        return f"refs/ow/pr/{num}", num
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (branch or fetch_ref)).strip("-") or "pr"
    return f"refs/ow/pr/{slug}", None


def _same_sha(bare: Path, ref_a: str, ref_b: str) -> bool:
    """Whether `ref_a` and `ref_b` both exist in `bare` and resolve to the same commit."""
    res_a = git.run(["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", ref_a])
    if res_a.returncode != 0:
        return False
    res_b = git.run(["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", ref_b])
    if res_b.returncode != 0:
        return False
    return res_a.stdout.strip() == res_b.stdout.strip()


def _checked_out_branches(bare: Path) -> set[str]:
    """Local branch names currently checked out in any worktree of `bare`."""
    res = git.run(["git", "-C", str(bare), "worktree", "list", "--porcelain"])
    if res.returncode != 0:
        return set()
    checked: set[str] = set()
    for line in res.stdout.splitlines():
        if line.startswith("branch refs/heads/"):
            checked.add(line[len("branch refs/heads/"):])
    return checked


def add_worktree(
    bare: Path,
    worktree: Path,
    *,
    branch: str | None,
    base: str,
    fetch_ref: str | None = None,
    remote_hint: str | None = None,
    ow_pr: str | None = None,
    detach: bool = False,
) -> tuple[GitStep, ...]:
    """The steps that add `worktree` to `bare` at `base`, detached or on `branch`.

    When `fetch_ref` is given (e.g. `refs/pull/12345/head`), fetches that ref first
    into `refs/ow/pr/<num>` and creates a worktree from it (detached when `detach=True`
    or `branch is None`, else on a non-colliding local branch with `ow-base` and `ow-pr`).
    """
    target = str(Path(worktree).resolve())
    fetch_remote, base_branch = _fetch_refspec(bare, base)
    steps: list[GitStep] = []
    if _stale_registration(bare, Path(target)):
        steps.append(GitStep(("git", "-C", str(bare), "worktree", "prune")))
    steps.append(
        GitStep(
            (
                "git", "-C", str(bare), "fetch", fetch_remote,
                f"+refs/heads/{base_branch}:refs/remotes/{fetch_remote}/{base_branch}",
            )
        )
    )
    if fetch_ref is not None:
        pr_remote = remote_hint or "origin"
        pr_ref, pr_num = pr_local_ref(fetch_ref, branch)
        steps.append(
            GitStep(
                ("git", "-C", str(bare), "fetch", pr_remote, f"+{fetch_ref}:{pr_ref}")
            )
        )
        if detach or branch is None:
            steps.append(
                GitStep(
                    ("git", "-C", str(bare), "worktree", "add", "--detach", target, pr_ref),
                    live=True,
                )
            )
            return tuple(steps)

        local_branch = branch
        reset_existing = False
        if _has_ref(bare, f"refs/heads/{local_branch}"):
            checked_out = _checked_out_branches(bare)
            same_pr = (
                local_branch not in checked_out
                and (
                    (ow_pr is not None and _config(bare, f"branch.{local_branch}.ow-pr") == ow_pr)
                    or _same_sha(bare, f"refs/heads/{local_branch}", pr_ref)
                )
            )
            if same_pr:
                if not _same_sha(bare, f"refs/heads/{local_branch}", pr_ref) and not branch_is_pushed(
                    bare, local_branch
                ):
                    raise UsageError(
                        f"local branch {local_branch!r} in {bare.stem} has unpushed commits; "
                        "push, rename, or delete it before re-adding this PR"
                    )
                reset_existing = True
            else:
                # Avoid silently checking out a stale branch or colliding with another worktree
                # (e.g. fork PRs opened from `18.0`/`master`/`patch-1`).
                if pr_num is not None:
                    local_branch = f"pr-{pr_num}-{branch}"
                if _has_ref(bare, f"refs/heads/{local_branch}"):
                    if local_branch in checked_out:
                        raise UsageError(
                            f"branch {local_branch!r} is already checked out in another worktree of {bare.stem}"
                        )
                    if not _same_sha(bare, f"refs/heads/{local_branch}", pr_ref) and not branch_is_pushed(
                        bare, local_branch
                    ):
                        raise UsageError(
                            f"local branch {local_branch!r} in {bare.stem} has unpushed commits; "
                            "push, rename, or delete it before re-adding this PR"
                        )
                    reset_existing = True

        if reset_existing:
            steps.append(GitStep(("git", "-C", str(bare), "branch", "-f", local_branch, pr_ref)))
            steps.append(
                GitStep(("git", "-C", str(bare), "worktree", "add", target, local_branch), live=True)
            )
        else:
            steps.append(
                GitStep(
                    (
                        "git", "-C", str(bare), "worktree", "add", "--no-track", "-b", local_branch,
                        target, pr_ref,
                    ),
                    live=True,
                )
            )
        steps.append(
            GitStep(("git", "-C", target, "config", f"branch.{local_branch}.ow-base", base))
        )
        if ow_pr:
            steps.append(
                GitStep(("git", "-C", target, "config", f"branch.{local_branch}.ow-pr", ow_pr))
            )
        return tuple(steps)

    if detach or branch is None:
        if branch is not None and remote_hint is not None:
            if (remote_hint, branch) != (fetch_remote, base_branch):
                steps.append(
                    GitStep(
                        (
                            "git", "-C", str(bare), "fetch", remote_hint,
                            f"+refs/heads/{branch}:refs/remotes/{remote_hint}/{branch}",
                        )
                    )
                )
            start_ref = f"{remote_hint}/{branch}"
        else:
            start_ref = base
        steps.append(
            GitStep(
                ("git", "-C", str(bare), "worktree", "add", "--detach", target, start_ref), live=True
            )
        )
        return tuple(steps)
    if _has_ref(bare, f"refs/heads/{branch}"):
        if remote_hint is not None and (remote_hint, branch) != (fetch_remote, base_branch):
            steps.append(
                GitStep(
                    (
                        "git", "-C", str(bare), "fetch", remote_hint,
                        f"+refs/heads/{branch}:refs/remotes/{remote_hint}/{branch}",
                    )
                )
            )
        steps.append(GitStep(("git", "-C", str(bare), "worktree", "add", target, branch), live=True))
        if remote_hint is not None and _config(bare, f"branch.{branch}.remote") is None:
            steps.extend(tracking_steps(target, branch, remote_hint))
        steps.append(GitStep(("git", "-C", target, "config", f"branch.{branch}.ow-base", base)))
        return tuple(steps)
    if remote_hint is not None:
        if (remote_hint, branch) != (fetch_remote, base_branch):
            steps.append(
                GitStep(
                    (
                        "git", "-C", str(bare), "fetch", remote_hint,
                        f"+refs/heads/{branch}:refs/remotes/{remote_hint}/{branch}",
                    )
                )
            )
        steps.append(
            GitStep(
                (
                    "git", "-C", str(bare), "worktree", "add", "--no-track", "-b", branch, target,
                    f"{remote_hint}/{branch}",
                ),
                live=True,
            )
        )
        steps.extend(tracking_steps(target, branch, remote_hint))
        steps.append(GitStep(("git", "-C", target, "config", f"branch.{branch}.ow-base", base)))
        return tuple(steps)
    remote, cached = _branch_on_one_remote(bare, branch)
    if remote is not None:
        if not cached:
            steps.append(
                GitStep(
                    (
                        "git", "-C", str(bare), "fetch", remote,
                        f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
                    )
                )
            )
        steps.append(
            GitStep(
                (
                    "git", "-C", str(bare), "worktree", "add", "--no-track", "-b", branch, target,
                    f"{remote}/{branch}",
                ),
                live=True,
            )
        )
        steps.extend(tracking_steps(target, branch, remote))
    else:
        steps.append(
            GitStep(
                ("git", "-C", str(bare), "worktree", "add", "--no-track", "-b", branch, target, base),
                live=True,
            )
        )
    steps.append(GitStep(("git", "-C", target, "config", f"branch.{branch}.ow-base", base)))
    return tuple(steps)
