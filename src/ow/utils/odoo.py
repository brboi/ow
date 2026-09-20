"""Checkout-derived Odoo identity, capabilities and addon discovery.

Everything here reads checkout files as text and `ast`. Nothing here
imports, execs, or subprocess-invokes any file under a checkout, and
nothing here installs, runs, or checks for bwrap/firejail: sandbox probing
only reports which fixed scripts/profiles exist and are usable.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Mapping

ProbeKind = Literal["none", "supported", "unsupported", "invalid", "ambiguous", "incomplete"]

# Odoo support is the explicit set 18, 19, 20, their saas series, and
# current master (inspected identity (20, 1)). A future major requires a
# support-table change, not a wall-clock or branch-name calculation.
SUPPORTED_MAJORS = frozenset({18, 19, 20})

_WITH_DEMO_FLAG = "--with-demo"
_WITHOUT_DEMO_FLAG = "--without-demo"
_DEMO_FLAGS = frozenset({_WITH_DEMO_FLAG, _WITHOUT_DEMO_FLAG})

_SAAS_MAJOR_RE = re.compile(r"^saas~(\d+)$")

# task name -> (checkout-relative path, must be an executable regular file).
# The firejail profile is read by firejail itself, never executed directly.
_SANDBOX_ASSETS: tuple[tuple[str, Path, bool], ...] = (
    ("bwrap-claude", Path("setup/sandboxing/bwrap/bwrap-claude.sh"), True),
    ("bwrap-opencode", Path("setup/sandboxing/bwrap/bwrap-opencode.sh"), True),
    ("bwrap-pi", Path("setup/sandboxing/bwrap/bwrap-pi.sh"), True),
    ("firejail-claude", Path("setup/sandboxing/firejail/claude.profile"), False),
)


@dataclass
class OdooInfo:
    """Everything derived from one checkout's files, never its imports."""

    alias: str
    series: int
    major: int
    minor: int
    python_min: tuple[int, int]
    python_max: tuple[int, int]
    with_demo: bool
    sandbox_paths: dict[str, Path]


@dataclass
class OdooProbe:
    """Outcome of classifying a workspace's declared repos for an Odoo core."""

    kind: ProbeKind
    info: OdooInfo | None
    reason: str | None = None


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


def workspace_addon_paths(ws_dir: Path, repo_dirs: Mapping[str, Path]) -> list[Path]:
    """Every addons_path this workspace exposes, in Odoo's resolution order.

    `.local` addons first, then other loose workspace addons, then each
    non-core repo's own addons_paths in `repo_dirs` order, then the core
    repo's two fixed addons_path directories last: Odoo resolves a module
    from the first addons_path holding it, and loose addons exist to shadow
    the module they replace. Raises if more than one repo looks like the
    Odoo core: an ambiguous core has no correct place in this order.
    """
    cores = [alias for alias, repo_dir in repo_dirs.items() if is_odoo_main_repo(repo_dir)]
    if len(cores) > 1:
        raise ValueError(f"repo_dirs: multiple Odoo cores {sorted(cores)!r}")

    main_paths: list[Path] = []
    repo_paths: list[Path] = []
    for alias, repo_dir in repo_dirs.items():
        if alias in cores:
            main_paths = [repo_dir / "addons", repo_dir / "odoo" / "addons"]
        else:
            repo_paths.extend(find_addon_paths(repo_dir))

    loose = find_addon_paths(ws_dir / ".local") + find_addon_paths(
        ws_dir, exclude=list(repo_dirs.values())
    )
    return loose + repo_paths + main_paths


def probe_odoo(repo_dirs: Mapping[str, Path]) -> OdooProbe:
    """Classify a workspace's Odoo core from its declared repo checkouts.

    Reads only the filesystem: no git status, no branch/remote names, no
    import or exec of any file under `repo_dirs`. `repo_dirs` maps each
    declared repo alias to its worktree path. A missing declared worktree
    is `incomplete`; more than one repo matching the core markers is
    `ambiguous` rather than picking the first or last alias; a recognized
    core with malformed metadata is `invalid`, never silently `none`.
    """
    missing = sorted(alias for alias, repo_dir in repo_dirs.items() if not repo_dir.is_dir())
    if missing:
        return OdooProbe(
            kind="incomplete", info=None, reason=f"missing worktree(s): {', '.join(missing)}"
        )

    cores = sorted(alias for alias, repo_dir in repo_dirs.items() if is_odoo_main_repo(repo_dir))
    if len(cores) > 1:
        return OdooProbe(
            kind="ambiguous", info=None, reason=f"multiple Odoo cores: {', '.join(cores)}"
        )
    if not cores:
        return OdooProbe(kind="none", info=None, reason=None)

    alias = cores[0]
    try:
        info = _probe_core(alias, repo_dirs[alias])
    except ValueError as exc:
        return OdooProbe(kind="invalid", info=None, reason=str(exc))

    kind: ProbeKind = "supported" if info.major in SUPPORTED_MAJORS else "unsupported"
    return OdooProbe(kind=kind, info=info, reason=None)


def _probe_core(alias: str, core_path: Path) -> OdooInfo:
    release_path = core_path / "odoo" / "release.py"
    release_tree = _parse_module(release_path)

    major, minor = _read_identity(release_tree, release_path)
    python_min, python_max = _read_python_bounds(release_tree, core_path, release_path)
    with_demo = _read_demo_capability(core_path)
    sandbox_paths = _probe_sandbox_paths(core_path)

    return OdooInfo(
        alias=alias,
        series=major,
        major=major,
        minor=minor,
        python_min=python_min,
        python_max=python_max,
        with_demo=with_demo,
        sandbox_paths=sandbox_paths,
    )


def _parse_module(path: Path) -> ast.Module:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"{path}: cannot read ({exc.strerror or exc})") from exc
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise ValueError(f"{path}: invalid Python syntax ({exc.msg})") from exc


def _module_assignments(tree: ast.Module, name: str) -> list[ast.expr]:
    """Every value directly bound to `name` at module level.

    Only module-body Assign/AnnAssign statements count: a name reassigned
    inside a function or conditional is not a declaration ow can trust.
    """
    values: list[ast.expr] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                values.append(value)
    return values


def _literal_pair(node: ast.expr, label: str) -> tuple[object, object]:
    if not isinstance(node, (ast.Tuple, ast.List)) or len(node.elts) < 2:
        raise ValueError(f"{label}: expected at least two literal tuple elements")
    first, second = node.elts[:2]
    if not isinstance(first, ast.Constant) or not isinstance(second, ast.Constant):
        raise ValueError(f"{label}: non-literal identity")
    return first.value, second.value


def _parse_major(value: object, release_path: Path) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str):
        match = _SAAS_MAJOR_RE.match(value)
        if match:
            return int(match.group(1))
    raise ValueError(f"version_info: unrecognized major {value!r} in {release_path}")


def _read_identity(tree: ast.Module, release_path: Path) -> tuple[int, int]:
    assignments = _module_assignments(tree, "version_info")
    if not assignments:
        raise ValueError(f"version_info: not declared in {release_path}")
    if len(assignments) > 1:
        raise ValueError(f"version_info: multiple definitions in {release_path}")

    major_value, minor_value = _literal_pair(assignments[0], "version_info")
    if type(minor_value) is not int:
        raise ValueError(f"version_info: non-integer minor in {release_path}")
    return _parse_major(major_value, release_path), minor_value


def _literal_int_pair(node: ast.expr, name: str, path: Path) -> tuple[int, int]:
    if not isinstance(node, (ast.Tuple, ast.List)) or len(node.elts) != 2:
        raise ValueError(f"{name}: expected exactly two literal tuple elements in {path}")
    values: list[int] = []
    for elt in node.elts:
        if not isinstance(elt, ast.Constant) or type(elt.value) is not int:
            raise ValueError(f"{name}: non-integer element in {path}")
        values.append(elt.value)
    return values[0], values[1]


def _read_bound(tree: ast.Module, name: str, path: Path) -> tuple[int, int] | None:
    assignments = _module_assignments(tree, name)
    if not assignments:
        return None
    if len(assignments) > 1:
        raise ValueError(f"{name}: multiple definitions in {path}")
    return _literal_int_pair(assignments[0], name, path)


def _read_python_bounds(
    release_tree: ast.Module, core_path: Path, release_path: Path
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Read MIN/MAX_PY_VERSION, falling back to odoo/__init__.py only when a
    bound is absent from release.py -- a present but invalid bound is an
    error, never permission to fall back."""
    python_min = _read_bound(release_tree, "MIN_PY_VERSION", release_path)
    python_max = _read_bound(release_tree, "MAX_PY_VERSION", release_path)

    if python_min is None or python_max is None:
        init_path = core_path / "odoo" / "__init__.py"
        init_tree = _parse_module(init_path)
        if python_min is None:
            python_min = _read_bound(init_tree, "MIN_PY_VERSION", init_path)
        if python_max is None:
            python_max = _read_bound(init_tree, "MAX_PY_VERSION", init_path)

    if python_min is None or python_max is None:
        raise ValueError(
            f"MIN_PY_VERSION/MAX_PY_VERSION: not declared in {release_path} "
            "or its odoo/__init__.py fallback"
        )
    if python_min[0] != 3 or python_max[0] != 3:
        raise ValueError(f"MIN_PY_VERSION/MAX_PY_VERSION: expected Python major 3 in {release_path}")
    if python_min > python_max:
        raise ValueError(f"MIN_PY_VERSION/MAX_PY_VERSION: min exceeds max in {release_path}")

    return python_min, python_max


def _declared_demo_flags(tree: ast.AST) -> set[str]:
    return {
        argument.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"add_option", "add_argument"}
        for argument in node.args
        if isinstance(argument, ast.Constant)
        and isinstance(argument.value, str)
        and argument.value in _DEMO_FLAGS
    }


def _read_demo_capability(core_path: Path) -> bool:
    config_path = core_path / "odoo" / "tools" / "config.py"
    tree = _parse_module(config_path)
    flags = _declared_demo_flags(tree)
    if _WITH_DEMO_FLAG in flags:
        return True
    if _WITHOUT_DEMO_FLAG in flags:
        return False
    raise ValueError(f"--with-demo/--without-demo: not declared in {config_path}")


def _probe_sandbox_paths(core_path: Path) -> dict[str, Path]:
    """Discover which fixed sandbox assets exist and are usable.

    Never runs a script, checks a profile's content, or verifies bwrap/
    firejail are installed: presence and executability only. A path whose
    fully resolved real location leaves the checkout -- whether through a
    symlinked leaf or a symlinked ancestor directory -- is rejected.
    """
    try:
        resolved_root = core_path.resolve(strict=True)
    except OSError:
        return {}

    found: dict[str, Path] = {}
    for task_name, relative, executable_required in _SANDBOX_ASSETS:
        candidate = core_path / relative
        if not candidate.is_file():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_relative_to(resolved_root):
            continue
        if executable_required and not os.access(candidate, os.X_OK):
            continue
        found[task_name] = candidate
    return found
