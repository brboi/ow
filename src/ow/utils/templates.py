import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ow.utils.display import print_git_result, task_progress
from ow.utils.config import Config, WorkspaceConfig
from ow.utils import paths
from ow.utils.git import (
    attach_worktree,
    create_worktree,
    get_worktree_branch,
    detach_worktree,
    ensure_bare_repo,
    in_progress_operation,
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


def find_addon_paths(path: Path, exclude: Iterable[Path] = ()) -> list[Path]:
    """Return addons_path directories found under path.

    Uses an iterative walk with a visited inode set to break symlink cycles.
    Prunes node_modules, __pycache__ and every hidden directory: an Odoo
    addon never lives inside .git, .venv, .odoo or .ow, and naming them one
    by one only postpones the next one. `exclude` names directories the
    caller handles itself and the walk must not enter.
    An addons_path is a directory whose immediate children include addon
    directories (directories containing __manifest__.py for Odoo >= 10, or
    __openerp__.py for Odoo < 10). A bare __init__.py is a Python package
    marker, not an addon marker: counting it made every Python subtree look
    like an addons_path. Stops descending once an addons_path is identified.
    Returns [] if path is not a directory or contains no addons.
    """
    if not path.is_dir():
        return []

    skip = {p.resolve() for p in exclude}
    result: list[Path] = []
    seen: set[int] = set()
    stack = [path]

    while stack:
        current = stack.pop()
        try:
            st = current.stat()
        except OSError:
            continue

        inode = st.st_ino
        if inode in seen:
            continue
        seen.add(inode)

        if not current.is_dir():
            continue

        # Prune noise directories. `path` itself is never pruned: a caller
        # asking about ~/ws/.hidden means it.
        if current != path and (
            current.name.startswith(".")
            or current.name in ("node_modules", "__pycache__")
        ):
            continue
        if current.resolve() in skip:
            continue

        # Get directory children
        try:
            children = [p for p in current.iterdir() if p.is_dir()]
        except OSError:
            continue

        # Check if this is an addons_path (children have manifests)
        has_manifest = any(
            (child / m).exists()
            for child in children
            for m in ("__manifest__.py", "__openerp__.py")
        )

        if has_manifest:
            result.append(current)
            continue  # don't descend into addon dirs

        # Descend into children
        stack.extend(children)

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
            found = find_addon_paths(repo_dir)
            addons_paths.extend(str(p) for p in found)
            for p in found:
                odools_path_items.append(str(p.relative_to(ws_dir)))

    # Addons that belong to no repo: a template bundle may ship one of its
    # own, and it lands next to the worktrees rather than inside them —
    # writing into a worktree would show up as a dirty git checkout (#42).
    # The alias directories are excluded because the loop above owns them.
    for p in find_addon_paths(ws_dir, exclude=[ws_dir / alias for alias in ws.repos]):
        addons_paths.append(str(p))
        odools_path_items.append(str(p.relative_to(ws_dir)))

    return {
        "ws_name": ws_dir.name,
        "ws_dir": str(ws_dir),
        "main_repo_alias": main_repo_alias,
        "repos": list(ws.repos.keys()),
        "vars": {**config.vars, **ws.vars},
        "addons_paths": addons_paths + main_addons_paths,
        "odools_path_items": odools_path_items + odools_main_items,
        "services_compose": str(paths.services_dir() / "compose.yml"),
        "volumes_dir": str(paths.volumes_dir()),
    }


# ---------------------------------------------------------------------------
# Template resolution (hybrid: local + packaged)
# ---------------------------------------------------------------------------


def _packaged_templates_dir() -> Path:
    """The template tree shipped inside the ow distribution."""
    from importlib.resources import files

    return files("ow") / "_static" / "templates"  # type: ignore[return-value]


def _get_packaged_templates() -> list[str]:
    """Names of the bundles ow ships.

    An unreadable tree means a broken install, not an ow that ships nothing:
    say so, then carry on with whatever the user has locally. Anything other
    than an OSError is a bug and travels on.
    """
    try:
        return sorted(d.name for d in _packaged_templates_dir().iterdir() if d.is_dir())
    except OSError as exc:
        print(
            f"Warning: ow's packaged templates are unreadable ({exc}); "
            "only your own templates are available.",
            file=sys.stderr,
        )
        return []


def available_templates() -> list[str]:
    """Return sorted list of available template names (local + packaged).

    Local templates take priority and can override packaged ones per file.
    """
    local_templates_dir = paths.templates_dir()
    local_names = set()
    if local_templates_dir.exists():
        local_names = set(d.name for d in local_templates_dir.iterdir() if d.is_dir())

    packaged_names = set(_get_packaged_templates())

    return sorted(local_names | packaged_names)


def _packaged_bundle(bundle: str) -> Path | None:
    """The packaged directory for `bundle`, or None if ow does not ship it."""
    path = _packaged_templates_dir() / bundle
    return path if path.is_dir() else None


def _files_under(root: Path | None) -> dict[Path, Path]:
    """Every file found by walking `root`, keyed by its path relative to root."""
    if root is None or not root.is_dir():
        return {}
    return {
        src.relative_to(root): src for src in sorted(root.rglob("*")) if src.is_file()
    }


def packaged_files(bundle: str) -> dict[str, Path]:
    """Every file the packaged bundle ships, keyed by its relative posix path."""
    return {
        rel.as_posix(): src for rel, src in _files_under(_packaged_bundle(bundle)).items()
    }


def resolve_template_files(bundle: str) -> dict[Path, Path]:
    """Every file of a bundle, local copy winning per file.

    Per file, not per bundle: taking one file must not silently drop the
    others. That is the difference between owning a file and forking a
    bundle.
    """
    files: dict[Path, Path] = {}
    for root in (_packaged_bundle(bundle), paths.templates_dir() / bundle):
        files.update(_files_under(root))
    return files


# ---------------------------------------------------------------------------
# Template application helpers (shared between cmd_init and cmd_apply)
# ---------------------------------------------------------------------------


def apply_templates(ws: WorkspaceConfig, config: Config, ws_dir: Path) -> None:
    """Apply templates in order to ws_dir (later templates override earlier ones).

    Rendered twice when the first pass changed what the addon scan can see: a
    template bundle may itself materialise an Odoo addon, and
    build_template_context reads the filesystem — so on the first pass that
    addon does not exist yet and never reaches addons_path (#42). The second
    pass is skipped whenever the rescan agrees with the first, which is the
    normal case.
    """
    context = build_template_context(ws, config, ws_dir)
    _render_bundles(ws, context, ws_dir)

    rescanned = build_template_context(ws, config, ws_dir)
    if any(
        rescanned[key] != context[key]
        for key in ("addons_paths", "odools_path_items")
    ):
        _render_bundles(ws, rescanned, ws_dir)


def _render_bundles(ws: WorkspaceConfig, context: dict, ws_dir: Path) -> None:
    """Render every bundle of `ws` into ws_dir against a fixed context.

    Every write here is a deterministic function of `context`, which is what
    lets apply_templates run this twice.
    """
    for template_name in ws.templates:
        files = resolve_template_files(template_name)
        local_dir = paths.templates_dir() / template_name
        if not files:
            packaged_dir = _packaged_bundle(template_name)
            existing_dir = next(
                (d for d in (local_dir, packaged_dir) if d is not None and d.is_dir()),
                None,
            )
            if existing_dir is not None:
                raise FileNotFoundError(
                    f"Template '{template_name}' found in {existing_dir} but it is empty"
                )
            raise FileNotFoundError(
                f"Template '{template_name}' not found in local or packaged templates"
            )

        # Local wins per file, but an include/extends/import inside a local
        # file must still be able to reach a packaged sibling. A search-path
        # loader (local first) resolves that per file, exactly like
        # resolve_template_files does for the non-Jinja operations below.
        search_path = [local_dir]
        packaged_dir = _packaged_bundle(template_name)
        if packaged_dir is not None:
            search_path.append(packaged_dir)
        env = Environment(
            loader=FileSystemLoader(search_path),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )

        # Detect output-path collisions before rendering.
        seen_outputs: dict[Path, str] = {}
        for rel, src in sorted(files.items()):
            out_rel = rel.with_suffix("") if src.suffix == ".j2" else rel
            if out_rel in seen_outputs:
                raise ValueError(
                    f"Template output collision: {rel} and {seen_outputs[out_rel]} "
                    f"both write to {out_rel}"
                )
            seen_outputs[out_rel] = str(rel)

        for rel, src in sorted(files.items()):
            if src.suffix == ".j2":
                out_path = ws_dir / rel.with_suffix("")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(env.get_template(rel.as_posix()).render(context), encoding="utf-8")
                out_path.chmod(src.stat().st_mode)
            else:
                out_path = ws_dir / rel
                out_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, out_path)


def ensure_services_compose() -> Path:
    """Render the bundled compose.yml.j2 into services_dir().

    Idempotent: skips if the file already exists and the template hasn't changed.
    """
    from importlib.resources import files

    src = files("ow") / "_static" / "services" / "compose.yml.j2"
    dst = paths.services_dir() / "compose.yml"
    dst.parent.mkdir(parents=True, exist_ok=True)

    template_text = src.read_text(encoding="utf-8")
    env = Environment(
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    rendered = env.from_string(template_text).render(
        volumes_dir=str(paths.volumes_dir()),
    )
    if dst.exists() and dst.read_text(encoding="utf-8") == rendered:
        return dst

    dst.write_text(rendered, encoding="utf-8")
    return dst


def ensure_workspace_materialized(ws: WorkspaceConfig, config: Config, ws_dir: Path) -> tuple[Path, set[str], dict[str, str]]:
    """Ensure bare repos exist, refs are fetched, and worktrees are created.

    Returns (workspace directory path, set of successfully materialized aliases, dict of alias -> error message for failures).
    """
    bare_repos_dir = paths.repos_dir()
    ws_dir.mkdir(parents=True, exist_ok=True)

    resolved_specs: dict[str, Any] = {}
    successful: set[str] = set()
    errors: dict[str, str] = {}

    def _setup_alias(alias: str, spec) -> Any:
        alias_remotes = config.remotes.get(alias, {})
        ensure_bare_repo(alias, alias_remotes, bare_repos_dir)
        return resolve_spec(bare_repos_dir / f"{alias}.git", spec, alias_remotes)

    tasks = {alias: (lambda a=alias, s=spec: _setup_alias(a, s)) for alias, spec in ws.repos.items()}

    with task_progress("Setting up repo(s)", len(tasks)) as advance:
        results = parallel_per_repo(tasks, on_done=lambda _alias: advance())

    for alias in ws.repos:
        result = results[alias]
        if isinstance(result, Exception):
            errors[alias] = str(result)
            print_git_result(alias, "setup", [], False, str(result))
        else:
            resolved_specs[alias] = result
            successful.add(alias)
            print_git_result(alias, "setup", [], True)

    for alias, resolved in resolved_specs.items():
        bare_repo = bare_repos_dir / f"{alias}.git"
        worktree_path = ws_dir / alias
        try:
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
                    current_branch = get_worktree_branch(worktree_path)
                    if current_branch != resolved.local_branch:
                        attach_worktree(bare_repo, worktree_path, resolved)
                    else:
                        set_branch_upstream(
                            bare_repo,
                            resolved.local_branch,
                            resolved.remote,
                            resolved.branch,
                        )
        except (OSError, subprocess.CalledProcessError) as exc:
            successful.discard(alias)
            busy = in_progress_operation(worktree_path)
            if busy is not None:
                operation, continue_cmd, abort_cmd = busy
                errors[alias] = (
                    f"{operation} in progress; finish with `{continue_cmd}` or abort with `{abort_cmd}`"
                )
            else:
                errors[alias] = str(exc)
            print_git_result(alias, "reconcile", [], False, errors[alias])

    return ws_dir, successful, errors
