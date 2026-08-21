import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from textwrap import indent
from typing import Callable, TypeVar

from ow.utils import paths
from ow.utils.config import BranchSpec, RemoteConfig

# Every git child is tracked here so an interrupt can kill it. An abandoned
# `git fetch` holds a lock inside a bare repo that every workspace shares, so
# the next plain `git fetch` blocks on it — issue #26.
_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run, with the child in its own process group and tracked.

    start_new_session detaches the child from our process group, so a Ctrl-C
    typed at the terminal does not reach it directly — we decide when and how
    it dies, rather than racing the signal.

    Built on Popen.communicate rather than subprocess.run itself, so two
    subprocess.run-only keywords need translating before they reach Popen:
    `check` (Popen has no such concept) and `capture_output` (Popen has no
    such keyword at all — it wants stdout=/stderr=PIPE instead).
    """
    check = kwargs.pop("check", False)
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    # The lock spans Popen() itself, not just the registration after it: a
    # child that exists between Popen() returning and the lock being taken
    # would be invisible to a terminate_children() racing that window.
    # Spawning is a few milliseconds and there are single-digit repos, so
    # serialising it costs nothing measurable. The lock must not extend to
    # communicate() below, or a parallel run would become sequential.
    with _children_lock:
        proc = subprocess.Popen(args, start_new_session=True, **kwargs)
        _children.add(proc)
    try:
        stdout, stderr = proc.communicate()
    except BaseException:
        # Whatever went wrong here — a Ctrl-C landing in communicate() above
        # all — the child must not outlive it. Its own session means the
        # terminal's SIGINT never reached it, and the discard below takes it
        # out of terminate_children()'s reach, so this is the last moment
        # anything can still kill it.
        _kill_group(proc)
        raise
    finally:
        with _children_lock:
            _children.discard(proc)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args, stdout, stderr)
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the child's whole process group, tolerating its being gone.

    The group, not the process: git drives helpers of its own (ssh, git-remote-*,
    index-pack), and signalling only the parent leaves those behind.
    """
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill_group(proc: subprocess.Popen, grace: float = 2.0) -> None:
    """SIGTERM one child's group, SIGKILL it if it outlives `grace`, and reap it."""
    _signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        _signal_group(proc, signal.SIGKILL)
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass


def live_children() -> int:
    with _children_lock:
        return len(_children)


def terminate_children(grace: float = 2.0) -> int:
    """SIGTERM every tracked child's group, SIGKILL whatever outlives `grace`.

    Returns how many children were registered when called.
    """
    with _children_lock:
        procs = list(_children)
        _children.clear()

    for proc in procs:
        _signal_group(proc, signal.SIGTERM)

    # One deadline shared by every child, rather than `grace` each: they were
    # all signalled together, so they get their grace period together.
    deadline = time.monotonic() + grace
    for proc in procs:
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass

    return len(procs)


def run_cmd(args: list[str], quiet: bool = False, label: str | None = None, **kwargs) -> subprocess.CompletedProcess:
    if not quiet:
        if label:
            display_args = args
            if len(args) >= 3 and args[0] == "git" and args[1] == "-C":
                display_args = ["git"] + args[3:]
            print(f"  [{label}] {' '.join(display_args)}", file=sys.stderr)
        else:
            print(f"    $ {' '.join(args)}", file=sys.stderr)
    return _run(args, **kwargs)


def ordered_remotes(alias_remotes: dict[str, RemoteConfig]) -> list[str]:
    result = []
    if "origin" in alias_remotes:
        result.append("origin")
    result.extend(sorted(r for r in alias_remotes if r != "origin"))
    return result


def _get_bare_config(bare_repo: Path) -> dict[str, str]:
    """Read all local git config as a dict via a single subprocess."""
    result = _run(
        ["git", "-C", str(bare_repo), "config", "--list", "--local"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {}
    config: dict[str, str] = {}
    for line in result.stdout.strip().split("\n"):
        if "=" in line:
            key, _, value = line.partition("=")
            config[key] = value
    return config


def _is_bare_repo(path: Path) -> bool:
    """True only if `path` is itself a git repository.

    `--absolute-git-dir` rather than a bare `--git-dir` check: git discovers
    repositories by walking upwards, so a plain directory that happens to sit
    inside one would otherwise answer yes. Comparing what git resolved against
    the path asked about pins the answer to this directory.

    The comparison is samefile() and not `==`: git answers with every symlink
    resolved, while `path` is built from XDG_DATA_HOME exactly as configured.
    On a machine whose home traverses a symlink — /home -> /var/home is the
    common one — two spellings of the same directory would not match as
    strings, and ow answers "not a repository" by cloning over the top of the
    user's work. Asking the filesystem whether they are the same object also
    covers bind mounts and case-insensitive filesystems, where resolving the
    strings still would not match.
    """
    if not path.is_dir():
        return False
    result = _run(
        ["git", "-C", str(path), "rev-parse", "--absolute-git-dir"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    try:
        return os.path.samefile(result.stdout.strip(), path)
    except OSError:
        # Both paths existed a moment ago; if one no longer does, or cannot be
        # stat'd, "not a repository" is the answer already given everywhere
        # else on this path.
        return False


def _git_calls_it_a_repository(path: Path) -> bool:
    """Whether git validates `path` itself as a git directory.

    `rev-parse --resolve-git-dir` never walks upwards and never compares paths
    — it inspects the directory it is handed — so it is an answer to the same
    question by an independent route. That independence is the point: it is
    used to veto destructive repairs, and a veto that shared _is_bare_repo's
    machinery would share its mistakes.
    """
    if not path.is_dir():
        return False
    return _run(
        ["git", "rev-parse", "--resolve-git-dir", str(path)],
        capture_output=True, text=True,
    ).returncode == 0


def _refuse_to_displace_a_repository(bare_repo: Path) -> None:
    """Stop before a repair can destroy a repository it failed to recognise.

    Getting here means _is_bare_repo answered "not a repository", and the
    repair that follows renames whatever is at the path to <name>.broken and
    deletes whatever was already there. That is the user's work if the answer
    was wrong, and one wrong answer — a bug like the symlink comparison, a git
    that would not start, a directory that could not be read — must not be
    enough to lose it. So ask git again by a route that shares nothing with
    the first answer, and refuse rather than widen the retry: a false refusal
    costs the user one message, a false repair costs them their commits.
    """
    if _git_calls_it_a_repository(bare_repo):
        raise RuntimeError(
            f"{bare_repo} is a git repository, but ow does not recognise it as "
            f"the bare repo it expects there, so it will not move it aside.\n"
            f"  Move it somewhere safe (or remove it) and run ow again."
        )
    broken = bare_repo.with_name(f"{bare_repo.name}.broken")
    if bare_repo.exists() and _git_calls_it_a_repository(broken):
        raise RuntimeError(
            f"{broken} is a git repository left by an earlier repair, and ow "
            f"would have to delete it to move {bare_repo.name} aside.\n"
            f"  Move it somewhere safe (or remove it) and run ow again."
        )


def _clone_bare_into_place(alias: str, url: str, bare_repo: Path) -> None:
    """Clone into a sibling directory and move it into place once it is whole.

    Nothing appears at the final path until there is a complete repository to
    put there, so a clone that is interrupted — terminate_children() SIGKILLs
    after a two-second grace, which an Odoo-sized clone will not always beat —
    leaves no half-repo for the next run to trust.

    The staging path is derived from the alias rather than randomised, so a
    killed run leaves at most one leftover and the next one reuses the space
    instead of accumulating another copy.

    Refuses before it clones if anything git recognises as a repository stands
    where it would have to move or delete — see
    _refuse_to_displace_a_repository.
    """
    _refuse_to_displace_a_repository(bare_repo)
    staging = bare_repo.with_name(f"{bare_repo.name}.incoming")
    bare_repo.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(staging, ignore_errors=True)
    try:
        _clone_bare(alias, url, staging)
        if bare_repo.exists():
            # Whatever is there is not a repository, but ow did not put it
            # there and cannot know it holds nothing of the user's. One slot,
            # so a repeatedly-repaired repo cannot fill the disk with copies.
            broken = bare_repo.with_name(f"{bare_repo.name}.broken")
            shutil.rmtree(broken, ignore_errors=True)
            os.rename(bare_repo, broken)
            print(f"  [{alias}] {bare_repo} was not a git repository; moved to {broken}",
                  file=sys.stderr)
        os.rename(staging, bare_repo)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _undefined_repo_message(alias: str, remotes: dict[str, RemoteConfig]) -> str:
    """Why ow cannot clone `alias`, in the user's terms and with a file to open.

    "No origin remote configured" names one of ow's own notions and points at
    nothing; a workspace only ever names a repo, and the global config is where
    that name has to be defined.
    """
    config_file = paths.config_file()
    if remotes:
        return (
            f"repo '{alias}' has no origin remote\n"
            f"  Add origin.url to [remotes.{alias}] in {config_file}"
        )
    return (
        f"configuration references repo '{alias}' but it's not defined in [remotes]\n"
        f"  Add a [remotes.{alias}] section with an origin.url to {config_file}"
    )


def _clone_bare(alias: str, url: str, destination: Path) -> None:
    """Clone `url` into `destination`, failing with git's own diagnosis.

    The output is captured only so that a failure can carry it: bare
    CalledProcessError stringifies to "Command '[...]' returned non-zero exit
    status 128", which tells the user nothing, while git has already said
    "repository does not exist" or "Permission denied (publickey)". Several of
    these run in parallel under a progress counter, where interleaved transfer
    lines would be unreadable anyway.
    """
    try:
        run_cmd(
            [
                "git", "clone", "--bare", "--filter=blob:none",
                "--single-branch",
                url, str(destination),
            ],
            label=alias,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        if detail:
            raise RuntimeError(
                f"cloning '{alias}' from {url} failed:\n" + indent(detail, "  ")
            ) from exc
        raise RuntimeError(
            f"cloning '{alias}' from {url} failed with exit status {exc.returncode}"
        ) from exc


def ensure_bare_repo(
    alias: str,
    remotes: dict[str, RemoteConfig],
    bare_repos_dir: Path,
) -> None:
    bare_repo = bare_repos_dir / f"{alias}.git"
    if not _is_bare_repo(bare_repo):
        origin = remotes.get("origin")
        if not origin:
            raise ValueError(_undefined_repo_message(alias, remotes))
        _clone_bare_into_place(alias, origin.url, bare_repo)

    # Configure non-origin remotes (skip writes when values already match)
    current_config = _get_bare_config(bare_repo)

    # Bare repos default core.logAllRefUpdates to false, so remote-tracking refs
    # get no reflog and `git merge-base --fork-point` has nothing to walk. That
    # reflog is how a force-push stays detectable after some other command
    # (ow status, or a plain git fetch) has already absorbed it.
    if current_config.get("core.logallrefupdates") != "true":
        run_cmd(
            ["git", "-C", str(bare_repo), "config", "core.logAllRefUpdates", "true"],
            quiet=True, check=True, label=alias,
        )

    for remote_name in ordered_remotes(remotes):
        remote_cfg = remotes[remote_name]
        desired: dict[str, str] = {}
        if remote_name != "origin":
            desired[f"remote.{remote_name}.url"] = remote_cfg.url
        if remote_cfg.pushurl:
            desired[f"remote.{remote_name}.pushurl"] = remote_cfg.pushurl
        if remote_cfg.fetch:
            desired[f"remote.{remote_name}.fetch"] = remote_cfg.fetch
        for key, value in desired.items():
            if current_config.get(key) != value:
                run_cmd(
                    ["git", "-C", str(bare_repo), "config", key, value],
                    quiet=True, check=True, label=alias,
                )


def ensure_ref(bare_repo: Path, remote: str, branch: str) -> None:
    ref = f"refs/remotes/{remote}/{branch}"
    result = _run(
        ["git", "-C", str(bare_repo), "rev-parse", "--verify", ref],
        capture_output=True,
    )
    if result.returncode != 0:
        run_cmd(
            ["git", "-C", str(bare_repo), "fetch", remote, f"{branch}:refs/remotes/{remote}/{branch}"],
            label=bare_repo.stem,
            check=True,
        )


def _ensure_base_ref_non_fatal(bare_repo: Path, spec: BranchSpec) -> None:
    """Ensure refs/remotes/spec.remote/spec.branch exists locally; non-fatal if it can't be fetched."""
    base_ref = f"refs/remotes/{spec.remote}/{spec.branch}"
    if _run(
        ["git", "-C", str(bare_repo), "rev-parse", "--verify", base_ref],
        capture_output=True,
    ).returncode != 0:
        _run(
            ["git", "-C", str(bare_repo), "fetch", spec.remote,
             f"{spec.branch}:refs/remotes/{spec.remote}/{spec.branch}"],
            capture_output=True,
        )


def resolve_spec(bare_repo: Path, spec: BranchSpec, alias_remotes: dict[str, RemoteConfig]) -> BranchSpec:
    """Find which remote actually has spec.branch; return updated BranchSpec with correct remote.

    If spec.local_branch already exists on a remote (i.e. already pushed), that remote
    branch is used as base_ref so the worktree tracks the correct upstream.
    """
    remotes_to_try = [spec.remote]
    for remote_name in ordered_remotes(alias_remotes):
        if remote_name not in remotes_to_try:
            remotes_to_try.append(remote_name)

    # First: if local_branch is set, check whether it already exists on a remote.
    # If it does, use that as base_ref so the worktree tracks its upstream.
    if spec.local_branch is not None:
        for remote in remotes_to_try:
            ref = f"refs/remotes/{remote}/{spec.local_branch}"
            result = _run(
                ["git", "-C", str(bare_repo), "rev-parse", "--verify", ref],
                capture_output=True,
            )
            if result.returncode == 0:
                _ensure_base_ref_non_fatal(bare_repo, spec)
                return BranchSpec(f"{remote}/{spec.local_branch}", spec.local_branch)
            result = _run(
                ["git", "-C", str(bare_repo), "fetch", remote,
                 f"{spec.local_branch}:refs/remotes/{remote}/{spec.local_branch}"],
                capture_output=True,
            )
            if result.returncode == 0:
                _ensure_base_ref_non_fatal(bare_repo, spec)
                return BranchSpec(f"{remote}/{spec.local_branch}", spec.local_branch)

    # Fall through: find which remote has the base branch.
    for remote in remotes_to_try:
        ref = f"refs/remotes/{remote}/{spec.branch}"
        result = _run(
            ["git", "-C", str(bare_repo), "rev-parse", "--verify", ref],
            capture_output=True,
        )
        if result.returncode == 0:
            return BranchSpec(f"{remote}/{spec.branch}", spec.local_branch)
        result = _run(
            ["git", "-C", str(bare_repo), "fetch", remote,
             f"{spec.branch}:refs/remotes/{remote}/{spec.branch}"],
            capture_output=True,
        )
        if result.returncode == 0:
            return BranchSpec(f"{remote}/{spec.branch}", spec.local_branch)

    raise RuntimeError(f"Branch '{spec.branch}' not found on any configured remote")


def worktree_exists(bare_repo: Path, worktree_path: Path) -> bool:
    if not worktree_path.exists():
        return False
    result = _run(
        ["git", "-C", str(bare_repo), "worktree", "list"],
        capture_output=True, text=True, check=True,
    )
    return str(worktree_path) in result.stdout


def get_all_remote_refs(bare_repo: Path) -> set[str]:
    """Return all remote refs as short names (e.g. 'origin/master') via a single subprocess."""
    result = _run(
        ["git", "-C", str(bare_repo), "for-each-ref", "--format=%(refname:short)", "refs/remotes/"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return set()
    return {line for line in result.stdout.strip().split("\n") if line}


def resolve_spec_local(
    bare_repo: Path, spec: BranchSpec, alias_remotes: dict[str, RemoteConfig],
    *, refs: set[str] | None = None,
) -> BranchSpec:
    """Find which remote has spec.branch in local refs (no fetch). Raises RuntimeError if not found."""
    if refs is None:
        refs = get_all_remote_refs(bare_repo)
    remotes_to_try = [spec.remote] + [r for r in ordered_remotes(alias_remotes) if r != spec.remote]
    for remote in remotes_to_try:
        candidate = f"{remote}/{spec.branch}"
        if candidate in refs:
            return BranchSpec(f"{remote}/{spec.branch}", spec.local_branch)
    raise RuntimeError(f"Branch '{spec.branch}' not found in local refs")


def set_branch_upstream(bare_repo: Path, local_branch: str, remote: str, remote_branch: str) -> None:
    """Write branch.X.remote / branch.X.merge directly into the bare repo's git config.

    This is the correct mechanism for selective-fetch bare repos. The bare repo is cloned
    with --single-branch, so only the initial branch has a normal fetch refspec entry.
    Additional branches are fetched explicitly with custom mappings, intentionally outside
    the normal refspec — so `git branch --set-upstream-to` (which validates against the
    refspec before writing) would refuse. Writing branch.X.remote / branch.X.merge directly
    is the documented git mechanism that `--set-upstream-to` itself uses under the hood.
    """
    alias = bare_repo.stem
    run_cmd(
        ["git", "-C", str(bare_repo), "config", f"branch.{local_branch}.remote", remote],
        label=alias,
        check=True,
    )
    run_cmd(
        ["git", "-C", str(bare_repo), "config", f"branch.{local_branch}.merge", f"refs/heads/{remote_branch}"],
        label=alias,
        check=True,
    )


def create_worktree(bare_repo: Path, worktree_path: Path, spec: BranchSpec) -> None:
    alias = bare_repo.stem
    if spec.is_detached:
        run_cmd(
            ["git", "-C", str(bare_repo), "worktree", "add", "--detach", str(worktree_path), spec.base_ref],
            label=alias,
            check=True,
        )
    else:
        branch_exists = _run(
            ["git", "-C", str(bare_repo), "rev-parse", "--verify", f"refs/heads/{spec.local_branch}"],
            capture_output=True,
        ).returncode == 0
        if branch_exists:
            run_cmd(
                ["git", "-C", str(bare_repo), "worktree", "add", str(worktree_path), spec.local_branch],
                label=alias,
                check=True,
            )
        else:
            run_cmd(
                ["git", "-C", str(bare_repo), "worktree", "add", "-b", spec.local_branch, str(worktree_path), spec.base_ref],
                label=alias,
                check=True,
            )
        set_branch_upstream(bare_repo, spec.local_branch, spec.remote, spec.branch)


def get_rev_list_count(repo_path: Path, ref_a: str, ref_b: str) -> tuple[int, int]:
    """Return (ahead, behind): ref_a ahead of ref_b, ref_a behind ref_b."""
    result = _run(
        ["git", "-C", str(repo_path), "rev-list", "--left-right", "--count", f"{ref_a}...{ref_b}"],
        capture_output=True, text=True, check=True,
    )
    parts = result.stdout.strip().split()
    return int(parts[0]), int(parts[1])


def get_worktree_head(worktree_path: Path) -> tuple[str, str]:
    """Return (short_hash, full_hash)."""
    result = _run(
        ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    full_hash = result.stdout.strip()
    return full_hash[:7], full_hash


def get_upstream(worktree_path: Path) -> str | None:
    result = _run(
        ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def worktree_is_detached(worktree_path: Path) -> bool:
    """True if HEAD is detached (no symbolic ref)."""
    result = _run(
        ["git", "-C", str(worktree_path), "symbolic-ref", "--quiet", "HEAD"],
        capture_output=True,
    )
    return result.returncode != 0


def get_worktree_branch(worktree_path: Path) -> str | None:
    """Return the current branch name, or None if HEAD is detached."""
    result = _run(
        ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return None if branch == "HEAD" else branch


def attach_worktree(bare_repo: Path, worktree_path: Path, spec: BranchSpec) -> None:
    """Switch a detached worktree to a local branch tracking spec.base_ref."""
    alias = worktree_path.name
    branch_exists = _run(
        ["git", "-C", str(bare_repo), "rev-parse", "--verify", f"refs/heads/{spec.local_branch}"],
        capture_output=True,
    ).returncode == 0
    if branch_exists:
        run_cmd(
            ["git", "-C", str(worktree_path), "switch", spec.local_branch],
            label=alias,
            check=True,
        )
    else:
        run_cmd(
            ["git", "-C", str(worktree_path), "switch", "-c", spec.local_branch],
            label=alias,
            check=True,
        )
    set_branch_upstream(bare_repo, spec.local_branch, spec.remote, spec.branch)


def detach_worktree(worktree_path: Path, base_ref: str) -> None:
    """Switch an attached worktree to detached HEAD at base_ref."""
    run_cmd(
        ["git", "-C", str(worktree_path), "switch", "--detach", base_ref],
        label=worktree_path.name,
        check=True,
    )


def get_remote_ref_for_branch(
    repo: Path, local_branch: str, alias_remotes: dict,
    exclude_ref: str | None = None, base_remote: str | None = None,
    *, refs: set[str] | None = None,
) -> str | None:
    """Check all ow.toml-configured remotes for refs/remotes/{remote}/{local_branch}.

    Returns the first match (as "{remote}/{local_branch}"), excluding exclude_ref
    (typically spec.base_ref, to avoid returning the base branch itself).
    base_remote is checked last so fork remotes are preferred over the upstream.

    ``repo`` can be a bare repo or a worktree — any git repo path works.
    If ``refs`` is provided, does pure in-memory lookup instead of subprocess calls.
    """
    if refs is None:
        refs = get_all_remote_refs(repo)
    remotes = ordered_remotes(alias_remotes)
    if base_remote and base_remote in remotes:
        remotes.remove(base_remote)
        remotes.append(base_remote)
    for remote in remotes:
        candidate = f"{remote}/{local_branch}"
        if candidate == exclude_ref:
            continue
        if candidate in refs:
            return candidate
    return None


def get_remote_url(bare_repo: Path, remote: str) -> str | None:
    result = _run(
        ["git", "-C", str(bare_repo), "remote", "get-url", remote],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def git(repo: Path, *args, quiet: bool = False, **kwargs) -> subprocess.CompletedProcess:
    """Central git wrapper with automatic -C."""
    if repo.suffix == ".git" and repo.parent == paths.repos_dir():
        label = repo.stem
    else:
        label = repo.name
    return run_cmd(["git", "-C", str(repo)] + list(args), quiet=quiet, label=label, **kwargs)


def git_fetch(repo: Path, remote: str, refspec: str, *, force: bool = False, **kwargs) -> None:
    """Fetch with optional force (+refspec)."""
    ref = f"+{refspec}" if force else refspec
    git(repo, "fetch", remote, ref, **kwargs)


def _git_dir(worktree: Path) -> Path | None:
    """Resolve the real .git directory.

    Worktrees attached to a bare repo have a .git *file* pointing into
    <bare>/worktrees/<name>, so `worktree / ".git"` is not a directory.
    """
    result = _run(
        ["git", "-C", str(worktree), "rev-parse", "--absolute-git-dir"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def rev_parse(repo: Path, ref: str) -> str | None:
    """Resolve ref to a full SHA, or None if it does not exist."""
    result = _run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True, text=True,
    )
    return result.stdout.strip() or None


def is_ancestor(repo: Path, a: str, b: str) -> bool:
    """True if a is an ancestor of b (a commit is its own ancestor)."""
    return _run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", a, b],
        capture_output=True,
    ).returncode == 0


def merge_base(repo: Path, a: str, b: str) -> str | None:
    result = _run(
        ["git", "-C", str(repo), "merge-base", a, b],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def merge_base_fork_point(repo: Path, upstream: str, ref: str = "HEAD") -> str | None:
    """The newest past value of `upstream` that `ref` is built on.

    Walks the upstream ref's reflog newest-first and returns the first entry
    that is an ancestor of `ref`. Returns None when the reflog is missing or
    holds nothing usable — which is why the caller must treat it as one
    candidate among several, never as the answer.
    """
    result = _run(
        ["git", "-C", str(repo), "merge-base", "--fork-point", upstream, ref],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def count_commits(repo: Path, rev_range: str) -> int:
    result = _run(
        ["git", "-C", str(repo), "rev-list", "--count", rev_range],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return 0
    return int(result.stdout.strip() or 0)


def count_new_patches(worktree: Path, other: str) -> int:
    """Commits in `other` whose patch HEAD does not already carry.

    Plain commit counting is wrong here: after a rebase the original commits
    are unreachable from HEAD under their old SHAs, so `HEAD..other` stays
    positive forever and the caller would rebase onto a stale ref on every
    run. --cherry-pick drops commits with an equivalent patch on the other
    side, which is the same test git rebase applies internally.
    """
    result = _run(
        ["git", "-C", str(worktree), "rev-list", "--count",
         "--cherry-pick", "--right-only", "--no-merges", f"HEAD...{other}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return 0
    return int(result.stdout.strip() or 0)


def count_unpushed(worktree: Path, bound: str, other: str) -> int:
    """Commits in bound..HEAD whose patch `other` does not already carry.

    A plain `bound..HEAD` count is wrong for the same reason `count_new_patches`
    exists: after a rebase, commits that are already on `other` get new SHAs
    locally, so counting SHAs would report them as unpushed forever. And using
    `other`'s own merge-base with HEAD instead of `bound` is wrong too — the
    moment `other`'s history diverges from HEAD's (a colleague's push, a
    squash), that merge-base slides behind `bound` and pulls the base branch's
    own commits into the count.

    `git cherry <other> HEAD <bound>` is built exactly for this: it walks
    `bound..HEAD`, and for each commit checks whether `other` already carries
    an equivalent patch ('-') or not ('+'). Only the '+' commits are genuinely
    unpushed.
    """
    result = _run(
        ["git", "-C", str(worktree), "cherry", other, "HEAD", bound],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return 0
    return sum(1 for line in result.stdout.splitlines() if line.startswith("+"))


# Ordered: rebase markers first, because an interactive rebase also writes a
# sequencer directory and must not be reported as a cherry-pick.
_IN_PROGRESS_MARKERS: tuple[tuple[str, str], ...] = (
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("sequencer", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
    ("MERGE_HEAD", "merge"),
)


def in_progress_operation(worktree: Path) -> tuple[str, str, str] | None:
    """Return (operation, continue_command, abort_command), or None if idle."""
    git_dir = _git_dir(worktree)
    if git_dir is None:
        return None
    for marker, operation in _IN_PROGRESS_MARKERS:
        if (git_dir / marker).exists():
            return operation, f"git {operation} --continue", f"git {operation} --abort"
    return None


def dirty_files(worktree: Path) -> list[str]:
    """Modified tracked files. Untracked files do not block a rebase."""
    result = _run(
        ["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return [line[3:] for line in result.stdout.splitlines() if line.strip()]


T = TypeVar("T")


def parallel_per_repo(
    tasks: dict[str, Callable[[], T]],
    *,
    on_done: Callable[[str], None] | None = None,
) -> dict[str, T | Exception]:
    """Run callables in parallel per repo alias. Returns {alias: result_or_exception}.

    Deliberately not a `with` block: ThreadPoolExecutor's context manager exits
    through shutdown(wait=True), which swallows an interrupt into a join and
    leaves the git children running. Collecting through as_completed gives the
    interrupt somewhere to land, and gives callers a completion event to count.
    """
    if not tasks:
        return {}

    results: dict[str, T | Exception] = {}
    pool = ThreadPoolExecutor()
    try:
        futures = {pool.submit(fn): alias for alias, fn in tasks.items()}
        for future in as_completed(futures):
            alias = futures[future]
            try:
                results[alias] = future.result()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                results[alias] = exc
            if on_done is not None:
                on_done(alias)
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        terminate_children()
        raise
    except BaseException:
        # Anything else reaching here came from on_done, a caller-supplied
        # callback. It must not leak the pool on its way out.
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)

    return results
