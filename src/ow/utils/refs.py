import subprocess
from dataclasses import dataclass
from typing import NamedTuple

from ow.utils.display import Spinner, _print_git_result
from ow.utils.config import BranchSpec, Config, WorkspaceConfig
from ow.utils.git import (
    get_upstream,
    parallel_per_repo,
    resolve_spec_local,
)


class _FetchJob(NamedTuple):
    bare_repo: str
    remote: str
    refspec: str
    force: bool = False


@dataclass
class _ResolveResult:
    """Result of resolving specs for one alias."""
    track_ref: str
    upstream_ref: str | None
    fetch_jobs: list[_FetchJob]
    resolved_spec: BranchSpec | None = None


def fetch_workspace_refs(
    ws: WorkspaceConfig,
    ws_dir,
    config: Config,
    *,
    fetch_upstreams: bool = False,
    resolve_fn=resolve_spec_local,
    spinner_prefix: str = "Checking",
) -> tuple[dict[str, str], dict[str, str], dict[str, BranchSpec]]:
    """Fetch refs for all workspace repos into their bare repos.

    Returns (resolved_tracks, resolved_upstreams, resolved_specs) dicts.

    Three-phase pipeline:
    1. Resolve specs per repo (parallel) — determines what to fetch
    2. Execute all fetches flat (parallel) — one thread per fetch, not per repo
    3. Print results (sequential)
    """
    bare_repos_dir = config.root_dir / ".bare-git-repos"
    resolved_tracks: dict[str, str] = {}
    resolved_upstreams: dict[str, str] = {}
    resolved_specs: dict[str, BranchSpec] = {}

    # -- Phase 1: resolve specs per repo ----------------------------------

    def _resolve_alias(alias: str, spec: BranchSpec) -> _ResolveResult:
        worktree_path = ws_dir / alias
        alias_remotes = config.remotes.get(alias, {})
        bare_repo_path = bare_repos_dir / f"{alias}.git"
        bare_repo = str(bare_repo_path)
        track_spec = BranchSpec(spec.base_ref)
        jobs: list[_FetchJob] = []

        resolved_track = resolve_fn(bare_repo_path, track_spec, alias_remotes)
        refspec = f"{resolved_track.branch}:refs/remotes/{resolved_track.remote}/{resolved_track.branch}"
        jobs.append(_FetchJob(bare_repo, resolved_track.remote, refspec))

        resolved_spec = resolve_fn(bare_repo_path, spec, alias_remotes)

        upstream_ref = None
        if fetch_upstreams and not spec.is_detached:
            if resolved_spec.base_ref != resolved_track.base_ref:
                full_refspec = f"{resolved_spec.branch}:refs/remotes/{resolved_spec.remote}/{resolved_spec.branch}"
                jobs.append(_FetchJob(bare_repo, resolved_spec.remote, full_refspec, force=True))
                upstream_ref = resolved_spec.base_ref
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
        )

    resolve_tasks = {}
    skipped: list[str] = []
    for alias, spec in ws.repos.items():
        if not (ws_dir / alias).exists():
            skipped.append(alias)
            continue
        resolve_tasks[alias] = (lambda a=alias, s=spec: _resolve_alias(a, s))

    if resolve_tasks:
        with Spinner(f"{spinner_prefix} {len(resolve_tasks)} repo(s)"):
            resolve_results = parallel_per_repo(resolve_tasks)
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
            _print_git_result(alias, "fetch", ["?"], False, str(result))
            resolved_tracks[alias] = ws.repos[alias].base_ref
            continue
        alias_resolve[alias] = result
        for i, job in enumerate(result.fetch_jobs):
            key = f"{alias}:{i}"
            fetch_tasks[key] = job

    # -- Phase 2: execute all fetches flat --------------------------------

    def _do_fetch(job: _FetchJob) -> subprocess.CompletedProcess:
        args = ["git", "-C", job.bare_repo, "fetch"]
        if job.force:
            args.append("-f")
        args.extend([job.remote, job.refspec])
        return subprocess.run(args, capture_output=True)

    if fetch_tasks:
        fetch_callables = {key: (lambda j=job: _do_fetch(j)) for key, job in fetch_tasks.items()}
        with Spinner(f"Fetching {len(fetch_callables)} ref(s)"):
            fetch_results = parallel_per_repo(fetch_callables)
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
        if resolve.resolved_spec:
            resolved_specs[alias] = resolve.resolved_spec

        for i, job in enumerate(resolve.fetch_jobs):
            key = f"{alias}:{i}"
            fetch_result = fetch_results[key]
            if isinstance(fetch_result, Exception):
                _print_git_result(alias, "fetch", [job.remote, job.refspec], False, str(fetch_result))
            elif fetch_result.returncode != 0:
                err = fetch_result.stderr.decode().strip() if fetch_result.stderr else "unknown"
                _print_git_result(alias, "fetch", [job.remote, job.refspec], False, err)
            else:
                _print_git_result(alias, "fetch", [job.remote, job.refspec], True)

    return resolved_tracks, resolved_upstreams, resolved_specs
