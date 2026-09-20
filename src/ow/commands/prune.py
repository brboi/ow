import sys
from pathlib import Path
from typing import NamedTuple

from ow.utils.display import confirm, err_console
from ow.utils import index, paths
from ow.utils.config import load_workspace_config
from ow.utils.git import _run, get_worktree_branch, is_branch_pushed, parallel_per_repo


class _PrunePlan(NamedTuple):
    """What prune would do to one bare repo. Observation only, nothing applied.

    Deciding first and acting second is what makes --dry-run truthful and
    the confirmation worth answering: both show the very list that the
    apply step then works from, rather than a guess at it.
    """

    alias: str
    repo: Path
    stale_worktrees: list[str]
    to_delete: list[str]
    kept: list[str]

    @property
    def is_empty(self) -> bool:
        return not (self.stale_worktrees or self.to_delete or self.kept)

    @property
    def commands(self) -> list[list[str]]:
        argv: list[list[str]] = []
        if self.stale_worktrees:
            argv.append(["worktree", "prune"])
        argv.extend(["branch", "-D", branch] for branch in self.to_delete)
        return argv


class _PruneOutcome(NamedTuple):
    alias: str
    deleted: list[str]
    failed: list[tuple[str, str]]


def _survey_worktrees(bare_repo: Path) -> tuple[set[str], list[str]]:
    """One porcelain listing, read before pruning: (branches in use, stale paths).

    Before, not after: `git worktree prune` writes nothing to stdout without
    --verbose (and writes to stderr even with it), so asking it what it
    removed never got an answer. The listing marks a registered worktree
    whose directory has gone as `prunable`, which is the same judgement
    prune is about to act on — so read it while the evidence is still there.

    A prunable worktree's branch is deliberately not counted as in use: it
    is about to stop being attached to anything, and the branch pass has to
    see the repo as it will be, not as it was. What keeps that from eating a
    live workspace's branch is _declared_branches(), not this listing.
    """
    result = _run(
        ["git", "-C", str(bare_repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    used: set[str] = set()
    stale: list[str] = []
    if result.returncode != 0:
        # Not "nothing is in use": that reading turns every branch in the
        # repo into an orphan candidate, which is the one direction this
        # command must never fail in.
        raise RuntimeError(
            f"git worktree list failed: {result.stderr.strip() or 'no output'}"
        )

    path: str | None = None
    branch: str | None = None
    prunable = False

    def close_block() -> None:
        nonlocal path, branch, prunable
        if path is not None:
            if prunable:
                stale.append(path)
            elif branch is not None:
                used.add(branch)
        path, branch, prunable = None, None, False

    for line in result.stdout.splitlines():
        if not line.strip():
            close_block()
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            close_block()
            path = value
        elif key == "branch" and value.startswith("refs/heads/"):
            branch = value[len("refs/heads/"):]
        elif key == "prunable":
            prunable = True
    close_block()

    return used, stale


def _declared_branches() -> tuple[dict[str, set[str]], list[tuple[Path, str]]]:
    """Per alias, the branches live workspaces own. Plus the configs that would not read.

    git's worktree bookkeeping is not the only truth. A registration that
    went stale — a directory moved by hand, a mount that was away when the
    listing ran — makes git call the worktree prunable and its branch
    unattached, while the workspace is still there and still says in its own
    .ow/config.toml which branch it works on. Issue #50: prune offered to
    delete exactly such a branch, and git refused it as in use.

    Both the declared branch and the branch actually checked out are taken:
    a worktree moved to a branch the config does not name is still work.
    """
    declared: dict[str, set[str]] = {}
    unreadable: list[tuple[Path, str]] = []

    # known_workspaces, not list_workspaces: a dead entry has no branches to
    # protect, and the read filters those out without writing anything. The
    # config check below still covers a marker that vanished between the two
    # reads — this loop may report a workspace it cannot read, never guess.
    for ws_dir in index.known_workspaces():
        try:
            config_file = ws_dir / ".ow" / "config.toml"
            if not config_file.is_file():
                # A dead index entry, not a workspace whose config is broken.
                continue
            ws = load_workspace_config(config_file)
            owned_here: dict[str, set[str]] = {}
            for alias, spec in ws.repos.items():
                owned = owned_here.setdefault(alias, set())
                if spec.local_branch:
                    # A detached spec owns no branch.
                    owned.add(spec.local_branch)
                if (ws_dir / alias).is_dir():
                    checked_out = get_worktree_branch(ws_dir / alias)
                    if checked_out:
                        owned.add(checked_out)
        except Exception as exc:
            # Unparseable, unreadable, on a directory that will not even
            # stat: all the same answer — this workspace's branches cannot
            # be named, so none of them can be shown to be orphaned.
            unreadable.append((ws_dir, str(exc)))
            continue

        for alias, owned in owned_here.items():
            declared.setdefault(alias, set()).update(owned)

    return declared, unreadable


def _survey_bare_repo(bare_repo: Path, protected: frozenset[str] = frozenset()) -> _PrunePlan:
    """Work out what would go from one bare repo, without touching it."""
    used_branches, stale = _survey_worktrees(bare_repo)

    to_delete: list[str] = []
    kept: list[str] = []

    # for-each-ref, not `branch --list`: the latter is a porcelain command and
    # honours color.ui=always, which paints every name with escape codes that
    # no prefix-stripping removes. A branch name is an identifier we hand back
    # to git, not a string to display.
    branch_result = _run(
        ["git", "-C", str(bare_repo), "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        capture_output=True, text=True,
    )
    head_result = _run(
        ["git", "-C", str(bare_repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    head_branch = head_result.stdout.strip() if head_result.returncode == 0 else None
    if head_branch == "HEAD":
        head_branch = None

    if branch_result.returncode == 0:
        all_branches = {b.strip() for b in branch_result.stdout.splitlines() if b.strip()}
        for branch in sorted(all_branches - used_branches - protected):
            if branch == head_branch:
                # Never delete the branch HEAD points at — a dangling HEAD
                # confuses every subsequent git command and looks like data loss.
                continue
            # "Orphaned" describes the worktree that is gone, not the work
            # that may still be on the branch. Only the former is ours to
            # throw away.
            target = to_delete if is_branch_pushed(bare_repo, branch) else kept
            target.append(branch)

    return _PrunePlan(
        alias=bare_repo.stem, repo=bare_repo,
        stale_worktrees=stale, to_delete=to_delete, kept=kept,
    )


def _apply(plan: _PrunePlan) -> _PruneOutcome:
    """Run exactly what the plan showed, and report exactly what took."""
    deleted: list[str] = []
    failed: list[tuple[str, str]] = []

    if plan.stale_worktrees:
        _run(
            ["git", "-C", str(plan.repo), "worktree", "prune"],
            capture_output=True, text=True,
        )

    for branch in plan.to_delete:
        # Only a delete git confirmed. Claiming a refusal as a deletion
        # sends the user looking elsewhere for work that is still here.
        result = _run(
            ["git", "-C", str(plan.repo), "branch", "-D", branch],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            deleted.append(branch)
        else:
            reason = result.stderr.strip().splitlines()[0] if result.stderr else "git refused"
            failed.append((branch, reason))

    return _PruneOutcome(alias=plan.alias, deleted=deleted, failed=failed)


def _dead_index_entries() -> list[Path]:
    """Every index line naming a workspace that is gone. Reads only.

    known_workspaces() filters those out of what it returns and nothing
    rewrites the file as a side effect any more, so dropping them for good
    is this command's explicit job — and the reason the list of paths is
    returned, not just its length.

    Only a path whose .ow/config.toml is gone counts. A duplicate raw line
    is internal hygiene from a read-modify-write race in remember() (two
    concurrent writers), not something the user caused or can act on, so it
    is neither reported nor counted as a death.
    """
    index_file = paths.index_file()
    dead: list[Path] = []
    if index_file.exists():
        seen: set[Path] = set()
        for line in index_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            candidate = Path(line)
            if candidate in seen:
                continue
            seen.add(candidate)
            if not index._still_there(candidate):
                dead.append(candidate)

    return dead


def _index_line(dropped: int, verb: str) -> str:
    noun = "entry" if dropped == 1 else "entries"
    return f"{verb} {dropped} dead index {noun}."


def _stale_backups() -> list[Path]:
    """Every backup file on disk, newest last. Empty list when there are none.

    `--also-backups` lists every backup — no age heuristic to explain, and
    the confirmation prompt already makes the list visible before anything
    is deleted. The glob is non-recursive on purpose: `backups/migrations/`
    holds migration backups keyed by content hash, not rm backups keyed by
    workspace name, and they are never this operation's to delete.
    """
    try:
        return sorted(paths.backups_dir().glob("*.toml"))
    except OSError:
        return []


def _display_backups(backups: list[Path]) -> None:
    if not backups:
        return
    print()
    noun = "backup" if len(backups) == 1 else "backups"
    print(f"Backups ({len(backups)} {noun}):")
    for p in backups:
        print(f"  {p.name}")


def _display_plan(plans: list[_PrunePlan]) -> None:
    """The whole report, in the imperative: nothing here has happened yet."""
    for plan in plans:
        if plan.stale_worktrees:
            noun = "worktree" if len(plan.stale_worktrees) == 1 else "worktrees"
            print(
                f"  [{plan.alias}] prune {len(plan.stale_worktrees)} stale {noun}: "
                f"{', '.join(plan.stale_worktrees)}"
            )
        if plan.to_delete:
            noun = "branch" if len(plan.to_delete) == 1 else "branches"
            print(
                f"  [{plan.alias}] delete {len(plan.to_delete)} orphaned {noun}: "
                f"{', '.join(plan.to_delete)}"
            )
        if plan.kept:
            noun = "branch" if len(plan.kept) == 1 else "branches"
            print(
                f"  [{plan.alias}] keep {len(plan.kept)} {noun} with unpushed commits: "
                f"{', '.join(plan.kept)}"
            )

    if any(plan.kept for plan in plans):
        print("\nKept branches hold commits no remote has. Push them, or delete one by hand:")
        for plan in plans:
            for branch in plan.kept:
                print(f"  git -C {plan.repo} branch -D {branch}")


def _display_dry_run(plans: list[_PrunePlan]) -> None:
    print("\nWould run:")
    for plan in plans:
        for argv in plan.commands:
            print(f"  [{plan.alias}] git {' '.join(argv)}")


def cmd_prune(*, dry_run: bool = False, yes: bool = False, also_backups: bool = False) -> None:
    """Clean up stale worktree references, orphaned branches, and dead index entries.

    Survey first, then act. --dry-run stops after the survey; otherwise the
    branch deletions — the only step that can lose work — are confirmed
    first, defaulting to no, exactly as `ow rebase` does. Answering no
    leaves everything untouched, the index included.
    """
    dropped = _dead_index_entries()
    declared, unreadable = _declared_branches()
    bare_repos_dir = paths.repos_dir()
    # Not every *.git under there is an alias: a stray `.git` directory
    # matches the pattern too, and surveying it fails as "not a repository".
    bare_repos = sorted(
        p for p in bare_repos_dir.glob("*.git") if not p.name.startswith(".")
    ) if bare_repos_dir.exists() else []
    if not bare_repos:
        print("No bare repos found.")

    surveyed = parallel_per_repo({
        repo.stem: (lambda r=repo: _survey_bare_repo(r, frozenset(declared.get(r.stem, ()))))
        for repo in bare_repos
    })
    plans: list[_PrunePlan] = []
    survey_errors: dict[str, Exception] = {}
    for repo in bare_repos:
        result = surveyed.get(repo.stem)
        if isinstance(result, Exception):
            survey_errors[repo.stem] = result
        else:
            plans.append(result)

    sys.stdout.flush()
    for alias, exc in survey_errors.items():
        err_console.print(f"  [{alias}] survey failed: {exc}", markup=False)

    for ws_dir, reason in unreadable:
        err_console.print(f"  [{ws_dir.name}] unreadable .ow/config.toml: {reason}", markup=False)
    if unreadable:
        # Which branches that workspace owns is exactly what could not be
        # read, so no branch can be shown to be orphaned. The reversible
        # work — stale worktrees, the index, backups — still happens.
        err_console.print(
            "  No branch will be deleted this run: ow cannot tell which branches that workspace owns."
        )
        plans = [
            plan._replace(to_delete=[], kept=[*plan.kept, *plan.to_delete])
            for plan in plans
        ]

    _display_plan(plans)
    backups = _stale_backups() if also_backups else []
    _display_backups(backups)
    if bare_repos and not survey_errors and all(plan.is_empty for plan in plans):
        print("All bare repos are clean.")

    if dry_run:
        if any(plan.commands for plan in plans):
            _display_dry_run(plans)
        if backups:
            print()
            print("Would delete:")
            for p in backups:
                print(f"  rm {p}")
        if dropped:
            print(_index_line(len(dropped), "Would drop"))
        return

    if (
        (any(plan.to_delete for plan in plans) or backups)
        and not yes and not confirm()
    ):
        print("Aborted.")
        sys.exit(2)

    sys.stdout.flush()
    outcomes = parallel_per_repo({
        plan.alias: (lambda p=plan: _apply(p))
        for plan in plans
        if plan.commands
    })
    acted = False
    failed = bool(survey_errors)
    for plan in plans:
        outcome = outcomes.get(plan.alias)
        if isinstance(outcome, Exception):
            err_console.print(
                f"  [{plan.alias}] apply failed: {outcome}", markup=False,
            )
            failed = True
            continue
        if outcome is None:
            continue
        acted = acted or bool(outcome.deleted) or bool(plan.stale_worktrees)
        if outcome.failed:
            # git's refusals are the user's problem to act on, so they belong
            # on stderr where a pipeline can still see them.
            for branch, reason in outcome.failed:
                err_console.print(
                    f"  [{plan.alias}] could not delete {branch}: {reason}",
                    markup=False,
                )
            failed = True

    if backups:
        deleted = 0
        for p in backups:
            try:
                p.unlink()
                deleted += 1
            except OSError as exc:
                err_console.print(f"  could not delete {p.name}: {exc}", markup=False)
                failed = True
        if deleted:
            noun = "backup" if deleted == 1 else "backups"
            print(f"Deleted {deleted} backup {noun}.")

    if dropped:
        index.prune(dropped)
        print(_index_line(len(dropped), "Dropped"))

    if acted:
        # The plan above is written in the imperative. Without this, silence
        print("Done.")

    if failed:
        sys.exit(1)
