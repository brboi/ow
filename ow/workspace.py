from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from ow.config import (
    BranchSpec,
    Config,
    WorkspaceConfig,
    archive_workspace,
    format_workspace,
    parse_branch_spec,
    update_config_workspaces,
)
from ow.git import (
    _set_branch_upstream,
    attach_worktree,
    create_worktree,
    detach_worktree,
    ensure_bare_repo,
    get_remote_ref_for_branch,
    get_remote_url,
    get_rev_list_count,
    get_upstream,
    get_worktree_branch,
    get_worktree_head,
    parallel_fetch,
    remove_worktree,
    resolve_spec,
    resolve_spec_local,
    run_cmd,
    worktree_exists,
    worktree_is_detached,
)


# ---------------------------------------------------------------------------
# File generators
# ---------------------------------------------------------------------------

def is_odoo_main_repo(repo_dir: Path) -> bool:
    """Detect if a repo is the main Odoo source (community)."""
    return (
        (repo_dir / "odoo-bin").exists()
        and (repo_dir / "addons").is_dir()
        and (repo_dir / "odoo" / "addons").is_dir()
    )


def find_addon_paths(path: Path) -> list[Path]:
    """Return addons_path directories found under path.

    Descends level by level. Stops descending into a directory once it
    is identified as an addons_path (has children with __manifest__.py).
    Returns [] if path is not a directory or contains no addons.
    """
    if not path.is_dir():
        return []

    children = [p for p in path.iterdir() if p.is_dir()]

    # Is path itself an addons_path?
    if any((child / "__manifest__.py").exists() for child in children):
        return [path]

    # Recurse into subdirectories
    result: list[Path] = []
    for child in children:
        result.extend(find_addon_paths(child))

    return sorted(result)


def build_template_context(ws: WorkspaceConfig, config: Config, ws_dir: Path) -> dict:
    main_repo_alias = next(
        (alias for alias in ws.repos if is_odoo_main_repo(ws_dir / alias)),
        None,
    )

    addons_paths: list[str] = []
    main_addons_paths: list[str] = []
    odools_path_items: list[str] = []
    odools_main_items: list[str] = []

    for alias in ws.repos:
        repo_dir = ws_dir / alias
        if is_odoo_main_repo(repo_dir):
            main_addons_paths = [
                str(repo_dir / "addons"),
                str(repo_dir / "odoo" / "addons"),
            ]
            odools_main_items = [
                f"{alias}/addons",
                f"{alias}/odoo/addons",
            ]
        else:
            addons_paths.extend(str(p) for p in find_addon_paths(repo_dir))
            for p in find_addon_paths(repo_dir):
                odools_path_items.append(str(p.relative_to(ws_dir)))

    return {
        "ws_name": ws.name,
        "main_repo_alias": main_repo_alias,
        "repos": list(ws.repos.keys()),
        "vars": {**config.vars, **ws.vars},
        "addons_paths": addons_paths + main_addons_paths,
        "odools_path_items": odools_path_items + odools_main_items,
    }


# ---------------------------------------------------------------------------
# Cache / drift detection
# ---------------------------------------------------------------------------

def _compute_hash(path: Path) -> str:
    h = hashlib.sha256()
    if path.is_file():
        h.update(path.read_bytes())
    else:
        for p in sorted(path.rglob("*")):
            if p.is_file():
                h.update(str(p.relative_to(path)).encode())
                h.update(p.read_bytes())
    return h.hexdigest()


def _load_cache(root: Path) -> dict:
    cache_path = root / ".ow.cache"
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    return {}


def _save_cache(root: Path, cache: dict) -> None:
    (root / ".ow.cache").write_text(json.dumps(cache, indent=2) + "\n")


def _check_source_drift(root: Path, key: str, source: Path) -> bool:
    """Return True if the user wants to abort."""
    cache = _load_cache(root)
    entry = cache.get(key, {})
    current_hash = _compute_hash(source)
    if entry.get("hash") == current_hash:
        return False

    if not sys.stdin.isatty():
        print(f"(ow) [warn] {source.name} has been updated.", file=sys.stderr)
        return False

    print(f"[warn] {source.name} has been updated since you last copied it.")
    print("  [c] continue (default)")
    print("  [s] continue and skip displaying this until next update")
    print("  [a] abort now")
    choice = input(" > [Csa] ").strip().lower()
    if choice == "s":
        cache[key] = {"hash": current_hash}
        _save_cache(root, cache)
    if choice == "a":
        return True
    return False


def _record_hash(root: Path, key: str, source: Path) -> None:
    cache = _load_cache(root)
    cache[key] = {"hash": _compute_hash(source), "ignore": False}
    _save_cache(root, cache)


# ---------------------------------------------------------------------------
# Worktree drift detection
# ---------------------------------------------------------------------------

@dataclass
class DriftResult:
    alias: str
    spec: BranchSpec
    actual_branch: str | None  # None = detached

    @property
    def is_drifted(self) -> bool:
        if self.spec.is_detached:
            return self.actual_branch is not None
        return self.actual_branch != self.spec.local_branch

    @property
    def message(self) -> str:
        if self.spec.is_detached:
            expected = f"detached at {self.spec.base_ref}"
        else:
            expected = f"branch {self.spec.local_branch}"
        if self.actual_branch is None:
            actual = "detached HEAD"
        else:
            actual = f"branch {self.actual_branch}"
        return f"{self.alias}: expected {expected}, found {actual}"


def check_drift(worktree_path: Path, spec: BranchSpec, alias: str) -> DriftResult:
    actual_branch = get_worktree_branch(worktree_path)
    return DriftResult(alias=alias, spec=spec, actual_branch=actual_branch)


def assert_no_drift(ws: WorkspaceConfig, ws_dir: Path) -> None:
    drifted = []
    for alias, spec in ws.repos.items():
        worktree_path = ws_dir / alias
        if not worktree_path.exists():
            continue
        result = check_drift(worktree_path, spec, alias)
        if result.is_drifted:
            drifted.append(result)
    if drifted:
        print("Drift detected — config does not match worktree state:", file=sys.stderr)
        for d in drifted:
            print(f"  {d.message}", file=sys.stderr)
        print("Run 'ow apply' to reconcile.", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def _parse_github_org_repo(url: str) -> str | None:
    if url.startswith("git@github.com:"):
        path = url[len("git@github.com:"):]
        return path.removesuffix(".git")
    if "github.com/" in url:
        path = url.split("github.com/", 1)[1]
        return path.removesuffix(".git")
    return None


def _osc8(url: str, text: str) -> str:
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"


def _c(text: str, *codes: int) -> str:
    prefix = "".join(f"\x1b[{code}m" for code in codes)
    return f"{prefix}{text}\x1b[0m"


def _counts(behind: int, ahead: int) -> str:
    b = _c(f"↓{behind}", 33) if behind > 0 else _c(f"↓{behind}", 2)
    a = _c(f"↑{ahead}", 32) if ahead > 0 else _c(f"↑{ahead}", 2)
    return f"{b} {a}"


def _github_tree_url(org_repo: str, branch: str) -> str:
    return f"https://github.com/{org_repo}/tree/{branch}"


def _github_commit_url(org_repo: str, full_hash: str) -> str:
    return f"https://github.com/{org_repo}/commit/{full_hash}"


def _get_pr_info(org_repo: str, branch: str) -> tuple[int, str] | None:
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", org_repo, "--head", branch,
             "--json", "number,url", "--limit", "1"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if not data:
            return None
        pr = data[0]
        return (pr["number"], pr["url"])
    except Exception:
        return None


def _link(url: str | None, text: str) -> str:
    return _osc8(url, text) if url else text


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_apply(config: Config, name: str | None = None) -> None:
    bare_repos_dir = config.root_dir / ".bare-git-repos"
    workspaces = config.workspaces
    if name:
        workspaces = [ws for ws in workspaces if ws.name == name]

    # Initialize template directory if absent
    template_dir = config.root_dir / "workspaces" / ".template"
    template_init_dir = config.root_dir / "workspaces" / ".template.init"
    if _check_source_drift(config.root_dir, "workspaces/.template.init", template_init_dir):
        sys.exit(1)
    if not template_dir.exists():
        shutil.copytree(template_init_dir, template_dir)
    _record_hash(config.root_dir, "workspaces/.template.init", template_init_dir)

    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )

    for ws in workspaces:
        ws_dir = config.root_dir / "workspaces" / ws.name

        # 1. Ensure bare repos + refs (parallel, max 2)
        resolved_specs: dict[str, BranchSpec] = {}

        def make_setup_task(alias, spec):
            def task():
                alias_remotes = config.remotes.get(alias, {})
                bare_repo = bare_repos_dir / f"{alias}.git"
                ensure_bare_repo(alias, alias_remotes, bare_repos_dir)
                resolved_specs[alias] = resolve_spec(bare_repo, spec, alias_remotes)
            return task

        tasks = [make_setup_task(alias, spec) for alias, spec in ws.repos.items()]
        parallel_fetch(tasks, max_workers=2)

        # 2. Create worktrees if they don't exist
        for alias, resolved in resolved_specs.items():
            bare_repo = bare_repos_dir / f"{alias}.git"
            worktree_path = ws_dir / alias
            if not worktree_exists(bare_repo, worktree_path):
                run_cmd(["git", "-C", str(bare_repo), "worktree", "prune"], check=True)
                ws_dir.mkdir(parents=True, exist_ok=True)
                create_worktree(bare_repo, worktree_path, resolved)
            else:
                currently_detached = worktree_is_detached(worktree_path)
                if currently_detached and not resolved.is_detached:
                    attach_worktree(bare_repo, worktree_path, resolved)
                elif not currently_detached and resolved.is_detached:
                    detach_worktree(worktree_path, resolved.base_ref)
                elif not resolved.is_detached:
                    # Already on the right branch — ensure upstream config is current
                    _set_branch_upstream(bare_repo, resolved.local_branch, resolved.remote, resolved.branch)

        # 3. Render templates and copy statics
        ws_dir.mkdir(parents=True, exist_ok=True)
        context = build_template_context(ws, config, ws_dir)
        paths = template_dir.rglob("*")
        file_paths = filter(lambda p: p.is_file(), paths)

        for path in sorted(file_paths):
            rel = path.relative_to(template_dir)
            if path.suffix == ".j2":
                out_path = ws_dir / rel.with_suffix("")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(env.get_template(str(rel)).render(context))
            else:
                out_path = ws_dir / rel
                out_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, out_path)

        # 4. Trust the generated mise file
        run_cmd(["mise", "trust", str(ws_dir / "mise.toml")], check=True)


def cmd_remove(config: Config, name: str) -> None:
    workspaces = [ws for ws in config.workspaces if ws.name == name]
    if not workspaces:
        print(f"No workspace named '{name}'", file=sys.stderr)
        sys.exit(1)

    ws = workspaces[0]
    bare_repos_dir = config.root_dir / ".bare-git-repos"
    ws_dir = config.root_dir / "workspaces" / name

    assert_no_drift(ws, ws_dir)

    for alias, spec in ws.repos.items():
        bare_repo = bare_repos_dir / f"{alias}.git"
        worktree_path = ws_dir / alias
        if worktree_path.exists():
            remove_worktree(bare_repo, worktree_path, spec.local_branch)

    if ws_dir.exists():
        shutil.rmtree(ws_dir)

    config_path = config.root_dir / "ow.toml"
    archive_workspace(config_path, ws)
    remaining = [w for w in config.workspaces if w.name != name]
    update_config_workspaces(config_path, remaining)


def cmd_status(config: Config, name: str | None = None) -> None:
    workspaces = config.workspaces
    if name:
        workspaces = [ws for ws in workspaces if ws.name == name]

    for ws in workspaces:
        ws_dir = config.root_dir / "workspaces" / ws.name
        bare_repos_dir = config.root_dir / ".bare-git-repos"

        # 1. Drift check
        assert_no_drift(ws, ws_dir)

        # 2. Parallel fetch (silent)
        def make_status_fetch_task(alias, spec):
            def task():
                alias_remotes = config.remotes.get(alias, {})
                bare_repo = bare_repos_dir / f"{alias}.git"
                worktree_path = ws_dir / alias
                if not worktree_path.exists():
                    return
                # Fetch track branch
                try:
                    resolved = resolve_spec_local(bare_repo, spec, alias_remotes)
                    subprocess.run(
                        ["git", "-C", str(bare_repo), "fetch", resolved.remote,
                         f"{resolved.branch}:refs/remotes/{resolved.remote}/{resolved.branch}"],
                        capture_output=True,
                    )
                except (RuntimeError, subprocess.CalledProcessError):
                    pass
                # If attached: fetch upstream too
                if not spec.is_detached:
                    upstream = get_upstream(worktree_path)
                    if upstream:
                        parts = upstream.split("/", 1)
                        if len(parts) == 2:
                            subprocess.run(
                                ["git", "-C", str(bare_repo), "fetch",
                                 parts[0], f"{parts[1]}:refs/remotes/{upstream}"],
                                capture_output=True,
                            )
            return task

        fetch_tasks = [make_status_fetch_task(alias, spec) for alias, spec in ws.repos.items()]
        parallel_fetch(fetch_tasks, max_workers=2)

        # 3. Display
        print(_c(f"[{ws.name}]", 1, 36))
        print("    " + _c("branches", 2))

        first_attached_branch: str | None = None
        pr_links: list[tuple[str, int, str]] = []
        max_alias_len = max(len(a) for a in ws.repos)

        for alias, spec in ws.repos.items():
            padding = " " * (max_alias_len - len(alias) + 1)
            worktree_path = ws_dir / alias
            if not worktree_path.exists():
                print(f"        {alias}:{padding}{_c('(not applied)', 2)}")
                continue

            alias_remotes = config.remotes.get(alias, {})

            bare_repo = bare_repos_dir / f"{alias}.git"
            try:
                spec = resolve_spec_local(bare_repo, spec, alias_remotes)
            except (RuntimeError, subprocess.CalledProcessError):
                pass  # keep original spec; git error caught in the outer try/except

            # Helper: get org/repo for a given remote name
            def org_repo_for(remote_name: str) -> str | None:
                rc = alias_remotes.get(remote_name)
                if rc:
                    return _parse_github_org_repo(rc.url)
                url = get_remote_url(bare_repo, remote_name)
                return _parse_github_org_repo(url) if url else None

            try:
                if spec.is_detached:
                    ahead, behind = get_rev_list_count(worktree_path, "HEAD", spec.base_ref)
                    short_hash, full_hash = get_worktree_head(worktree_path)

                    base_org_repo = org_repo_for(spec.remote)
                    base_url = _github_tree_url(base_org_repo, spec.branch) if base_org_repo else None
                    commit_url = _github_commit_url(base_org_repo, full_hash) if base_org_repo else None

                    base_text = _link(base_url, _c(spec.base_ref, 1))
                    hash_text = _link(commit_url, _c(short_hash, 33))
                    status = f"{base_text} {_counts(behind, ahead)} ({_c('DETACHED', 33)}: {hash_text})"

                else:
                    if first_attached_branch is None:
                        first_attached_branch = spec.local_branch

                    remote_ref = get_remote_ref_for_branch(
                        bare_repo, spec.local_branch, alias_remotes,
                        exclude_ref=spec.base_ref, base_remote=spec.remote,
                    )
                    if remote_ref:
                        # Branch found on a configured remote — Case 1 display + PR detection
                        ahead_up, behind_up = get_rev_list_count(worktree_path, "HEAD", remote_ref)
                        up_parts = remote_ref.split("/", 1)
                        up_remote = up_parts[0]
                        up_branch = up_parts[1]
                        up_org_repo = org_repo_for(up_remote)
                        up_url = _github_tree_url(up_org_repo, up_branch) if up_org_repo else None

                        if up_remote != spec.remote:
                            base_org_repo = org_repo_for(spec.remote)
                            fork_user = up_org_repo.split("/")[0] if up_org_repo else None
                            head_filter = f"{fork_user}:{up_branch}" if fork_user else up_branch
                            if base_org_repo:
                                pr_info = _get_pr_info(base_org_repo, head_filter)
                                if not pr_info:
                                    # Branch may be mirrored to fork but PR is on base repo directly
                                    pr_info = _get_pr_info(base_org_repo, up_branch)
                                if pr_info:
                                    pr_links.append((base_org_repo, pr_info[0], pr_info[1]))
                        elif up_org_repo:
                            pr_info = _get_pr_info(up_org_repo, up_branch)
                            if pr_info:
                                pr_links.append((up_org_repo, pr_info[0], pr_info[1]))

                        ahead_base, behind_base = get_rev_list_count(worktree_path, remote_ref, spec.base_ref)
                        base_org_repo = org_repo_for(spec.remote)
                        base_url = _github_tree_url(base_org_repo, spec.branch) if base_org_repo else None
                        display_text = _link(up_url, _c(remote_ref, 1))
                        base_text = _link(base_url, _c(spec.base_ref, 1))
                        status = f"{display_text} {_counts(behind_up, ahead_up)} ({base_text} {_counts(behind_base, ahead_base)})"
                    else:
                        upstream = get_upstream(worktree_path)
                        if upstream:
                            ahead_up, behind_up = get_rev_list_count(worktree_path, "HEAD", upstream)

                            up_parts = upstream.split("/", 1)
                            up_remote = up_parts[0] if len(up_parts) == 2 else "origin"
                            up_branch = up_parts[1] if len(up_parts) == 2 else upstream
                            up_org_repo = org_repo_for(up_remote)
                            up_url = _github_tree_url(up_org_repo, up_branch) if up_org_repo else None

                            if up_remote != spec.remote:
                                # Branch is on a fork — PR lives in the base (origin) repo
                                base_org_repo = org_repo_for(spec.remote)
                                fork_user = up_org_repo.split("/")[0] if up_org_repo else None
                                head_filter = f"{fork_user}:{up_branch}" if fork_user else up_branch
                                if base_org_repo:
                                    pr_info = _get_pr_info(base_org_repo, head_filter)
                                    if not pr_info:
                                        # Branch may be mirrored to fork but PR is on base repo directly
                                        pr_info = _get_pr_info(base_org_repo, up_branch)
                                    if pr_info:
                                        pr_links.append((base_org_repo, pr_info[0], pr_info[1]))
                            elif up_org_repo:
                                pr_info = _get_pr_info(up_org_repo, up_branch)
                                if pr_info:
                                    pr_links.append((up_org_repo, pr_info[0], pr_info[1]))

                            if upstream != spec.base_ref:
                                # Case 1: upstream ≠ base — standard format
                                ahead_base, behind_base = get_rev_list_count(worktree_path, upstream, spec.base_ref)
                                base_org_repo = org_repo_for(spec.remote)
                                base_url = _github_tree_url(base_org_repo, spec.branch) if base_org_repo else None
                                display_text = _link(up_url, _c(upstream, 1))
                                base_text = _link(base_url, _c(spec.base_ref, 1))
                                status = f"{display_text} {_counts(behind_up, ahead_up)} ({base_text} {_counts(behind_base, ahead_base)})"
                            else:
                                # Case 2: upstream == base — show local branch as primary
                                upstream_text = _link(up_url, _c(upstream, 1))
                                status = f"{_c(spec.local_branch, 1)} {_c('(local)', 2)} ({upstream_text} {_counts(behind_up, ahead_up)})"

                        else:
                            # Case 3: no upstream
                            base_org_repo = org_repo_for(spec.remote)
                            base_url = _github_tree_url(base_org_repo, spec.branch) if base_org_repo else None
                            ahead_base, behind_base = get_rev_list_count(worktree_path, "HEAD", spec.base_ref)
                            base_text = _link(base_url, _c(spec.base_ref, 1))
                            status = f"{_c(spec.local_branch, 1)} {_c('(local)', 2)} ({base_text} {_counts(behind_base, ahead_base)})"

            except subprocess.CalledProcessError:
                status = _c("(error)", 31)

            print(f"        {alias}:{padding}{status}")

        if pr_links or first_attached_branch:
            print("    " + _c("links", 2))
            for org_repo, pr_num, pr_url in pr_links:
                pr_text = _osc8(pr_url, f"{org_repo}#{pr_num}")
                print(f"        pr:     {pr_text}")

            if first_attached_branch:
                runbot_url = f"https://runbot.odoo.com/runbot/bundle/{first_attached_branch}"
                runbot_text = _osc8(runbot_url, first_attached_branch)
                print(f"        runbot: {runbot_text}")

        print()


def cmd_create(config: Config, name: str, specs: list[str]) -> None:
    repos = {}
    ws_vars: dict[str, Any] = {}
    for s in specs:
        if s.startswith("vars.") and "=" in s:
            k, v = s[len("vars."):].split("=", 1)
            ws_vars[k] = v
        else:
            alias, spec = s.split(":", 1)
            repos[alias] = parse_branch_spec(spec)

    ws = WorkspaceConfig(name=name, repos=repos, vars=ws_vars)
    config.workspaces.append(ws)

    config_path = config.root_dir / "ow.toml"
    with open(config_path, "a") as f:
        f.write("\n" + format_workspace(ws))

    cmd_apply(config, name=name)


def _report_conflict(alias: str, worktree_path: Path, onto_ref: str) -> None:
    print(f"\n  {_c('CONFLICT', 31)} in {_c(alias, 1)} rebasing onto {onto_ref}", file=sys.stderr)
    print("    resolve conflicts, then:", file=sys.stderr)
    print(f"      cd {worktree_path}", file=sys.stderr)
    print("      git rebase --continue", file=sys.stderr)
    print("    or abort:", file=sys.stderr)
    print("      git rebase --abort\n", file=sys.stderr)


def cmd_rebase(config: Config, name: str) -> None:
    bare_repos_dir = config.root_dir / ".bare-git-repos"
    workspaces = [ws for ws in config.workspaces if ws.name == name]
    if not workspaces:
        print(f"No workspace named '{name}'", file=sys.stderr)
        sys.exit(1)

    for ws in workspaces:
        ws_dir = config.root_dir / "workspaces" / ws.name

        # 1. Drift check
        assert_no_drift(ws, ws_dir)

        # 2. Parallel: resolve + fetch track branch (and upstream if applicable)
        resolved_tracks: dict[str, str] = {}
        resolved_upstreams: dict[str, str] = {}

        def make_fetch_task(alias, spec):
            def task():
                alias_remotes = config.remotes.get(alias, {})
                bare_repo = bare_repos_dir / f"{alias}.git"
                # Resolve the track (base) branch — force detached so resolve_spec
                # finds the base branch, not the pushed work branch
                track_spec = BranchSpec(spec.base_ref)
                resolved_track = resolve_spec(bare_repo, track_spec, alias_remotes)
                # Fetch latest
                run_cmd(
                    ["git", "-C", str(bare_repo), "fetch", resolved_track.remote,
                     f"{resolved_track.branch}:refs/remotes/{resolved_track.remote}/{resolved_track.branch}"],
                    check=True,
                )
                resolved_tracks[alias] = resolved_track.base_ref
                # If attached: resolve the full spec to find the pushed work branch
                if not spec.is_detached:
                    resolved_full = resolve_spec(bare_repo, spec, alias_remotes)
                    if resolved_full.base_ref != resolved_track.base_ref:
                        # Work branch found on a remote — fetch latest as upstream
                        run_cmd(
                            ["git", "-C", str(bare_repo), "fetch", resolved_full.remote,
                             f"{resolved_full.branch}:refs/remotes/{resolved_full.remote}/{resolved_full.branch}"],
                            check=True,
                        )
                        resolved_upstreams[alias] = resolved_full.base_ref
            return task

        fetch_tasks = [make_fetch_task(alias, spec) for alias, spec in ws.repos.items()]
        parallel_fetch(fetch_tasks, max_workers=2)

        # 3. Sequential rebases
        failed = []
        for alias, spec in ws.repos.items():
            worktree_path = ws_dir / alias
            if not worktree_path.exists():
                continue

            track_ref = resolved_tracks[alias]

            if spec.is_detached:
                run_cmd(
                    ["git", "-C", str(worktree_path), "switch", "--detach", track_ref],
                    check=True,
                )
            else:
                upstream = resolved_upstreams.get(alias)

                # Step 1: rebase onto upstream (pushed work branch on remote)
                if upstream:
                    result = run_cmd(
                        ["git", "-C", str(worktree_path), "rebase", upstream],
                    )
                    if result.returncode != 0:
                        _report_conflict(alias, worktree_path, upstream)
                        failed.append(alias)
                        continue

                # Step 2: rebase onto track branch
                result = run_cmd(
                    ["git", "-C", str(worktree_path), "rebase", track_ref],
                )
                if result.returncode != 0:
                    _report_conflict(alias, worktree_path, track_ref)
                    failed.append(alias)

        if failed:
            sys.exit(1)
