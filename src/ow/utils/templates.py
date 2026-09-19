import hashlib
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import tomli_w
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
    main_repo_alias = odoo_main_alias(ws, ws_dir)

    repo_addons_paths: list[str] = []
    repo_odools_items: list[str] = []
    main_addons_paths: list[str] = []
    main_odools_items: list[str] = []

    for alias in ws.repos:
        repo_dir = ws_dir / alias
        if is_odoo_main_repo(repo_dir):
            main_addons_paths = [
                str(repo_dir / "addons"),
                str(repo_dir / "odoo" / "addons"),
            ]
            main_odools_items = [
                f"{alias}/addons",
                f"{alias}/odoo/addons",
            ]
        else:
            for p in find_addon_paths(repo_dir):
                repo_addons_paths.append(str(p))
                repo_odools_items.append(str(p.relative_to(ws_dir)))

    # Addons that belong to no repo: a template bundle may ship one of its
    # own, and it lands next to the worktrees rather than inside them —
    # writing into a worktree would show up as a dirty git checkout (#42).
    # The alias directories are excluded because the loop above owns them.
    loose = find_addon_paths(ws_dir, exclude=[ws_dir / alias for alias in ws.repos])
    # `.local` is where the bundled `local` template drops its addon, and the
    # walk above never descends into a hidden directory — a .venv or a .odoo
    # is not an addons_path, and naming them one by one only postpones the
    # next one. Handing `.local` over as a root is how it gets looked at at
    # all: find_addon_paths never prunes the root it was given.
    loose = find_addon_paths(ws_dir / ".local") + loose

    # Loose addons come first in the path: one exists to shadow the module it
    # replaces, and Odoo resolves a module from the first addons_path holding
    # it. The main repo comes last for the same reason, as it always has.
    return {
        "ws_name": ws_dir.name,
        "ws_dir": str(ws_dir),
        "main_repo_alias": main_repo_alias,
        "repos": list(ws.repos.keys()),
        "vars": dict(ws.vars),
        "addons_paths": [str(p) for p in loose] + repo_addons_paths + main_addons_paths,
        "odools_path_items": (
            [str(p.relative_to(ws_dir)) for p in loose]
            + repo_odools_items
            + main_odools_items
        ),
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


def bundle_source_files(bundle: str) -> dict[Path, Path]:
    """Every source file of a bundle, local copy winning per file.

    Per file, not per bundle: a local override of one file must not
    silently drop its packaged siblings. This is the source side of
    materialisation, used both to populate the workspace and to diff a
    materialised file against what ow would copy today.
    """
    files: dict[Path, Path] = {}
    for root in (_packaged_bundle(bundle), paths.templates_dir() / bundle):
        files.update(_files_under(root))
    return files


# ---------------------------------------------------------------------------
# Rendering (#45)
#
# Nothing is copied into the workspace: a bundle is rendered straight from
# its sources — the packaged tree, overridden per file by the user-local one
# — and `<ws>/.ow/rendered.lock.toml` records the sha256 of the *output* ow
# wrote. Locking the output rather than the template is what makes the file
# you actually open (odoorc, launch.json) the file ow protects: an output
# that no longer matches the lock is yours, and ow never writes it again.
# ---------------------------------------------------------------------------

RENDERED_LOCK = Path(".ow") / "rendered.lock.toml"

# The two bundles a workspace never declares: `common` is the floor every
# workspace stands on, `odoo` follows from what the repos turn out to be.
IMPLICIT_BUNDLE = "common"
ODOO_BUNDLE = "odoo"
_UNDECLARED = (IMPLICIT_BUNDLE, ODOO_BUNDLE)

_LOCK_HEADER = "# Managed by ow.\n"

UP_TO_DATE = "up to date"
OUTDATED = "outdated"
YOURS = "yours"
ABSENT = "absent"
NOT_RENDERED = "not rendered"


@dataclass
class RenderResult:
    """What apply_templates did, by workspace-relative path.

    `managed` is every path ow renders, whatever the verdict was — the four
    other lists only carry what changed hands this run, and a caller that
    needs to know which files ow speaks for (to trust a mise fragment, say)
    would otherwise have to render everything a second time to find out.
    """
    wrote: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    yours: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    managed: list[str] = field(default_factory=list)


@dataclass
class RenderedFile:
    """One output of this workspace's bundles, and how it compares today."""
    path: str
    state: str
    ow_text: str | None
    your_text: str | None


@dataclass
class _Output:
    """What a bundle wants at one path: bytes, or None to write nothing."""
    data: bytes | None
    mode: int


def odoo_main_alias(ws: WorkspaceConfig, ws_dir: Path) -> str | None:
    """The alias of this workspace's Odoo core checkout, or None."""
    return next(
        (alias for alias in ws.repos if is_odoo_main_repo(ws_dir / alias)),
        None,
    )


def is_odoo_workspace(ws: WorkspaceConfig, ws_dir: Path) -> bool:
    """Whether this workspace has an Odoo to run — the odoo bundle's trigger."""
    return odoo_main_alias(ws, ws_dir) is not None


def selectable_templates() -> list[str]:
    """Bundle names a workspace can declare.

    `common` and `odoo` are not among them: one is applied to every
    workspace, the other follows from the repos. Offering either in a picker
    would advertise a choice that does not exist.
    """
    return [name for name in available_templates() if name not in _UNDECLARED]


def effective_bundles(ws: WorkspaceConfig, ws_dir: Path) -> list[str]:
    """Bundles this workspace renders, in override order.

    `common` first so anything can override it, `odoo` next when there is an
    Odoo to serve, then the declared ones in their declared order. A config
    that still names `common` is not an error: the name is already there.
    """
    bundles = [IMPLICIT_BUNDLE]
    if is_odoo_workspace(ws, ws_dir):
        bundles.append(ODOO_BUNDLE)
    for name in ws.templates:
        if name not in bundles:
            bundles.append(name)
    return bundles


def legacy_mise_toml(ws_dir: Path) -> Path | None:
    """`<ws>/mise.toml` left behind by a pre-#45 ow, or None.

    ow writes `mise/conf.d/00-ow.toml` now, and mise loads that *below*
    `mise.toml` — so a leftover silently shadows it. Recognised by the one
    key ow's old template always wrote; anything else is the user's own file
    and none of ow's business.
    """
    path = ws_dir / "mise.toml"
    if not path.is_file():
        return None
    try:
        return path if "OW_WORKSPACE" in path.read_text(encoding="utf-8") else None
    except (OSError, UnicodeDecodeError):
        return None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _as_text(data: bytes | None) -> str | None:
    """utf-8 text, or None for bytes nobody can usefully diff."""
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_lock(ws_dir: Path) -> dict[str, str]:
    lock_path = ws_dir / RENDERED_LOCK
    if not lock_path.is_file():
        return {}
    with open(lock_path, "rb") as f:
        return tomllib.load(f)


def _write_lock(ws_dir: Path, lock: dict[str, str]) -> None:
    lock_path = ws_dir / RENDERED_LOCK
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    body = tomli_w.dumps(dict(sorted(lock.items())))
    tmp = lock_path.with_suffix(".tmp")
    tmp.write_text(_LOCK_HEADER + body, encoding="utf-8")
    os.replace(tmp, lock_path)


def _bundle_search_path(bundle: str) -> list[str]:
    """Where a bundle's templates are looked up, local copy winning.

    Both roots stay on the search path so an `{% include %}` still resolves
    when only one of the two files it spans was overridden locally.
    """
    roots = [paths.templates_dir() / bundle, _packaged_bundle(bundle)]
    return [str(r) for r in roots if r is not None and r.is_dir()]


def _missing_bundle(bundle: str) -> FileNotFoundError:
    local_dir = paths.templates_dir() / bundle
    packaged_dir = _packaged_bundle(bundle)
    existing_dir = next(
        (d for d in (local_dir, packaged_dir) if d is not None and d.is_dir()),
        None,
    )
    if existing_dir is not None:
        return FileNotFoundError(
            f"Template '{bundle}' found in {existing_dir} but it is empty"
        )
    return FileNotFoundError(
        f"Template '{bundle}' not found in local or packaged templates"
    )


def _render_outputs(ws: WorkspaceConfig, ws_dir: Path, context: dict) -> dict[Path, _Output]:
    """Every file this workspace's bundles want, keyed by workspace-relative path.

    A `.j2` renders to text, a plain file is taken byte for byte, and a `.j2`
    that renders to nothing but whitespace asks for no file at all — which is
    how an editor bundle keeps its Odoo debug config out of a workspace that
    has no Odoo, and how emptying a file makes ow forget it. Later bundles
    override earlier ones per path, as they always have.
    """
    outputs: dict[Path, _Output] = {}
    for bundle in effective_bundles(ws, ws_dir):
        files = bundle_source_files(bundle)
        if not files:
            raise _missing_bundle(bundle)

        env = Environment(
            loader=FileSystemLoader(_bundle_search_path(bundle)),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )

        seen: dict[Path, str] = {}
        for rel, src in sorted(files.items()):
            out_rel = rel.with_suffix("") if src.suffix == ".j2" else rel
            if out_rel in seen:
                raise ValueError(
                    f"Template output collision: {rel} and {seen[out_rel]} "
                    f"both write to {out_rel}"
                )
            seen[out_rel] = str(rel)

            if src.suffix == ".j2":
                text = env.get_template(rel.as_posix()).render(context)
                data = text.encode("utf-8") if text.strip() else None
            else:
                data = src.read_bytes()
            outputs[out_rel] = _Output(data, src.stat().st_mode)
    return outputs


def _write_file(dest: Path, out: _Output) -> None:
    assert out.data is not None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(out.data)
    dest.chmod(out.mode)


def _stale_outputs(
    ws_dir: Path, lock: dict[str, str], outputs: dict[Path, _Output]
) -> list[str]:
    """Locked paths no bundle renders any more, still on disk.

    A path in the lock that today's bundles do not produce is an output ow
    wrote once and has since retired — a bundle dropped it, a bundle that
    ships it was undeclared, the workspace no longer has the Odoo that wanted
    it. The file stays exactly where it is (ow never deletes, and re-adopting
    it would be claiming a file nothing renders), but it is ow's to name: it
    is reported like a render that comes out empty, so "not rendered" says
    the same thing about a file that is there and a file that never was.

    The lock is the evidence, and the only evidence: a path ow never locked
    is the user's, whatever it is called, so nothing walks the workspace
    looking for candidates. A lock entry whose file is gone is a tombstone,
    and naming a path that holds nothing would only puzzle whoever reads it.
    """
    produced = {rel.as_posix() for rel in outputs}
    return sorted(
        name
        for name in lock
        if name not in produced and (ws_dir / name).is_file()
    )


def _write_outputs(ws_dir: Path, outputs: dict[Path, _Output]) -> RenderResult:
    """Write what ow owns, leave what you touched, per the lock.

    Four cases, and the third is the one that earns the lock: a file whose
    hash still matches it is ow's to update, a file that drifted from it is
    yours and is never written again. A file already equal to the render is
    adopted without a write — that is how a workspace from before the lock
    comes under management without losing anything. A file you deleted comes
    back: ow cannot tell it from one it never wrote, and a tombstone would be
    a state file about a state file.
    """
    lock = _read_lock(ws_dir)
    result = RenderResult()
    for rel, out in sorted(outputs.items()):
        name = rel.as_posix()
        dest = ws_dir / rel

        if out.data is None:
            result.skipped.append(name)
            continue

        result.managed.append(name)

        if not dest.exists():
            _write_file(dest, out)
            lock[name] = _sha256_bytes(out.data)
            result.wrote.append(name)
            continue

        current = dest.read_bytes()
        if current == out.data:
            lock[name] = _sha256_bytes(current)
        elif lock.get(name) == _sha256_bytes(current):
            _write_file(dest, out)
            lock[name] = _sha256_bytes(out.data)
            result.updated.append(name)
        else:
            result.yours.append(name)

    # Retired outputs keep their lock entry — the lock records what ow wrote,
    # and forgetting a file is not the same as giving it back.
    result.skipped.extend(_stale_outputs(ws_dir, lock, outputs))

    _write_lock(ws_dir, lock)
    return result


def _first_verdict(first: RenderResult, second: RenderResult) -> RenderResult:
    """Merge two render passes, keeping each path's first verdict.

    A file the first pass created and the second rewrote was still created by
    this run; reporting it twice, under two names, would only puzzle whoever
    reads the output.

    A skip is the one first verdict the second pass may overturn. "ow wants
    nothing here" is provisional when it comes from a pass whose context could
    not yet see what that same pass was about to write — an addon-dependent
    output renders empty on the first pass and real on the second. Whatever
    the second pass renders at such a path decides instead: what it did there
    (wrote, updated, yours), or nothing at all when the file on disk is
    already what the render wants — an adoption is not a skip either.
    Skipped in both passes stays skipped.
    """
    merged = RenderResult(
        list(first.wrote),
        list(first.updated),
        list(first.yours),
        list(first.skipped),
        list(first.managed),
    )
    first_skipped = set(first.skipped)
    second_rendered = set(second.managed)

    # `managed` holds every path the second pass rendered, whichever verdict
    # it reached — the silent adoption of an already-correct file included.
    merged.skipped = [name for name in merged.skipped if name not in second_rendered]

    decided = set(first.wrote) | set(first.updated) | set(first.yours)
    for names, target in (
        (second.wrote, merged.wrote),
        (second.updated, merged.updated),
        (second.yours, merged.yours),
    ):
        for name in names:
            if name in decided:
                continue
            target.append(name)
            decided.add(name)
    for name in second.skipped:
        if name not in decided and name not in first_skipped:
            merged.skipped.append(name)
    for name in second.managed:
        if name not in merged.managed:
            merged.managed.append(name)
    return merged


# ---------------------------------------------------------------------------
# Template application helpers (shared between cmd_init and cmd_apply)
# ---------------------------------------------------------------------------


def apply_templates(ws: WorkspaceConfig, config: Config, ws_dir: Path) -> RenderResult:
    """Render every bundle of `ws` into ws_dir, and report what happened.

    Rendered twice when the first pass changed what the addon scan can see: a
    bundle may ship an Odoo addon of its own, and build_template_context
    reads the filesystem — so on the first pass that addon does not exist yet
    and never reaches addons_path (#42). The second pass is skipped whenever
    the rescan agrees with the first, which is the normal case.
    """
    context = build_template_context(ws, config, ws_dir)
    result = _write_outputs(ws_dir, _render_outputs(ws, ws_dir, context))

    rescanned = build_template_context(ws, config, ws_dir)
    if any(
        rescanned[key] != context[key]
        for key in ("addons_paths", "odools_path_items")
    ):
        second = _write_outputs(ws_dir, _render_outputs(ws, ws_dir, rescanned))
        result = _first_verdict(result, second)

    return result


def rendered_states(ws: WorkspaceConfig, config: Config, ws_dir: Path) -> list[RenderedFile]:
    """Every output of this workspace's bundles, with its state. Writes nothing.

    Retired outputs — locked once, rendered by nothing today, still on disk —
    are listed too, as NOT_RENDERED with no `ow_text`: there is nothing ow
    would write there, and the file you can see is the one ow stopped
    writing. A locked path whose file is gone is not listed at all.
    """
    context = build_template_context(ws, config, ws_dir)
    outputs = _render_outputs(ws, ws_dir, context)
    lock = _read_lock(ws_dir)

    states: list[RenderedFile] = []
    for rel, out in sorted(outputs.items()):
        name = rel.as_posix()
        dest = ws_dir / rel
        current = dest.read_bytes() if dest.is_file() else None

        if out.data is None:
            state = NOT_RENDERED
        elif current is None:
            state = ABSENT
        elif current == out.data:
            state = UP_TO_DATE
        elif lock.get(name) == _sha256_bytes(current):
            state = OUTDATED
        else:
            state = YOURS

        states.append(RenderedFile(name, state, _as_text(out.data), _as_text(current)))

    for name in _stale_outputs(ws_dir, lock, outputs):
        on_disk = _as_text((ws_dir / name).read_bytes())
        states.append(RenderedFile(name, NOT_RENDERED, None, on_disk))

    # Retired outputs were appended; the listing stays one sorted list.
    return sorted(states, key=lambda f: Path(f.path))


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
