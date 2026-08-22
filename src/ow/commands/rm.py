"""`ow rm` — remove a workspace and clean up its worktrees, branches, and index entry.

Takes a workspace name known to `ow ls`. Shows a summary of what will be removed
— including unpushed commits and uncommitted changes — and asks for confirmation
before touching anything.
"""

import shutil
import sys
import tomllib
from pathlib import Path

from ow.utils import index, paths
from ow.utils.config import BranchSpec, WorkspaceConfig, load_workspace_config
from ow.utils.display import err_console
from ow.utils.git import (
    count_commits,
    dirty_files,
    is_branch_pushed,
    run_cmd,
    worktree_exists,
)

MARKER = Path(".ow") / "config.toml"


class _RepoInfo:
    """What rm gathered about one repo: observation only, nothing applied."""

    __slots__ = (
        "alias", "spec", "bare_repo", "worktree_path",
        "bare_exists", "worktree_exists",
        "dirty", "unpushed", "pushed", "will_delete_branch",
    )

    def __init__(
        self, alias: str, spec: BranchSpec, bare_repo: Path, worktree_path: Path,
    ) -> None:
        self.alias = alias
        self.spec = spec
        self.bare_repo = bare_repo
        self.worktree_path = worktree_path
        self.bare_exists = bare_repo.exists()
        self.worktree_exists = worktree_path.exists()

        self.dirty: list[str] = []
        self.unpushed: int = 0
        self.pushed: bool = False
        self.will_delete_branch: bool = False

        if not self.bare_exists or not self.spec.local_branch:
            return

        self.pushed = is_branch_pushed(bare_repo, spec.local_branch)
        if not self.pushed:
            self.unpushed = count_commits(
                bare_repo, f"{spec.base_ref}..{spec.local_branch}",
            )
        self.will_delete_branch = True

        if self.worktree_exists:
            self.dirty = dirty_files(worktree_path)


def _display_path(path: Path) -> str:
    """Render a path with the home directory abbreviated to ~."""
    home = Path.home()
    try:
        rel = path.relative_to(home)
    except ValueError:
        return str(path)
    if str(rel) == ".":
        return "~"
    return f"~/{rel}"


def _display_summary(name: str, ws_dir: Path, repos: list[_RepoInfo]) -> None:
    """The whole report, in the imperative: nothing here has happened yet."""
    print(f"Removing workspace '{name}' at {_display_path(ws_dir)}")
    print()
    print("Repos:")
    for r in repos:
        spec_str = r.spec.to_spec_str()
        line = f"  {r.alias}: {spec_str}"
        if not r.bare_exists:
            line += "  (bare repo missing)"
        elif r.spec.is_detached:
            line += "  (detached — no local branch to delete)"
        elif not r.will_delete_branch:
            line += "  (no local branch)"
        print(line)

        if r.dirty:
            noun = "file" if len(r.dirty) == 1 else "files"
            print(f"    ⚠ {len(r.dirty)} uncommitted {noun}: {', '.join(r.dirty)}")
        if r.unpushed:
            noun = "commit" if r.unpushed == 1 else "commits"
            print(f"    ⚠ {r.unpushed} unpushed {noun} on {r.spec.local_branch}")
        if r.will_delete_branch and r.pushed and not r.dirty and not r.unpushed:
            print(f"    branch {r.spec.local_branch} pushed — safe to delete")

    print()
    print("Will remove:")
    print(f"  workspace directory {_display_path(ws_dir)}")
    for r in repos:
        if r.bare_exists:
            print(f"  [{r.alias}] worktree + local branch {r.spec.local_branch}"
                  if r.will_delete_branch
                  else f"  [{r.alias}] worktree (detached, no branch)")
    print("  index entry")


def _confirm() -> bool:
    """Default is no. A destructive command must not proceed unasked."""
    try:
        answer = input("\nProceed? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _remove_worktree(bare_repo: Path, worktree_path: Path, alias: str) -> None:
    """Unregister the worktree from the bare repo. Tolerates a gone directory.

    Checks registration first: a directory that exists on disk but isn't a
    registered worktree (manual setup, already pruned) makes `worktree remove`
    print a fatal error to stderr — confusing, even though the fallback prune
    handles it. Skip straight to prune when the worktree isn't registered.
    """
    if worktree_exists(bare_repo, worktree_path):
        run_cmd(
            ["git", "-C", str(bare_repo), "worktree", "remove", "--force", str(worktree_path)],
            quiet=True, label=alias,
        )
    run_cmd(
        ["git", "-C", str(bare_repo), "worktree", "prune"],
        quiet=True, label=alias,
    )


def _delete_branch(bare_repo: Path, branch: str, alias: str) -> bool:
    """Delete a local branch from the bare repo. Returns True on success."""
    result = run_cmd(
        ["git", "-C", str(bare_repo), "branch", "-D", branch],
        quiet=True, label=alias,
    )
    return result.returncode == 0


def cmd_rm(name: str, *, yes: bool = False) -> None:
    """Remove a workspace: worktrees, local branches, directory, and index entry.

    Shows what will be removed — including unpushed commits and uncommitted
    changes — and asks for confirmation before touching anything. `-y/--yes`
    skips the prompt. Bare repos are shared and stay.
    """
    matches = index.find_by_name(name)
    if not matches:
        print(
            f"No workspace named '{name}' found. "
            "Run `ow ls` to see known workspaces.",
            file=sys.stderr,
        )
        sys.exit(1)
    if len(matches) > 1:
        print(f"Multiple workspaces named '{name}':", file=sys.stderr)
        for m in matches:
            print(f"  {m}", file=sys.stderr)
        print("Pass a path instead.", file=sys.stderr)
        sys.exit(1)

    ws_dir = matches[0]
    try:
        ws = load_workspace_config(ws_dir / MARKER)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"Could not read workspace config: {exc}", file=sys.stderr)
        sys.exit(1)

    bare_repos_dir = paths.repos_dir()
    repos: list[_RepoInfo] = []
    for alias, spec in ws.repos.items():
        bare_repo = bare_repos_dir / f"{alias}.git"
        worktree_path = ws_dir / alias
        repos.append(_RepoInfo(alias, spec, bare_repo, worktree_path))

    _display_summary(name, ws_dir, repos)

    if not yes and not _confirm():
        print("Aborted.")
        sys.exit(2)

    sys.stdout.flush()

    # 1. Remove worktrees and delete local branches from bare repos.
    for r in repos:
        if not r.bare_exists:
            continue
        if r.worktree_exists:
            _remove_worktree(r.bare_repo, r.worktree_path, r.alias)
        if r.will_delete_branch:
            if not _delete_branch(r.bare_repo, r.spec.local_branch, r.alias):
                # HEAD branch, a lock, or git's refusal — not fatal: the
                # workspace directory and index entry still go.
                err_console.print(
                    f"  [{r.alias}] could not delete branch "
                    f"{r.spec.local_branch}",
                    markup=False,
                )

    # 2. Remove the workspace directory (worktrees, templates, .ow, .data, etc.).
    shutil.rmtree(ws_dir, ignore_errors=True)

    # 3. Remove the index entry.
    index.forget(ws_dir)

    print("Done.")
