import subprocess
from collections import defaultdict
from dataclasses import dataclass
from typing import NamedTuple

from ow.utils.display import print_git_result, task_progress
from ow.utils.config import BranchSpec, Config, WorkspaceConfig
from ow.utils import paths
from ow.utils.git import (
    _run,
    get_upstream,
    parallel_per_repo,
    resolve_spec_local,
    rev_parse,
)


class _FetchJob(NamedTuple):
    bare_repo: str
    remote: str
    refspec: str
    force: bool = False


@dataclass
class FetchOutcome:
    """What fetch_workspace_refs learned, per alias.

    upstream_before holds each upstream ref's SHA as read *before* the fetch.
    It is the whole basis of force-push detection: comparing it to the SHA
    after the fetch needs no reflog, so it does not depend on
    core.logAllRefUpdates being set on the bare repo.

    `failed` holds the aliases whose refs are not what the remote says they
    are — a fetch that failed, or a resolve that never got as far as one.
    Printing the ✗ is not enough: a caller that goes on to rebase does so
    against the stale cached ref, and would report success for it.
    """
    tracks: dict[str, str]
    upstreams: dict[str, str]
    specs: dict[str, BranchSpec]
    upstream_before: dict[str, str]
    failed: frozenset[str] = frozenset()


@dataclass
class _ResolveResult:
    """Result of resolving specs for one alias."""
    track_ref: str
    upstream_ref: str | None
    fetch_jobs: list[_FetchJob]
    resolved_spec: BranchSpec | None = None
    upstream_before: str | None = None


def fetch_workspace_refs(
    ws: WorkspaceConfig,
    ws_dir,
    config: Config,
    fetch_upstreams: bool = False,
    fetch: bool = True,
    resolve_fn=resolve_spec_local,
    spinner_prefix: str = "Checking",
) -> FetchOutcome:
    """Fetch refs for all workspace repos into their bare repos.

    Returns a FetchOutcome.

    When `fetch` is False, phase 2 (the actual `git fetch`) is skipped
    entirely and phase 1 is forced to use `resolve_spec_local`, which
    never fetches on its own. The outcome still names the resolved base
    and upstream refs — `resolve_spec` itself calls `git fetch` when a ref
    is missing locally, which is why the local resolver is forced here
    rather than left to the caller.

    Three-phase pipeline:
    1. Resolve specs per repo (parallel) — determines what to fetch
    2. Execute fetches chained per bare repo (parallel across repos,
       sequential within each repo) — git takes no repo-wide fetch lock,
       so two fetches against the same bare repo must not overlap
    3. Print results (sequential)
    """
    bare_repos_dir = paths.repos_dir()
    resolved_tracks: dict[str, str] = {}
    resolved_upstreams: dict[str, str] = {}
    resolved_specs: dict[str, BranchSpec] = {}
    upstream_before: dict[str, str] = {}
    failed: set[str] = set()
    if not fetch:
        resolve_fn = resolve_spec_local

    def _resolve_alias(alias: str, spec: BranchSpec) -> _ResolveResult:
        worktree_path = ws_dir / alias
        alias_remotes = config.remotes.get(alias, {})
        bare_repo_path = bare_repos_dir / f"{alias}.git"
        if not bare_repo_path.exists():
            raise RuntimeError(
                f"no bare repo at {bare_repo_path}; run `ow apply` to materialize it"
            )
        bare_repo = str(bare_repo_path)
        track_spec = BranchSpec(spec.base_ref)
        jobs: list[_FetchJob] = []

        resolved_track = resolve_fn(bare_repo_path, track_spec, alias_remotes)
        refspec = f"{resolved_track.branch}:refs/remotes/{resolved_track.remote}/{resolved_track.branch}"
        jobs.append(_FetchJob(bare_repo, resolved_track.remote, refspec))

        resolved_spec = resolve_fn(bare_repo_path, spec, alias_remotes)

        upstream_ref = None
        upstream_before = None
        if fetch_upstreams and not spec.is_detached:
            if resolved_spec.base_ref != resolved_track.base_ref:
                full_refspec = f"{resolved_spec.branch}:refs/remotes/{resolved_spec.remote}/{resolved_spec.branch}"
                jobs.append(_FetchJob(bare_repo, resolved_spec.remote, full_refspec, force=True))
                upstream_ref = resolved_spec.base_ref
                # Read before phase 2 rewrites the ref — this is the only
                # moment the previous value is still observable.
                upstream_before = rev_parse(bare_repo_path, f"refs/remotes/{upstream_ref}")
            else:
                upstream = get_upstream(worktree_path)
                if upstream:
                    parts = upstream.split("/", 1)
                    if len(parts) == 2:
                        already_fetched = (parts[0] == resolved_track.remote and parts[1] == resolved_track.branch)
                        if not already_fetched:
                            upstream_refspec = f"{parts[1]}:refs/remotes/{upstream}"
                            jobs.append(_FetchJob(bare_repo, parts[0], upstream_refspec))

        return _ResolveResult(
            track_ref=resolved_track.base_ref,
            upstream_ref=upstream_ref,
            fetch_jobs=jobs,
            resolved_spec=resolved_spec,
            upstream_before=upstream_before,
        )

    resolve_tasks = {}
    skipped: list[str] = []
    for alias, spec in ws.repos.items():
        if not (ws_dir / alias).exists():
            skipped.append(alias)
            continue
        resolve_tasks[alias] = (lambda a=alias, s=spec: _resolve_alias(a, s))

    if resolve_tasks:
        with task_progress(f"{spinner_prefix} repo(s)", len(resolve_tasks)) as advance:
            resolve_results = parallel_per_repo(
                resolve_tasks, on_done=lambda _alias: advance()
            )
    else:
        resolve_results = {}

    # Collect resolve results; build flat fetch jobs
    alias_resolve: dict[str, _ResolveResult] = {}
    fetch_tasks: dict[str, _FetchJob] = {}
    for alias in ws.repos:
        if alias in skipped:
            continue
        result = resolve_results[alias]
        if isinstance(result, Exception):
            print_git_result(alias, "resolve", [], False, str(result))
            resolved_tracks[alias] = ws.repos[alias].base_ref
            failed.add(alias)
            continue
        alias_resolve[alias] = result
        for i, job in enumerate(result.fetch_jobs):
            key = f"{alias}:{i}"
            fetch_tasks[key] = job

    # -- Phase 2: execute fetches chained per bare repo -------------------
    #
    # git-fetch takes no repo-wide lock.  Two fetches against the same
    # bare repo race on loose-ref updates and can corrupt them.  Group
    # jobs by bare repo: chains run in parallel across repos, but jobs
    # within each chain run one at a time.

    def _do_fetch(job: _FetchJob) -> subprocess.CompletedProcess:
        args = ["git", "-C", job.bare_repo, "fetch"]
        if job.force:
            args.append("-f")
        args.extend([job.remote, job.refspec])
        return _run(args, capture_output=True)

    if fetch and fetch_tasks:
        repo_chains: dict[str, list[tuple[str, _FetchJob]]] = defaultdict(list)
        for key, job in fetch_tasks.items():
            repo_chains[job.bare_repo].append((key, job))

        def _run_chain(items: list[tuple[str, _FetchJob]]) -> dict[str, subprocess.CompletedProcess]:
            return {key: _do_fetch(job) for key, job in items}

        chain_tasks = {
            repo: (lambda chain=items: _run_chain(chain))
            for repo, items in repo_chains.items()
        }
        with task_progress("Fetching ref(s)", len(fetch_tasks)) as advance:
            def _on_chain_done(repo: str) -> None:
                for _ in repo_chains[repo]:
                    advance()
            chain_results = parallel_per_repo(
                chain_tasks,
                on_done=_on_chain_done,
            )
        fetch_results: dict[str, subprocess.CompletedProcess | Exception] = {}
        for result in chain_results.values():
            if isinstance(result, Exception):
                continue
            fetch_results.update(result)
        for repo, result in chain_results.items():
            if isinstance(result, Exception):
                for key, _job in repo_chains[repo]:
                    fetch_results[key] = result
    else:
        fetch_results = {}

    # -- Phase 3: print results -------------------------------------------

    for alias in ws.repos:
        if alias in skipped or alias not in alias_resolve:
            continue
        resolve = alias_resolve[alias]
        resolved_tracks[alias] = resolve.track_ref
        if resolve.upstream_ref:
            resolved_upstreams[alias] = resolve.upstream_ref
        if resolve.upstream_before:
            upstream_before[alias] = resolve.upstream_before
        if resolve.resolved_spec:
            resolved_specs[alias] = resolve.resolved_spec

        if fetch:
            for i, job in enumerate(resolve.fetch_jobs):
                key = f"{alias}:{i}"
                fetch_result = fetch_results[key]
                if isinstance(fetch_result, Exception):
                    print_git_result(alias, "fetch", [job.remote, job.refspec], False, str(fetch_result))
                    failed.add(alias)
                elif fetch_result.returncode != 0:
                    err = fetch_result.stderr.decode().strip() if fetch_result.stderr else "unknown"
                    print_git_result(alias, "fetch", [job.remote, job.refspec], False, err)
                    failed.add(alias)
                else:
                    print_git_result(alias, "fetch", [job.remote, job.refspec], True)

    return FetchOutcome(
        tracks=resolved_tracks,
        upstreams=resolved_upstreams,
        specs=resolved_specs,
        upstream_before=upstream_before,
        failed=frozenset(failed),
    )
