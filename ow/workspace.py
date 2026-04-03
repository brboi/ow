from __future__ import annotations

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
    git,
    git_cherry_pick,
    git_fetch,
    git_log_oneline,
    git_merge_base_fork_point,
    git_rebase,
    git_reset_hard,
    git_rev_list,
    git_switch,
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


def warn_if_drifted(ws: WorkspaceConfig | None, ws_dir: Path) -> None:
    """Display warnings for drift or missing config; never exit."""
    # Find actual repos in workspace
    actual_repos = {}
    for child in ws_dir.iterdir():
        if child.is_dir() and (child / ".git").exists():
            actual_repos[child.name] = get_worktree_branch(child)

    if ws is None:
        # No config in ow.toml - warn about deduced state
        print(f"Warning: workspace '{ws_dir.name}' not in ow.toml", file=sys.stderr)
        print("  Actual state:", file=sys.stderr)
        for alias, branch in sorted(actual_repos.items()):
            if branch:
                print(f"    {alias}: branch {branch}", file=sys.stderr)
            else:
                print(f"    {alias}: detached HEAD", file=sys.stderr)
        return

    # Check for drift
    drifted = []
    for alias, spec in ws.repos.items():
        worktree_path = ws_dir / alias
        if not worktree_path.exists():
            continue
        result = check_drift(worktree_path, spec, alias)
        if result.is_drifted:
            drifted.append(result)

    if drifted:
        print("Warning: drift detected between ow.toml and worktree state:", file=sys.stderr)
        for d in drifted:
            print(f"  {d.message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------


def _parse_github_org_repo(url: str) -> str | None:
    if url.startswith("git@github.com:"):
        path = url[len("git@github.com:") :]
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


_spinner_chars = ['|', '/', '-', '\\']
_spinner_idx = 0

def _spinner() -> str:
    global _spinner_idx
    c = _spinner_chars[_spinner_idx]
    _spinner_idx = (_spinner_idx + 1) % len(_spinner_chars)
    return c


def _format_git_cmd(alias: str, cmd: str, args: list[str]) -> str:
    return f"  [{alias}.git] {cmd} {' '.join(args)}"


def _print_spinner(prefix: str, spinner: str) -> None:
    line = f"{prefix}  {spinner}  "
    sys.stdout.write(f"\r{line}")
    sys.stdout.flush()


def _clear_spinner_line(prefix: str) -> None:
    line_len = len(prefix) + 4
    sys.stdout.write(f"\r{' ' * line_len}\r")
    sys.stdout.flush()


def _print_git_result(alias: str, cmd: str, args: list[str], ok: bool, error: str | None = None) -> None:
    line = _format_git_cmd(alias, cmd, args)
    if ok:
        print(f"{line}  ✓")
    else:
        print(f"{line}  ✗", file=sys.stderr)
        if error:
            print(f"  Error: {error}", file=sys.stderr)


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
            [
                "gh",
                "pr",
                "list",
                "--repo",
                org_repo,
                "--head",
                branch,
                "--json",
                "number,url",
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=15,
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


def _deduce_workspace_state(ws_dir: Path, config: Config) -> WorkspaceConfig:
    """Deduce workspace state from actual worktrees for workspaces not in ow.toml."""
    repos = {}
    for child in ws_dir.iterdir():
        if not child.is_dir() or not (child / ".git").exists():
            continue
        alias = child.name
        branch = get_worktree_branch(child)
        upstream = get_upstream(child)

        if upstream:
            # Use upstream as base_ref, create local branch spec
            remote, remote_branch = upstream.split("/", 1)
            base_ref = upstream
            local_branch = branch if branch else remote_branch
        else:
            # No upstream - cannot determine base_ref, use detached state
            base_ref = "HEAD"  # Placeholder, will show as error in status
            local_branch = branch  # May be None if detached

        repos[alias] = BranchSpec(base_ref, local_branch)

    return WorkspaceConfig(name=ws_dir.name, repos=repos, templates=[])


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_apply(config: Config, name: str | None = None) -> None:
    bare_repos_dir = config.root_dir / ".bare-git-repos"
    workspaces = config.workspaces
    if name:
        workspaces = [ws for ws in workspaces if ws.name == name]

    templates_root = config.root_dir / "templates"

    for ws in workspaces:
        ws_dir = config.root_dir / "workspaces" / ws.name
        is_new = not ws_dir.exists()

        # 1. Ensure bare repos + refs (sequential, with spinner)
        resolved_specs: dict[str, BranchSpec] = {}

        for alias, spec in ws.repos.items():
            _print_spinner(f"Setting up {alias}", _spinner())

            alias_remotes = config.remotes.get(alias, {})
            bare_repo = bare_repos_dir / f"{alias}.git"

            try:
                ensure_bare_repo(alias, alias_remotes, bare_repos_dir)
                resolved = resolve_spec(bare_repo, spec, alias_remotes)
                resolved_specs[alias] = resolved
                _clear_spinner_line(f"Setting up {alias}")
                _print_git_result(alias, "setup", [], True)
            except Exception as e:
                _clear_spinner_line(f"Setting up {alias}")
                _print_git_result(alias, "setup", [], False, str(e))
                resolved_specs[alias] = spec  # fallback

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
                    _set_branch_upstream(
                        bare_repo,
                        resolved.local_branch,
                        resolved.remote,
                        resolved.branch,
                    )

        # 3. Apply templates in order (later templates override earlier ones)
        ws_dir.mkdir(parents=True, exist_ok=True)
        context = build_template_context(ws, config, ws_dir)

        for template_name in ws.templates:
            template_dir = templates_root / template_name
            if not template_dir.exists():
                print(f"Error: template '{template_name}' not found at {template_dir}", file=sys.stderr)
                sys.exit(1)

            env = Environment(
                loader=FileSystemLoader(str(template_dir)),
                keep_trailing_newline=True,
                trim_blocks=True,
                lstrip_blocks=True,
            )

            paths = template_dir.rglob("*")
            file_paths = [p for p in paths if p.is_file()]

            for path in sorted(file_paths):
                rel = path.relative_to(template_dir)
                if path.suffix == ".j2":
                    out_path = ws_dir / rel.with_suffix("")
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(env.get_template(str(rel)).render(context))
                    out_path.chmod(path.stat().st_mode)
                else:
                    out_path = ws_dir / rel
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, out_path)

        # 4. Trust the generated mise file (only for new workspaces)
        if is_new:
            run_cmd(["mise", "trust", str(ws_dir / "mise.toml")], check=True)
            print(f"\nWorkspace '{ws.name}' created. To install dependencies:")
            print(f"    cd workspaces/{ws.name} && mise install")


def cmd_remove(config: Config, name: str) -> None:
    workspaces = [ws for ws in config.workspaces if ws.name == name]
    bare_repos_dir = config.root_dir / ".bare-git-repos"
    ws_dir = config.root_dir / "workspaces" / name

    if not workspaces:
        if not ws_dir.exists():
            print(f"No workspace named '{name}'", file=sys.stderr)
            sys.exit(1)
        # Workspace exists but not in ow.toml - remove anyway
        warn_if_drifted(None, ws_dir)
        ws = None  # No config, so no need to update ow.toml
    else:
        ws = workspaces[0]
        warn_if_drifted(ws, ws_dir)

    # Remove all worktrees in ws_dir
    if ws_dir.exists():
        for child in ws_dir.iterdir():
            if child.is_dir() and (child / ".git").exists():
                alias = child.name
                bare_repo = bare_repos_dir / f"{alias}.git"
                branch = get_worktree_branch(child) if ws else None
                spec = ws.repos.get(alias) if ws else None
                local_branch = spec.local_branch if spec else branch
                remove_worktree(bare_repo, child, local_branch)
        shutil.rmtree(ws_dir)

    # Update ow.toml only if workspace was in config
    if ws:
        config_path = config.root_dir / "ow.toml"
        archive_workspace(config_path, ws)
        remaining = [w for w in config.workspaces if w.name != name]
        update_config_workspaces(config_path, remaining)


def cmd_status(config: Config, name: str | None = None) -> None:
    workspaces = config.workspaces
    if name:
        workspaces = [ws for ws in workspaces if ws.name == name]
        if not workspaces:
            # Workspace not in ow.toml - check if it exists
            ws_dir = config.root_dir / "workspaces" / name
            if ws_dir.exists():
                ws = _deduce_workspace_state(ws_dir, config)
                warn_if_drifted(None, ws_dir)
                workspaces = [ws]
            else:
                print(f"No workspace named '{name}'", file=sys.stderr)
                sys.exit(1)

    for ws in workspaces:
        ws_dir = config.root_dir / "workspaces" / ws.name
        bare_repos_dir = config.root_dir / ".bare-git-repos"

        # 1. Drift warning (no longer blocks)
        warn_if_drifted(ws, ws_dir)

        # 2. Fetch (sequential, with spinner feedback)
        for alias, spec in ws.repos.items():
            worktree_path = ws_dir / alias
            if not worktree_path.exists():
                continue

            _print_spinner(f"Checking {alias}", _spinner())

            # Fetch track branch
            try:
                alias_remotes = config.remotes.get(alias, {})
                bare_repo = bare_repos_dir / f"{alias}.git"
                resolved = resolve_spec_local(bare_repo, spec, alias_remotes)
                refspec = f"{resolved.branch}:refs/remotes/{resolved.remote}/{resolved.branch}"
                _print_spinner(f"Checking {alias}", _spinner())
                result = subprocess.run(
                    ["git", "-C", str(bare_repo), "fetch", resolved.remote, refspec],
                    capture_output=True,
                )
                if result.returncode != 0:
                    err = result.stderr.decode().strip() if result.stderr else "unknown"
                    _clear_spinner_line(f"Checking {alias}")
                    _print_git_result(alias, "fetch", [resolved.remote, refspec], False, err)
                else:
                    _clear_spinner_line(f"Checking {alias}")
                    _print_git_result(alias, "fetch", [resolved.remote, refspec], True)
            except (RuntimeError, subprocess.CalledProcessError) as e:
                _clear_spinner_line(f"Checking {alias}")
                _print_git_result(alias, "fetch", ["?"], False, str(e))

            # If attached: fetch upstream too
            if not spec.is_detached:
                upstream = get_upstream(worktree_path)
                if upstream:
                    parts = upstream.split("/", 1)
                    if len(parts) == 2:
                        _print_spinner(f"Checking {alias}", _spinner())
                        result = subprocess.run(
                            ["git", "-C", str(bare_repo), "fetch", parts[0], f"{parts[1]}:refs/remotes/{upstream}"],
                            capture_output=True,
                        )
                        if result.returncode != 0:
                            err = result.stderr.decode().strip() if result.stderr else "unknown"
                            _clear_spinner_line(f"Checking {alias}")
                            _print_git_result(alias, "fetch", [parts[0], f"{parts[1]}:refs/remotes/{upstream}"], False, err)
                        else:
                            _clear_spinner_line(f"Checking {alias}")
                            _print_git_result(alias, "fetch", [parts[0], f"{parts[1]}:refs/remotes/{upstream}"], True)

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
                    ahead, behind = get_rev_list_count(
                        worktree_path, "HEAD", spec.base_ref
                    )
                    short_hash, full_hash = get_worktree_head(worktree_path)

                    base_org_repo = org_repo_for(spec.remote)
                    base_url = (
                        _github_tree_url(base_org_repo, spec.branch)
                        if base_org_repo
                        else None
                    )
                    commit_url = (
                        _github_commit_url(base_org_repo, full_hash)
                        if base_org_repo
                        else None
                    )

                    base_text = _link(base_url, _c(spec.base_ref, 1))
                    hash_text = _link(commit_url, _c(short_hash, 33))
                    status = f"{base_text} {_counts(behind, ahead)} ({_c('DETACHED', 33)}: {hash_text})"

                else:
                    if first_attached_branch is None:
                        first_attached_branch = spec.local_branch

                    remote_ref = get_remote_ref_for_branch(
                        bare_repo,
                        spec.local_branch,
                        alias_remotes,
                        exclude_ref=spec.base_ref,
                        base_remote=spec.remote,
                    )
                    if remote_ref:
                        # Branch found on a configured remote — Case 1 display + PR detection
                        ahead_up, behind_up = get_rev_list_count(
                            worktree_path, "HEAD", remote_ref
                        )
                        up_parts = remote_ref.split("/", 1)
                        up_remote = up_parts[0]
                        up_branch = up_parts[1]
                        up_org_repo = org_repo_for(up_remote)
                        up_url = (
                            _github_tree_url(up_org_repo, up_branch)
                            if up_org_repo
                            else None
                        )

                        if up_remote != spec.remote:
                            base_org_repo = org_repo_for(spec.remote)
                            fork_user = (
                                up_org_repo.split("/")[0] if up_org_repo else None
                            )
                            head_filter = (
                                f"{fork_user}:{up_branch}" if fork_user else up_branch
                            )
                            if base_org_repo:
                                pr_info = _get_pr_info(base_org_repo, head_filter)
                                if not pr_info:
                                    # Branch may be mirrored to fork but PR is on base repo directly
                                    pr_info = _get_pr_info(base_org_repo, up_branch)
                                if pr_info:
                                    pr_links.append(
                                        (base_org_repo, pr_info[0], pr_info[1])
                                    )
                        elif up_org_repo:
                            pr_info = _get_pr_info(up_org_repo, up_branch)
                            if pr_info:
                                pr_links.append((up_org_repo, pr_info[0], pr_info[1]))

                        ahead_base, behind_base = get_rev_list_count(
                            worktree_path, remote_ref, spec.base_ref
                        )
                        base_org_repo = org_repo_for(spec.remote)
                        base_url = (
                            _github_tree_url(base_org_repo, spec.branch)
                            if base_org_repo
                            else None
                        )
                        display_text = _link(up_url, _c(remote_ref, 1))
                        base_text = _link(base_url, _c(spec.base_ref, 1))
                        status = f"{display_text} {_counts(behind_up, ahead_up)} ({base_text} {_counts(behind_base, ahead_base)})"
                    else:
                        upstream = get_upstream(worktree_path)
                        if upstream:
                            ahead_up, behind_up = get_rev_list_count(
                                worktree_path, "HEAD", upstream
                            )

                            up_parts = upstream.split("/", 1)
                            up_remote = up_parts[0] if len(up_parts) == 2 else "origin"
                            up_branch = up_parts[1] if len(up_parts) == 2 else upstream
                            up_org_repo = org_repo_for(up_remote)
                            up_url = (
                                _github_tree_url(up_org_repo, up_branch)
                                if up_org_repo
                                else None
                            )

                            if up_remote != spec.remote:
                                # Branch is on a fork — PR lives in the base (origin) repo
                                base_org_repo = org_repo_for(spec.remote)
                                fork_user = (
                                    up_org_repo.split("/")[0] if up_org_repo else None
                                )
                                head_filter = (
                                    f"{fork_user}:{up_branch}"
                                    if fork_user
                                    else up_branch
                                )
                                if base_org_repo:
                                    pr_info = _get_pr_info(base_org_repo, head_filter)
                                    if not pr_info:
                                        # Branch may be mirrored to fork but PR is on base repo directly
                                        pr_info = _get_pr_info(base_org_repo, up_branch)
                                    if pr_info:
                                        pr_links.append(
                                            (base_org_repo, pr_info[0], pr_info[1])
                                        )
                            elif up_org_repo:
                                pr_info = _get_pr_info(up_org_repo, up_branch)
                                if pr_info:
                                    pr_links.append(
                                        (up_org_repo, pr_info[0], pr_info[1])
                                    )

                            if upstream != spec.base_ref:
                                # Case 1: upstream ≠ base — standard format
                                ahead_base, behind_base = get_rev_list_count(
                                    worktree_path, upstream, spec.base_ref
                                )
                                base_org_repo = org_repo_for(spec.remote)
                                base_url = (
                                    _github_tree_url(base_org_repo, spec.branch)
                                    if base_org_repo
                                    else None
                                )
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
                            base_url = (
                                _github_tree_url(base_org_repo, spec.branch)
                                if base_org_repo
                                else None
                            )
                            ahead_base, behind_base = get_rev_list_count(
                                worktree_path, "HEAD", spec.base_ref
                            )
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
                runbot_url = (
                    f"https://runbot.odoo.com/runbot/bundle/{first_attached_branch}"
                )
                runbot_text = _osc8(runbot_url, first_attached_branch)
                print(f"        runbot: {runbot_text}")

        print()


def cmd_create(config: Config, name: str, specs: list[str]) -> None:
    repos = {}
    ws_vars: dict[str, Any] = {}
    templates: list[str] = []
    for s in specs:
        if s.startswith("vars.") and "=" in s:
            k, v = s[len("vars.") :].split("=", 1)
            ws_vars[k] = v
        elif s.startswith("templates="):
            templates = s[len("templates=") :].split(",")
        else:
            alias, spec = s.split(":", 1)
            repos[alias] = parse_branch_spec(spec)

    if not templates:
        print("Error: templates must be specified, e.g. templates=common,vscode", file=sys.stderr)
        sys.exit(1)

    ws = WorkspaceConfig(name=name, repos=repos, templates=templates, vars=ws_vars)
    config.workspaces.append(ws)

    config_path = config.root_dir / "ow.toml"
    with open(config_path, "a") as f:
        f.write("\n" + format_workspace(ws))

    cmd_apply(config, name=name)


def _report_conflict(alias: str, worktree_path: Path, onto_ref: str) -> None:
    print(
        f"\n  {_c('CONFLICT', 31)} in {_c(alias, 1)} rebasing onto {onto_ref}",
        file=sys.stderr,
    )
    print("    resolve conflicts, then:", file=sys.stderr)
    print(f"      cd {worktree_path}", file=sys.stderr)
    print("      git rebase --continue", file=sys.stderr)
    print("    or abort:", file=sys.stderr)
    print("      git rebase --abort\n", file=sys.stderr)


@dataclass
class RebasePlan:
    alias: str
    track_ref: str
    upstream: str | None
    is_detached: bool
    local_commits: int
    unpushed_commits: int
    fork_point: str | None
    commits_to_reapply: list[str]
    upstream_rewritten: bool
    has_conflicts: bool


def _analyze_repo_for_rebase(
    worktree: Path, track_ref: str, upstream: str | None, alias: str, is_detached: bool
) -> RebasePlan:
    rebase_merge = worktree / ".git" / "rebase-merge"
    has_conflicts = rebase_merge.exists()

    local_commits, _ = get_rev_list_count(worktree, "HEAD", upstream or track_ref)

    fork_point = None
    commits_to_reapply: list[str] = []
    upstream_rewritten = False
    unpushed_commits = 0

    if upstream:
        unpushed_commits, _ = get_rev_list_count(worktree, "HEAD", upstream)
        branch = get_worktree_branch(worktree)
        if branch:
            fork_point = git_merge_base_fork_point(worktree, upstream, branch)
        if fork_point:
            commits_to_reapply = git_rev_list(worktree, f"{fork_point}..HEAD", reverse=True)
        upstream_rewritten = fork_point is None and unpushed_commits > 0

    return RebasePlan(
        alias=alias,
        track_ref=track_ref,
        upstream=upstream,
        is_detached=is_detached,
        local_commits=local_commits,
        unpushed_commits=unpushed_commits,
        fork_point=fork_point,
        commits_to_reapply=commits_to_reapply,
        upstream_rewritten=upstream_rewritten,
        has_conflicts=has_conflicts,
    )


def _display_rebase_summary(plans: list[RebasePlan]) -> None:
    for p in plans:
        parts = [p.track_ref]
        if p.upstream:
            parts.append(f"← {p.upstream}")
        parts.append(f"({p.local_commits} commits)")

        markers = []
        if p.upstream_rewritten:
            if p.fork_point:
                markers.append(_c("rewritten, recoverable", 33))
            else:
                markers.append(_c("rewritten, no fork-point", 31))
        elif p.unpushed_commits > 0 and p.upstream:
            markers.append(_c(f"{p.unpushed_commits} unpushed", 33))
        if p.has_conflicts:
            markers.append(_c("in progress", 31))
        if markers:
            parts.append("[" + ", ".join(markers) + "]")

        print(f"  {p.alias}: {' → '.join(parts)}")


def _recover_with_cherry_pick(worktree: Path, upstream: str, commits: list[str]) -> str | None:
    """Reset hard to upstream and cherry-pick commits.
    
    Returns None on success, or the failing commit hash on conflict.
    """
    git_reset_hard(worktree, upstream)
    
    for i, commit in enumerate(commits, 1):
        msg = git_log_oneline(worktree, commit)
        print(f"    Cherry-picking {i}/{len(commits)}: {msg}")
        result = git_cherry_pick(worktree, commit)
        if result.returncode != 0:
            return commit
    
    return None


def _do_rebase(worktree: Path, upstream: str | None, track_ref: str) -> bool:
    if upstream:
        result = git_rebase(worktree, upstream)
        if result.returncode != 0:
            return False
    result = git_rebase(worktree, track_ref)
    return result.returncode == 0


def cmd_rebase(config: Config, name: str) -> None:
    bare_repos_dir = config.root_dir / ".bare-git-repos"
    workspaces = [ws for ws in config.workspaces if ws.name == name]
    if not workspaces:
        ws_dir = config.root_dir / "workspaces" / name
        if ws_dir.exists():
            ws = _deduce_workspace_state(ws_dir, config)
            warn_if_drifted(None, ws_dir)
            workspaces = [ws]
        else:
            print(f"No workspace named '{name}'", file=sys.stderr)
            sys.exit(1)

    for ws in workspaces:
        ws_dir = config.root_dir / "workspaces" / ws.name

        warn_if_drifted(ws, ws_dir)

        resolved_tracks: dict[str, str] = {}
        resolved_upstreams: dict[str, str] = {}

        for alias, spec in ws.repos.items():
            _print_spinner(f"Preparing {alias}", _spinner())

            alias_remotes = config.remotes.get(alias, {})
            bare_repo = bare_repos_dir / f"{alias}.git"
            track_spec = BranchSpec(spec.base_ref)

            try:
                resolved_track = resolve_spec(bare_repo, track_spec, alias_remotes)
                refspec = f"{resolved_track.branch}:refs/remotes/{resolved_track.remote}/{resolved_track.branch}"
                _print_spinner(f"Preparing {alias}", _spinner())
                result = subprocess.run(
                    ["git", "-C", str(bare_repo), "fetch", resolved_track.remote, refspec],
                    capture_output=True,
                )
                if result.returncode != 0:
                    err = result.stderr.decode().strip() if result.stderr else "unknown"
                    _clear_spinner_line(f"Preparing {alias}")
                    _print_git_result(alias, "fetch", [resolved_track.remote, refspec], False, err)
                    resolved_tracks[alias] = resolved_track.base_ref
                else:
                    _clear_spinner_line(f"Preparing {alias}")
                    _print_git_result(alias, "fetch", [resolved_track.remote, refspec], True)
                    resolved_tracks[alias] = resolved_track.base_ref

                if not spec.is_detached:
                    resolved_full = resolve_spec(bare_repo, spec, alias_remotes)
                    if resolved_full.base_ref != resolved_track.base_ref:
                        _print_spinner(f"Preparing {alias}", _spinner())
                        full_refspec = f"{resolved_full.branch}:refs/remotes/{resolved_full.remote}/{resolved_full.branch}"
                        result = subprocess.run(
                            ["git", "-C", str(bare_repo), "fetch", "-f", resolved_full.remote, full_refspec],
                            capture_output=True,
                        )
                        if result.returncode != 0:
                            err = result.stderr.decode().strip() if result.stderr else "unknown"
                            _clear_spinner_line(f"Preparing {alias}")
                            _print_git_result(alias, "fetch", [resolved_full.remote, full_refspec], False, err)
                        else:
                            _clear_spinner_line(f"Preparing {alias}")
                            _print_git_result(alias, "fetch", [resolved_full.remote, full_refspec], True)
                        resolved_upstreams[alias] = resolved_full.base_ref
            except Exception as e:
                _clear_spinner_line(f"Preparing {alias}")
                _print_git_result(alias, "fetch", ["?"], False, str(e))
                resolved_tracks[alias] = spec.base_ref

        plans: list[RebasePlan] = []
        for alias, spec in ws.repos.items():
            worktree = ws_dir / alias
            if not worktree.exists():
                continue
            track_ref = resolved_tracks[alias]
            upstream = resolved_upstreams.get(alias)
            plans.append(_analyze_repo_for_rebase(worktree, track_ref, upstream, alias, spec.is_detached))

        if not plans:
            continue

        print(_c(f"[{ws.name}]", 1, 36))
        _display_rebase_summary(plans)

        has_rewritten_no_fork = any(
            p.upstream_rewritten and p.fork_point is None
            for p in plans
        )
        if has_rewritten_no_fork:
            error_label = _c("Error:", 31)
            print(f"\n  {error_label} Cannot recover some repos - fork-point not found.", file=sys.stderr)
            print("  Manual recovery required:", file=sys.stderr)
            for p in plans:
                if p.upstream_rewritten and p.fork_point is None:
                    print(f"    {p.alias}:", file=sys.stderr)
                    print("      git reflog HEAD | head -20  # find previous state", file=sys.stderr)
                    print("      git cherry-pick <commit>...  # manually reapply", file=sys.stderr)
            print()

        has_recoverable = any(
            p.upstream_rewritten and p.fork_point is not None
            for p in plans
        )
        if has_recoverable:
            recovery_label = _c("Recovery:", 33)
            print(f"\n  {recovery_label} reset --hard + cherry-pick for rewritten upstreams", file=sys.stderr)
            for p in plans:
                if p.upstream_rewritten and p.fork_point:
                    print(f"    {p.alias}: {len(p.commits_to_reapply)} commits to reapply", file=sys.stderr)

        has_warnings = any(
            p.unpushed_commits > 0 and p.upstream and not p.upstream_rewritten
            for p in plans
        )
        if has_warnings:
            warning_label = _c("Warning:", 33)
            print(f"\n  {warning_label} unpushed commits may cause conflicts", file=sys.stderr)

        try:
            response = input("\nProceed? [Y/n] ")
        except EOFError:
            response = ""

        if response.lower() == "n":
            print("Aborted.")
            return

        failed = []
        for plan in plans:
            worktree = ws_dir / plan.alias

            if plan.has_conflicts:
                print(
                    f"  Skipping {plan.alias}: rebase already in progress",
                    file=sys.stderr,
                )
                continue

            if plan.upstream_rewritten and plan.fork_point is None:
                print(
                    f"  Skipping {plan.alias}: no fork-point, manual recovery required",
                    file=sys.stderr,
                )
                continue

            print(f"  {plan.alias}:")

            if plan.is_detached:
                git_switch(worktree, plan.track_ref, detach=True, check=True)
                print("    Done (detached).")
            elif plan.upstream_rewritten and plan.fork_point and plan.upstream:
                failed_commit = _recover_with_cherry_pick(
                    worktree, plan.upstream, plan.commits_to_reapply
                )
                if failed_commit:
                    print(
                        f"\n    {_c('CONFLICT', 31)} cherry-picking {failed_commit}",
                        file=sys.stderr,
                    )
                    print("    resolve conflicts, then:", file=sys.stderr)
                    print(f"      cd {worktree}", file=sys.stderr)
                    print("      git cherry-pick --continue", file=sys.stderr)
                    print("    or abort:", file=sys.stderr)
                    print("      git cherry-pick --abort\n", file=sys.stderr)
                    failed.append(plan.alias)
                else:
                    print("    Done (recovered).")
            else:
                if not _do_rebase(worktree, plan.upstream, plan.track_ref):
                    _report_conflict(
                        plan.alias, worktree, plan.upstream or plan.track_ref
                    )
                    failed.append(plan.alias)
                else:
                    print("    Done.")

        if failed:
            sys.exit(1)
