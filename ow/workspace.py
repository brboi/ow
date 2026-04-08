from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import questionary
from jinja2 import Environment, FileSystemLoader

from ow.config import (
    BranchSpec,
    Config,
    WorkspaceConfig,
    load_workspace_config,
    write_workspace_config,
    parse_branch_spec,
)
from ow.git import (
    _set_branch_upstream,
    attach_worktree,
    create_worktree,
    detach_worktree,
    ensure_bare_repo,
    get_all_remote_refs,
    get_remote_ref_for_branch,
    get_remote_url,
    get_rev_list_count,
    get_upstream,
    get_worktree_branch,
    get_worktree_head,
    git_cherry_pick,
    git_log_oneline,
    git_merge_base_fork_point,
    git_rebase,
    git_reset_hard,
    git_rev_list,
    git_switch,
    parallel_per_repo,
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
    """Detect if a repo is the main Odoo source (has odoo-bin)."""
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

    if any((child / "__manifest__.py").exists() for child in children):
        return [path]

    result: list[Path] = []
    for child in children:
        result.extend(find_addon_paths(child))

    return sorted(result)


def build_template_context(ws: WorkspaceConfig, config: Config, ws_dir: Path) -> dict:
    """Build Jinja2 template context for a workspace."""
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
        "ws_name": ws_dir.name,
        "main_repo_alias": main_repo_alias,
        "repos": list(ws.repos.keys()),
        "vars": {**config.vars, **ws.vars},
        "addons_paths": addons_paths + main_addons_paths,
        "odools_path_items": odools_path_items + odools_main_items,
    }


# ---------------------------------------------------------------------------
# Template resolution (hybrid: local + packaged)
# ---------------------------------------------------------------------------


def _get_packaged_templates() -> list[str]:
    """Return list of packaged template names from ow/templates/."""
    try:
        from importlib.resources import files
        pkg_templates = files("ow") / "templates"
        return sorted(d.name for d in pkg_templates.iterdir() if d.is_dir())
    except Exception:
        return []


def _available_templates(config: Config) -> list[str]:
    """Return sorted list of available template names (local + packaged).

    Local templates (./templates/) take priority and can override packaged ones.
    Packaged templates are used as fallback.
    """
    local_templates_dir = config.root_dir / "templates"
    local_names = set()
    if local_templates_dir.exists():
        local_names = set(d.name for d in local_templates_dir.iterdir() if d.is_dir())

    packaged_names = set(_get_packaged_templates())

    return sorted(local_names | packaged_names)


def _resolve_template_dir(template_name: str, config: Config) -> Path:
    """Resolve template directory: local first, fallback to packaged."""
    local_dir = config.root_dir / "templates" / template_name
    if local_dir.exists():
        return local_dir

    try:
        from importlib.resources import files
        pkg_dir = files("ow") / "templates" / template_name
        if pkg_dir.is_dir():
            return pkg_dir
    except Exception:
        pass

    raise FileNotFoundError(f"Template '{template_name}' not found in local or packaged templates")


def _find_ow_config(start: Path) -> Path | None:
    """Walk up from start looking for .ow/config."""
    for parent in [start] + list(start.parents):
        candidate = parent / ".ow" / "config"
        if candidate.exists():
            return candidate
    return None


def resolve_workspace(config: Config, name: str | None = None) -> tuple[Path, WorkspaceConfig]:
    """Resolve workspace from name, env var, or cwd walk-up.

    Returns (workspace_dir_path, WorkspaceConfig).
    """
    config_file = None
    if name is not None:
        ws_dir = config.root_dir / "workspaces" / name
        if not ws_dir.exists():
            print(f"Workspace '{name}' not found", file=sys.stderr)
            sys.exit(1)
        config_file = ws_dir / ".ow" / "config"
        if not config_file.exists():
            print(f"Workspace '{name}' is not a valid workspace (missing .ow/config)", file=sys.stderr)
            sys.exit(1)
    elif os.environ.get("OW_WORKSPACE"):
        env_val = os.environ["OW_WORKSPACE"]
        ws_dir = config.root_dir / "workspaces" / env_val
        if (ws_dir / ".ow" / "config").exists():
            config_file = ws_dir / ".ow" / "config"
        else:
            config_file = Path(env_val) / ".ow" / "config"
    else:
        config_file = _find_ow_config(Path.cwd())

    if not config_file or not config_file.exists():
        print("No workspace found. Run from a workspace or pass a path.", file=sys.stderr)
        sys.exit(1)

    ws_dir = config_file.parent.parent.resolve()
    return ws_dir, load_workspace_config(config_file)


# ---------------------------------------------------------------------------
# Template application helpers (shared between cmd_create and cmd_update)
# ---------------------------------------------------------------------------


def _apply_templates(ws: WorkspaceConfig, config: Config, ws_dir: Path) -> None:
    """Apply templates in order to ws_dir (later templates override earlier ones)."""
    context = build_template_context(ws, config, ws_dir)

    for template_name in ws.templates:
        template_dir = _resolve_template_dir(template_name, config)

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


def _ensure_workspace_materialized(ws: WorkspaceConfig, config: Config, ws_dir: Path) -> tuple[Path, set[str], dict[str, str]]:
    """Ensure bare repos exist, refs are fetched, and worktrees are created.

    Returns (workspace directory path, set of successfully materialized aliases, dict of alias -> error message for failures).
    """
    bare_repos_dir = config.root_dir / ".bare-git-repos"
    ws_dir.mkdir(parents=True, exist_ok=True)

    resolved_specs: dict[str, BranchSpec] = {}
    successful: set[str] = set()
    errors: dict[str, str] = {}

    def _setup_alias(alias: str, spec: BranchSpec) -> BranchSpec:
        alias_remotes = config.remotes.get(alias, {})
        ensure_bare_repo(alias, alias_remotes, bare_repos_dir)
        return resolve_spec(bare_repos_dir / f"{alias}.git", spec, alias_remotes)

    tasks = {alias: (lambda a=alias, s=spec: _setup_alias(a, s)) for alias, spec in ws.repos.items()}

    with Spinner(f"Setting up {len(tasks)} repo(s)"):
        results = parallel_per_repo(tasks)

    for alias in ws.repos:
        result = results[alias]
        if isinstance(result, Exception):
            errors[alias] = str(result)
            _print_git_result(alias, "setup", [], False, str(result))
        else:
            resolved_specs[alias] = result
            successful.add(alias)
            _print_git_result(alias, "setup", [], True)

    for alias, resolved in resolved_specs.items():
        bare_repo = bare_repos_dir / f"{alias}.git"
        worktree_path = ws_dir / alias
        if not worktree_exists(bare_repo, worktree_path):
            run_cmd(["git", "-C", str(bare_repo), "worktree", "prune"], check=True, label=alias)
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

    return ws_dir, successful, errors


# ---------------------------------------------------------------------------
# Worktree drift detection
# ---------------------------------------------------------------------------


@dataclass
class DriftResult:
    """Result of checking worktree drift from config."""
    alias: str
    spec: BranchSpec
    actual_branch: str | None

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
    """Check if worktree state matches config spec."""
    actual_branch = get_worktree_branch(worktree_path)
    return DriftResult(alias=alias, spec=spec, actual_branch=actual_branch)


def warn_if_drifted(ws: WorkspaceConfig, ws_dir: Path) -> None:
    """Display warnings for drift; never exit."""
    drift_tasks: dict[str, Any] = {}
    for alias, spec in ws.repos.items():
        worktree_path = ws_dir / alias
        if not worktree_path.exists():
            continue
        drift_tasks[alias] = (lambda w=worktree_path, s=spec, a=alias: check_drift(w, s, a))

    if not drift_tasks:
        return

    drift_results = parallel_per_repo(drift_tasks)

    drifted = []
    for alias in ws.repos:
        result = drift_results.get(alias)
        if result and not isinstance(result, Exception) and result.is_drifted:
            drifted.append(result)

    if drifted:
        print("Warning: drift detected between config and worktree state:", file=sys.stderr)
        for d in drifted:
            print(f"  {d.message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _c(text: str, *codes: int) -> str:
    """Apply ANSI color codes to text."""
    prefix = "".join(f"\x1b[{code}m" for code in codes)
    return f"{prefix}{text}\x1b[0m"


class Spinner:
    _chars = ['|', '/', '-', '\\']

    def __init__(self, prefix: str):
        self._prefix = prefix
        self._idx = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _animate(self) -> None:
        while not self._stop_event.is_set():
            line = f"{self._prefix}  {self._chars[self._idx]}  "
            sys.stdout.write(f"\r{line}")
            sys.stdout.flush()
            self._idx = (self._idx + 1) % len(self._chars)
            self._stop_event.wait(0.1)

    def __enter__(self) -> "Spinner":
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join()
        line_len = len(self._prefix) + 4
        sys.stdout.write(f"\r{' ' * line_len}\r")
        sys.stdout.flush()


def _format_git_cmd(alias: str, cmd: str, args: list[str]) -> str:
    """Format a git command for display."""
    return f"  [{alias}] git {cmd} {' '.join(args)}"


def _print_git_result(alias: str, cmd: str, args: list[str], ok: bool, error: str | None = None) -> None:
    """Print git command result."""
    line = _format_git_cmd(alias, cmd, args)
    if ok:
        print(f"{line}  ✓")
    else:
        print(f"{line}  ✗", file=sys.stderr)
        if error:
            print(f"  Error: {error}", file=sys.stderr)


def _counts(behind: int, ahead: int) -> str:
    """Format behind/ahead counts with colors."""
    b = _c(f"↓{behind}", 33) if behind > 0 else _c(f"↓{behind}", 2)
    a = _c(f"↑{ahead}", 32) if ahead > 0 else _c(f"↑{ahead}", 2)
    return f"{b} {a}"


def _github_url_from_remote(remote_url: str) -> str | None:
    """Parse git remote URL to GitHub web URL.

    git@github.com:odoo/odoo.git → https://github.com/odoo/odoo
    https://github.com/odoo/odoo.git → https://github.com/odoo/odoo
    """
    ssh_match = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", remote_url)
    if ssh_match:
        return f"https://github.com/{ssh_match.group(1)}/{ssh_match.group(2)}"
    https_match = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", remote_url)
    if https_match:
        return f"https://github.com/{https_match.group(1)}/{https_match.group(2)}"
    return None


def _osc8(url: str, text: str) -> str:
    """Create an OSC8 hyperlink."""
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"


# ---------------------------------------------------------------------------
# Command: init
# ---------------------------------------------------------------------------


def _copy_packaged_templates(dest: Path) -> None:
    """Copy all packaged templates to destination directory."""
    from importlib.resources import files

    pkg_templates = files("ow") / "templates"
    dest.mkdir(parents=True, exist_ok=True)

    for template_dir in pkg_templates.iterdir():
        if not template_dir.is_dir():
            continue

        src_dir = pkg_templates / template_dir.name
        dst_dir = dest / template_dir.name
        dst_dir.mkdir(exist_ok=True)

        for src_file in src_dir.rglob("*"):
            if src_file.is_file():
                rel = src_file.relative_to(src_dir)
                dst_file = dst_dir / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)


def _copy_ow_services(dest: Path) -> None:
    """Copy ow-scoped services to destination directory."""
    from importlib.resources import files

    pkg_services = files("ow") / "services"
    dest.mkdir(parents=True, exist_ok=True)

    for src_file in pkg_services.iterdir():
        if src_file.is_file():
            shutil.copy2(src_file, dest / src_file.name)


def cmd_init(path: Path | None = None, *, force: bool = False, with_backup: bool = False) -> None:
    """Initialize a new ow project in the current directory.

    Creates:
    - ow.toml (minimal config with odoo/odoo repo)
    - workspaces/ (empty directory)
    - templates/ (copy of packaged templates)
    - mise.toml (ow-scoped tools: Python, Node, rtlcss)
    - services/compose.yml (Docker Compose example)

    Args:
        path: Target directory (default: current directory)
        force: Overwrite existing files without backup
        with_backup: Backup existing files before overwrite
    """
    target = path or Path.cwd()

    ow_toml = target / "ow.toml"
    templates_dir = target / "templates"
    workspaces_dir = target / "workspaces"
    mise_toml = target / "mise.toml"
    services_dir = target / "services"

    # Check if files already exist
    exists = []
    if ow_toml.exists():
        exists.append("ow.toml")
    if templates_dir.exists() and any(templates_dir.iterdir()):
        exists.append("templates/")
    if mise_toml.exists():
        exists.append("mise.toml")
    if services_dir.exists():
        exists.append("services/")

    if exists and not force and not with_backup:
        print(f"Error: existing files found: {', '.join(exists)}", file=sys.stderr)
        print("Use --force to overwrite without backup, or --force-with-backup to backup first.", file=sys.stderr)
        sys.exit(1)

    # Backup if requested
    if with_backup and exists:
        if ow_toml.exists():
            backup_path = target / "ow.toml.bak"
            shutil.copy2(ow_toml, backup_path)
            print(f"Backed up: ow.toml → ow.toml.bak")

        if templates_dir.exists() and any(templates_dir.iterdir()):
            backup_path = target / "templates.bak"
            if backup_path.exists():
                shutil.rmtree(backup_path)
            shutil.copytree(templates_dir, backup_path)
            print(f"Backed up: templates/ → templates.bak/")

        if mise_toml.exists():
            backup_path = target / "mise.toml.bak"
            shutil.copy2(mise_toml, backup_path)
            print(f"Backed up: mise.toml → mise.toml.bak")

        if services_dir.exists():
            backup_path = target / "services.bak"
            if backup_path.exists():
                shutil.rmtree(backup_path)
            shutil.copytree(services_dir, backup_path)
            print(f"Backed up: services/ → services.bak/")

    # Create directories
    workspaces_dir.mkdir(parents=True, exist_ok=True)
    templates_dir.mkdir(parents=True, exist_ok=True)
    services_dir.mkdir(parents=True, exist_ok=True)

    # Copy packaged templates (overwrite if exists)
    _copy_packaged_templates(templates_dir)
    print(f"Copied packaged templates to templates/")

    # Copy ow-scoped services
    _copy_ow_services(services_dir)
    print(f"Copied services to services/")

    # Create ow.toml (minimal config)
    ow_toml_content = '''[vars]
http_port = 8069
db_host = "localhost"
db_port = 5432
db_user = "odoo"
db_password = "odoo"
admin_passwd = "Password"
# smtp_server = "mailpit"
# smtp_port = 1025

[remotes.community]
origin.url = "git@github.com:odoo/odoo.git"
# dev.url = "git@github.com:odoo-dev/odoo.git"
# dev.pushurl = "git@github.com:odoo-dev/odoo.git"
# dev.fetch = "+refs/heads/*:refs/remotes/dev/*"

# [remotes.enterprise]
# origin.url = "git@github.com:odoo/enterprise.git"
# dev.url = "git@github.com:odoo-dev/enterprise.git"
# dev.pushurl = "git@github.com:odoo-dev/enterprise.git"
# dev.fetch = "+refs/heads/*:refs/remotes/dev/*"
'''
    ow_toml.write_text(ow_toml_content)
    print(f"Created: ow.toml")

    # Create mise.toml (ow-scoped)
    mise_toml_content = '''[tools]
python = "3.12"
node = { version = "latest", postinstall = "npm install -g rtlcss" }

[env]
COMPOSE_FILE = "{{config_root}}/services/compose.yml"
'''
    mise_toml.write_text(mise_toml_content)
    print(f"Created: mise.toml")

    print("\nProject initialized successfully!")
    print("\nNext steps:")
    print("  1. Edit ow.toml to add more remotes if needed")
    print("  2. Run: mise install")
    print("  3. Run: ow create  (to create your first workspace)")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cleanup_failed_workspace(ws_dir: Path) -> None:
    """Remove workspace directory if it's empty or contains only .ow/."""
    if not ws_dir.exists():
        return
    contents = list(ws_dir.iterdir())
    if not contents or contents == [ws_dir / ".ow"]:
        shutil.rmtree(ws_dir)


def _validate_create_inputs(
    config: Config,
    name: str | None,
    templates: list[str] | None,
    repos: dict[str, BranchSpec] | None,
    configuration: str | None,
) -> tuple[WorkspaceConfig | None, str, Path]:
    """Validate CLI inputs and resolve source workspace if duplicating.

    Returns (source_ws, resolved_name, ws_dir).
    Exits on validation errors.
    """
    templates_root = config.root_dir / "templates"
    if not templates_root.exists():
        print("Error: templates/ directory not found.", file=sys.stderr)
        sys.exit(1)
    available_templates = sorted(
        d.name
        for d in templates_root.iterdir()
        if d.is_dir()
    )
    if not available_templates:
        print("Error: no templates found in templates/", file=sys.stderr)
        sys.exit(1)

    if templates is not None:
        invalid = [t for t in templates if t not in available_templates]
        if invalid:
            avail = ", ".join(available_templates)
            print(f"Error: unknown template(s): {', '.join(invalid)}. Available: {avail}", file=sys.stderr)
            sys.exit(1)

    known_aliases = list(config.remotes.keys())
    if repos is not None:
        unknown = [a for a in repos if a not in known_aliases]
        if unknown:
            avail = ", ".join(known_aliases) if known_aliases else "(none configured)"
            print(f"Error: unknown repo alias(es): {', '.join(unknown)}. Available: {avail}", file=sys.stderr)
            sys.exit(1)

    source_ws: WorkspaceConfig | None = None
    if configuration is not None:
        src_path = Path(configuration)
        if src_path.is_dir():
            src_config_file = src_path / ".ow" / "config"
        else:
            src_config_file = src_path
        if not src_config_file.exists():
            print(f"Error: configuration file not found: {src_config_file}", file=sys.stderr)
            sys.exit(1)
        source_ws = load_workspace_config(src_config_file)

        available = _available_templates(config)
        invalid = [t for t in source_ws.templates if t not in available]
        if invalid:
            avail = ", ".join(available) if available else "(none found)"
            print(f"Error: configuration references unknown template(s): {', '.join(invalid)}. Available: {avail}", file=sys.stderr)
            sys.exit(1)

        for alias in source_ws.repos:
            if alias not in known_aliases:
                avail = ", ".join(known_aliases) if known_aliases else "(none configured)"
                print(f"Error: configuration references repo '{alias}' but it's not defined in ow.toml [remotes]", file=sys.stderr)
                print(f"  Available remotes: {avail}", file=sys.stderr)
                sys.exit(1)

    if name is not None:
        name = name.strip()
        if not name or not re.match(r'^[a-zA-Z0-9_-]+$', name):
            print("Error: name must be alphanumeric with hyphens and underscores only.", file=sys.stderr)
            sys.exit(1)
        ws_dir = config.root_dir / "workspaces" / name
        if ws_dir.exists():
            print(f"Error: workspace '{name}' already exists at {ws_dir}.", file=sys.stderr)
            sys.exit(1)
    else:
        while True:
            try:
                name = questionary.text("Workspace name").ask()
            except KeyboardInterrupt:
                print("\nAborted.", file=sys.stderr)
                sys.exit(1)
            if not name:
                print("Error: name is required.", file=sys.stderr)
                sys.exit(1)
            name = name.strip()
            if not name or not re.match(r'^[a-zA-Z0-9_-]+$', name):
                print("Error: name must be alphanumeric with hyphens and underscores only.", file=sys.stderr)
                continue
            ws_dir = config.root_dir / "workspaces" / name
            if ws_dir.exists():
                print(f"Warning: workspace '{name}' already exists at {ws_dir}. Choose another name.")
                continue
            break

    return source_ws, name, ws_dir


def _gather_workspace_config_interactive(
    config: Config,
    source_ws: WorkspaceConfig | None,
    templates: list[str] | None,
    repos: dict[str, BranchSpec] | None,
) -> WorkspaceConfig:
    """Run interactive questionnaire to build WorkspaceConfig.

    Pre-populates from source_ws or CLI args where available.
    """
    available_templates = sorted(
        d.name
        for d in (config.root_dir / "templates").iterdir()
        if d.is_dir()
    )
    known_aliases = list(config.remotes.keys())

    if source_ws is not None:
        pre_selected = set(source_ws.templates)
        if templates is not None:
            pre_selected = set(templates)
        final_repos: dict[str, BranchSpec] = dict(source_ws.repos)
        if repos is not None:
            final_repos.update(repos)
    else:
        pre_selected = set(templates) if templates else set()
        final_repos = dict(repos) if repos else {}

    _check_duplicate_branches(final_repos, config)

    try:
        selected_templates = questionary.checkbox(
            "Templates (space to select, enter to confirm)",
            choices=[questionary.Choice(t, checked=(t in pre_selected)) for t in available_templates],
        ).ask()
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(1)
    if not selected_templates:
        selected_templates = []

    if known_aliases:
        pre_selected_aliases = set(final_repos.keys())
        try:
            selected_aliases = questionary.checkbox(
                "Repos to include (space to select, enter to confirm)",
                choices=[questionary.Choice(a, checked=(a in pre_selected_aliases)) for a in known_aliases],
            ).ask()
        except KeyboardInterrupt:
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)
        if not selected_aliases:
            selected_aliases = []

        for alias in selected_aliases:
            if alias not in final_repos:
                try:
                    spec_str = questionary.text(
                        f"{alias} branch spec (e.g. master, master..my-feature)",
                    ).ask()
                except KeyboardInterrupt:
                    print("\nAborted.", file=sys.stderr)
                    sys.exit(1)
                if not spec_str:
                    print("Aborted.", file=sys.stderr)
                    sys.exit(1)
                try:
                    final_repos[alias] = parse_branch_spec(spec_str.strip())
                except ValueError as e:
                    print(f"Error: invalid branch spec '{spec_str.strip()}': {e}", file=sys.stderr)
                    sys.exit(1)

    _check_duplicate_branches(final_repos, config)

    ws_vars: dict[str, Any] = dict(source_ws.vars) if source_ws is not None else dict(config.vars)

    return WorkspaceConfig(repos=final_repos, templates=selected_templates, vars=ws_vars)


def cmd_create(
    config: Config,
    name: str | None = None,
    templates: list[str] | None = None,
    repos: dict[str, BranchSpec] | None = None,
    configuration: str | None = None,
) -> None:
    """Interactive workspace creation with questionary.

    Optional pre-populated values from CLI args:
      name: workspace name
      templates: list of template names to pre-select
      repos: dict of alias -> BranchSpec to pre-select
      configuration: path to existing workspace config to duplicate
    """
    # Phase 1: Validate inputs and resolve name
    source_ws, resolved_name, ws_dir = _validate_create_inputs(
        config, name, templates, repos, configuration
    )

    # Phase 2: Interactive questionnaire
    ws = _gather_workspace_config_interactive(config, source_ws, templates, repos)

    # Phase 3: Confirm
    print(f"\nWorkspace '{resolved_name}' will be created with:")
    print(f"  Templates: {', '.join(ws.templates)}")
    for alias, spec in ws.repos.items():
        print(f"  {alias}: {spec.to_spec_str()}")
    if ws.vars:
        print(f"  Vars: {ws.vars}")

    try:
        confirm = questionary.confirm("Proceed?").ask()
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(1)
    if not confirm:
        print("Aborted.")
        return

    ow_config_path = ws_dir / ".ow" / "config"
    if ow_config_path.exists():
        print(f"Error: workspace '{resolved_name}' already exists at workspaces/{resolved_name}", file=sys.stderr)
        sys.exit(1)

    # Phase 4: Materialize
    _, successful, errors = _ensure_workspace_materialized(ws, config, ws_dir)

    if errors:
        if len(errors) == len(ws.repos):
            _cleanup_failed_workspace(ws_dir)
            print(f"\nError: all repos failed to set up:", file=sys.stderr)
            for alias, err in errors.items():
                print(f"  {alias}: {err}", file=sys.stderr)
            sys.exit(1)

        print(f"\nWarning: some repos failed to set up:", file=sys.stderr)
        for alias, err in errors.items():
            print(f"  {alias}: {err}", file=sys.stderr)

    _apply_templates(ws, config, ws_dir)

    write_workspace_config(ow_config_path, ws)

    mise_toml = ws_dir / "mise.toml"
    if mise_toml.exists():
        run_cmd(["mise", "trust", str(mise_toml)], check=True)

    if errors:
        print(f"\nWorkspace '{resolved_name}' created with errors. Fix issues and run: ow update")
    else:
        print(f"\nWorkspace '{resolved_name}' created. To install dependencies:")
        print(f"    cd workspaces/{resolved_name} && mise install")
    print(f"\nWorkspace config: {ow_config_path}")
    print("Edit it to customize vars, then run: ow update")


def _check_duplicate_branches(new_repos: dict[str, BranchSpec], config: Config) -> None:
    """Abort if any repo alias shares the same local_branch as an existing workspace.

    Only local_branches (the part after `..`) are checked — source branches don't conflict
    since git only prevents two worktrees on the same local branch.
    """
    ws_root = config.root_dir / "workspaces"
    if not ws_root.exists():
        return
    for existing_ws_dir in sorted(ws_root.iterdir()):
        if not existing_ws_dir.is_dir():
            continue
        ow_config = existing_ws_dir / ".ow" / "config"
        if not ow_config.exists():
            continue
        existing = load_workspace_config(ow_config)
        for alias, new_spec in new_repos.items():
            if alias in existing.repos:
                existing_spec = existing.repos[alias]
                new_target = new_spec.local_branch
                existing_target = existing_spec.local_branch
                if new_target and existing_target and new_target == existing_target:
                    print(f"Error: workspace '{existing_ws_dir.name}' already uses {alias}:{existing_spec.to_spec_str()}", file=sys.stderr)
                    print(f"  Target branch '{new_target}' is already in use. Each target branch must be unique.", file=sys.stderr)
                    sys.exit(1)


def cmd_update(config: Config, workspace: str | None = None) -> None:
    """Re-render templates and materialize worktrees for the current workspace."""
    ws_dir, ws = resolve_workspace(config, name=workspace)
    _, successful, errors = _ensure_workspace_materialized(ws, config, ws_dir)
    _apply_templates(ws, config, ws_dir)

    if errors:
        print(f"\nWarning: repo(s) failed to set up:", file=sys.stderr)
        for alias, err in errors.items():
            print(f"  {alias}: {err}", file=sys.stderr)

    missing_vars = {k: v for k, v in config.vars.items() if k not in ws.vars}
    if missing_vars:
        ws.vars = {**ws.vars, **missing_vars}
        ow_config_path = ws_dir / ".ow" / "config"
        write_workspace_config(ow_config_path, ws)

    mise_toml = ws_dir / "mise.toml"
    if mise_toml.exists():
        run_cmd(["mise", "trust", str(mise_toml)], check=True)

    print(f"\nWorkspace '{ws_dir.name}' updated.")


class _PruneResult(NamedTuple):
    alias: str
    pruned_worktrees: bool
    deleted_branches: list[str]


def _prune_bare_repo(bare_repo: Path) -> _PruneResult:
    """Prune a single bare repo: clean worktrees and delete orphaned branches."""
    alias = bare_repo.stem
    pruned = False
    deleted: list[str] = []

    # 1. Worktree prune
    result = subprocess.run(
        ["git", "-C", str(bare_repo), "worktree", "prune"],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        pruned = True

    # 2. Delete local branches not attached to any worktree
    wt_result = subprocess.run(
        ["git", "-C", str(bare_repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    )
    used_branches: set[str] = set()
    if wt_result.returncode == 0:
        for line in wt_result.stdout.splitlines():
            if line.startswith("branch "):
                branch_ref = line.split(" ", 1)[1]
                if branch_ref.startswith("refs/heads/"):
                    used_branches.add(branch_ref[len("refs/heads/"):])

    branch_result = subprocess.run(
        ["git", "-C", str(bare_repo), "branch", "--list"],
        capture_output=True, text=True,
    )
    if branch_result.returncode == 0:
        all_branches = {b.strip().lstrip("*+ ") for b in branch_result.stdout.splitlines() if b.strip()}
        orphaned = all_branches - used_branches
        if orphaned:
            for branch in sorted(orphaned):
                subprocess.run(
                    ["git", "-C", str(bare_repo), "branch", "-D", branch],
                    capture_output=True, text=True,
                )
            deleted = sorted(orphaned)

    return _PruneResult(alias=alias, pruned_worktrees=pruned, deleted_branches=deleted)


def cmd_prune(config: Config) -> None:
    """Clean up stale worktree references and orphaned branches from bare repos."""
    bare_repos_dir = config.root_dir / ".bare-git-repos"
    if not bare_repos_dir.exists():
        print("No bare repos found.")
        return

    bare_repos = sorted(bare_repos_dir.glob("*.git"))
    if not bare_repos:
        print("No bare repos found.")
        return

    prune_tasks = {
        repo.stem: (lambda r=repo: _prune_bare_repo(r))
        for repo in bare_repos
    }
    prune_results = parallel_per_repo(prune_tasks)

    cleaned = False
    for repo in bare_repos:
        alias = repo.stem
        result = prune_results.get(alias)
        if isinstance(result, Exception):
            continue
        if result.pruned_worktrees:
            print(f"  [{alias}] pruned stale worktrees")
            cleaned = True
        if result.deleted_branches:
            print(f"  [{alias}] deleted orphaned branches: {', '.join(result.deleted_branches)}")
            cleaned = True

    if not cleaned:
        print("All bare repos are clean.")


def _display_detached_status(
    alias: str,
    spec: BranchSpec,
    resolved: BranchSpec,
    worktree_path: Path,
    max_alias_len: int,
) -> str:
    """Format status line for a detached worktree."""
    padding = " " * (max_alias_len - len(alias) + 1)
    ahead, behind = get_rev_list_count(worktree_path, "HEAD", resolved.base_ref)
    short_hash, _ = get_worktree_head(worktree_path)

    status = f"{_c(resolved.base_ref, 1)} {_counts(behind, ahead)} ({_c('DETACHED', 33)}: {short_hash})"
    return f"        {alias}:{padding}{status}"


def _display_attached_status(
    alias: str,
    spec: BranchSpec,
    resolved: BranchSpec,
    worktree_path: Path,
    max_alias_len: int,
    *,
    refs: set[str] | None = None,
) -> str:
    """Format status line for an attached worktree."""
    padding = " " * (max_alias_len - len(alias) + 1)

    remote_ref = get_remote_ref_for_branch(
        worktree_path,
        resolved.local_branch,
        {},
        exclude_ref=resolved.base_ref,
        base_remote=resolved.remote,
        refs=refs,
    )
    if remote_ref:
        ahead_up, behind_up = get_rev_list_count(worktree_path, "HEAD", remote_ref)
        ahead_base, behind_base = get_rev_list_count(worktree_path, remote_ref, resolved.base_ref)
        status = f"{_c(remote_ref, 1)} {_counts(behind_up, ahead_up)} ({_c(resolved.base_ref, 1)} {_counts(behind_base, ahead_base)})"
    else:
        upstream = get_upstream(worktree_path)
        if upstream:
            ahead_up, behind_up = get_rev_list_count(worktree_path, "HEAD", upstream)
            if upstream != resolved.base_ref:
                ahead_base, behind_base = get_rev_list_count(worktree_path, upstream, resolved.base_ref)
                status = f"{_c(upstream, 1)} {_counts(behind_up, ahead_up)} ({_c(resolved.base_ref, 1)} {_counts(behind_base, ahead_base)})"
            else:
                status = f"{_c(resolved.local_branch, 1)} {_c('(local)', 2)} ({_c(upstream, 1)} {_counts(behind_up, ahead_up)})"
        else:
            ahead_base, behind_base = get_rev_list_count(worktree_path, "HEAD", resolved.base_ref)
            status = f"{_c(resolved.local_branch, 1)} {_c('(local)', 2)} ({_c(resolved.base_ref, 1)} {_counts(behind_base, ahead_base)})"

    return f"        {alias}:{padding}{status}"


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


def _fetch_workspace_refs(
    ws: WorkspaceConfig,
    ws_dir: Path,
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


class _StatusResult(NamedTuple):
    status_line: str
    first_attached_branch: str | None
    github_link: tuple[str, str] | None


def _gather_repo_status(
    alias: str, spec: BranchSpec, resolved: BranchSpec,
    worktree_path: Path, bare_repo: Path, max_alias_len: int,
    refs: set[str],
) -> _StatusResult:
    """Gather all display data for one repo (runs in parallel)."""
    if resolved.is_detached:
        status_line = _display_detached_status(alias, spec, resolved, worktree_path, max_alias_len)
        short_hash, _ = get_worktree_head(worktree_path)
        remote_url = get_remote_url(bare_repo, resolved.remote)
        link = None
        if remote_url:
            github_base = _github_url_from_remote(remote_url)
            if github_base:
                link = (alias, f"{github_base}/commit/{short_hash}")
        return _StatusResult(status_line, None, link)
    else:
        status_line = _display_attached_status(
            alias, spec, resolved, worktree_path, max_alias_len, refs=refs,
        )
        remote_url = get_remote_url(bare_repo, resolved.remote)
        link = None
        if remote_url:
            github_base = _github_url_from_remote(remote_url)
            if github_base:
                link = (alias, f"{github_base}/tree/{resolved.local_branch}")
        return _StatusResult(status_line, resolved.local_branch, link)


def cmd_status(config: Config, workspace: str | None = None) -> None:
    """Show branch status for the current workspace."""
    ws_dir, ws = resolve_workspace(config, name=workspace)
    bare_repos_dir = config.root_dir / ".bare-git-repos"

    warn_if_drifted(ws, ws_dir)

    _, _, resolved_specs = _fetch_workspace_refs(ws, ws_dir, config, fetch_upstreams=True)

    print(_c(f"[{ws_dir.name}]", 1, 36))
    print("    " + _c("branches", 2))

    max_alias_len = max((len(a) for a in ws.repos), default=0)

    # Build parallel tasks for repos that exist and have resolved specs
    status_tasks: dict[str, Any] = {}
    for alias, spec in ws.repos.items():
        worktree_path = ws_dir / alias
        if not worktree_path.exists():
            continue
        resolved = resolved_specs.get(alias)
        if resolved is None:
            continue
        bare_repo = bare_repos_dir / f"{alias}.git"
        refs = get_all_remote_refs(bare_repo)
        status_tasks[alias] = (
            lambda a=alias, s=spec, r=resolved, w=worktree_path, b=bare_repo, rf=refs:
            _gather_repo_status(a, s, r, w, b, max_alias_len, rf)
        )

    if status_tasks:
        status_results = parallel_per_repo(status_tasks)
    else:
        status_results = {}

    # Print results in config order
    first_attached_branch: str | None = None
    github_links: list[tuple[str, str]] = []

    for alias, spec in ws.repos.items():
        padding = " " * (max_alias_len - len(alias) + 1)
        worktree_path = ws_dir / alias
        if not worktree_path.exists():
            print(f"        {alias}:{padding}{_c('(not applied)', 2)}")
            continue

        resolved = resolved_specs.get(alias)
        if resolved is None:
            print(f"        {alias}:{padding}{_c('(error: could not resolve)', 31)}")
            continue

        result = status_results.get(alias)
        if isinstance(result, Exception):
            print(f"        {alias}:{padding}{_c('(error)', 31)}")
            continue

        print(result.status_line)
        if first_attached_branch is None and result.first_attached_branch:
            first_attached_branch = result.first_attached_branch
        if result.github_link:
            github_links.append(result.github_link)

    print("    " + _c("links", 2))
    if first_attached_branch:
        runbot_url = f"https://runbot.odoo.com/runbot/bundle/{first_attached_branch}"
        runbot_text = _osc8(runbot_url, first_attached_branch)
        print(f"        runbot: {runbot_text}")
    for link_alias, link_url in github_links:
        link_padding = " " * (max_alias_len - len(link_alias) + 1)
        print(f"        {link_alias}:{link_padding}{_osc8(link_url, link_url)}")

    print()


def _report_conflict(alias: str, worktree_path: Path, onto_ref: str) -> None:
    """Print conflict resolution instructions."""
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
    """Plan for rebasing a single repo."""
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
    """Analyze the rebase situation for a single repo."""
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
    """Display rebase summary for all repos."""
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
    """Execute rebase onto upstream then track_ref. Returns True on success."""
    if upstream:
        result = git_rebase(worktree, upstream)
        if result.returncode != 0:
            return False
    result = git_rebase(worktree, track_ref)
    return result.returncode == 0


def cmd_rebase(config: Config, workspace: str | None = None) -> None:
    """Fetch and rebase all repos in the current workspace."""
    ws_dir, ws = resolve_workspace(config, name=workspace)

    warn_if_drifted(ws, ws_dir)

    resolved_tracks, resolved_upstreams, _ = _fetch_workspace_refs(
        ws, ws_dir, config, fetch_upstreams=True,
        resolve_fn=resolve_spec, spinner_prefix="Preparing",
    )

    # Parallelize rebase analysis
    analysis_tasks: dict[str, Any] = {}
    for alias, spec in ws.repos.items():
        worktree = ws_dir / alias
        if not worktree.exists():
            continue
        track_ref = resolved_tracks[alias]
        upstream = resolved_upstreams.get(alias)
        analysis_tasks[alias] = (
            lambda w=worktree, t=track_ref, u=upstream, a=alias, d=spec.is_detached:
            _analyze_repo_for_rebase(w, t, u, a, d)
        )

    if analysis_tasks:
        analysis_results = parallel_per_repo(analysis_tasks)
    else:
        analysis_results = {}

    plans: list[RebasePlan] = []
    for alias in ws.repos:
        result = analysis_results.get(alias)
        if result is not None and not isinstance(result, Exception):
            plans.append(result)

    if not plans:
        return

    print(_c(f"[{ws_dir.name}]", 1, 36))
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
