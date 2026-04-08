import shutil
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from ow.utils.display import Spinner, _print_git_result
from ow.utils.config import Config, WorkspaceConfig
from ow.utils.git import (
    attach_worktree,
    create_worktree,
    detach_worktree,
    ensure_bare_repo,
    parallel_per_repo,
    resolve_spec,
    run_cmd,
    set_branch_upstream,
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
    """Return list of packaged template names from ow/_static/templates/."""
    try:
        from importlib.resources import files
        pkg_templates = files("ow") / "_static" / "templates"
        return sorted(d.name for d in pkg_templates.iterdir() if d.is_dir())
    except Exception:
        return []


def available_templates(config: Config) -> list[str]:
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
        pkg_dir = files("ow") / "_static" / "templates" / template_name
        if pkg_dir.is_dir():
            return pkg_dir
    except Exception:
        pass

    raise FileNotFoundError(f"Template '{template_name}' not found in local or packaged templates")


# ---------------------------------------------------------------------------
# Template application helpers (shared between cmd_create and cmd_update)
# ---------------------------------------------------------------------------


def apply_templates(ws: WorkspaceConfig, config: Config, ws_dir: Path) -> None:
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


def ensure_workspace_materialized(ws: WorkspaceConfig, config: Config, ws_dir: Path) -> tuple[Path, set[str], dict[str, str]]:
    """Ensure bare repos exist, refs are fetched, and worktrees are created.

    Returns (workspace directory path, set of successfully materialized aliases, dict of alias -> error message for failures).
    """
    bare_repos_dir = config.root_dir / ".bare-git-repos"
    ws_dir.mkdir(parents=True, exist_ok=True)

    resolved_specs: dict[str, Any] = {}
    successful: set[str] = set()
    errors: dict[str, str] = {}

    def _setup_alias(alias: str, spec) -> Any:
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
                set_branch_upstream(
                    bare_repo,
                    resolved.local_branch,
                    resolved.remote,
                    resolved.branch,
                )

    return ws_dir, successful, errors
