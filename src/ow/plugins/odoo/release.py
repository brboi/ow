"""Odoo core detection and `release.py` / `config.py` AST reading.

Checkout code is only parsed, never executed.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

from ow.core.plugins import Diagnostic

if TYPE_CHECKING:
    from ow.core.workspace import Repo, Workspace


def is_odoo_core(repo_path: Path) -> bool:
    """Check if a repo contains odoo-bin, addons/ and odoo/addons/."""
    return (
        (repo_path / "odoo-bin").exists()
        and (repo_path / "addons").is_dir()
        and (repo_path / "odoo" / "addons").is_dir()
    )


_parse_cache: dict[tuple[Path, int], ast.Module | None] = {}


def _parse_module(path: Path) -> ast.Module | None:
    """Parse a Python module, returning None on any error; cached by path and mtime."""
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    key = (path, mtime)
    if key not in _parse_cache:
        try:
            _parse_cache[key] = ast.parse(
                path.read_text(encoding="utf-8"), filename=str(path)
            )
        except (OSError, SyntaxError):
            _parse_cache[key] = None
    return _parse_cache[key]


def _module_assignments(tree: ast.Module, name: str) -> list[ast.expr]:
    """Find all module-level assignments to a name."""
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


def _literal_int_pair(node: ast.expr) -> tuple[int, int] | None:
    """Extract a tuple of two integers from an AST node."""
    if not isinstance(node, (ast.Tuple, ast.List)) or len(node.elts) != 2:
        return None
    values: list[int] = []
    for elt in node.elts:
        if not isinstance(elt, ast.Constant) or type(elt.value) is not int:
            return None
        values.append(elt.value)
    return (values[0], values[1])


def read_python_bounds(core_path: Path) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Read MIN_PY_VERSION and MAX_PY_VERSION from release.py or odoo/__init__.py."""
    release_path = core_path / "odoo" / "release.py"
    init_path = core_path / "odoo" / "__init__.py"

    python_min = None
    python_max = None

    if release_path.exists():
        tree = _parse_module(release_path)
        if tree:
            min_assigns = _module_assignments(tree, "MIN_PY_VERSION")
            max_assigns = _module_assignments(tree, "MAX_PY_VERSION")
            if min_assigns:
                python_min = _literal_int_pair(min_assigns[-1])
            if max_assigns:
                python_max = _literal_int_pair(max_assigns[-1])

    # Fall back to odoo/__init__.py if needed
    if (python_min is None or python_max is None) and init_path.exists():
        tree = _parse_module(init_path)
        if tree:
            if python_min is None:
                assigns = _module_assignments(tree, "MIN_PY_VERSION")
                if assigns:
                    python_min = _literal_int_pair(assigns[-1])
            if python_max is None:
                assigns = _module_assignments(tree, "MAX_PY_VERSION")
                if assigns:
                    python_max = _literal_int_pair(assigns[-1])

    if python_min and python_max:
        return (python_min, python_max)
    return None


def read_odoo_series(core_path: Path) -> str | None:
    """Read Odoo series (e.g. '18.0', 'saas-18.3') from odoo/release.py, or None."""
    release_path = core_path / "odoo" / "release.py"
    if not release_path.exists():
        return None
    tree = _parse_module(release_path)
    if tree is None:
        return None

    for var in ("serie", "series", "major_version"):
        assigns = _module_assignments(tree, var)
        if assigns and isinstance(assigns[-1], ast.Constant) and isinstance(assigns[-1].value, str):
            raw = assigns[-1].value.strip()
            if raw:
                return raw.replace("~", "-")

    vi_assigns = _module_assignments(tree, "version_info")
    if vi_assigns and isinstance(vi_assigns[-1], (ast.Tuple, ast.List)) and len(vi_assigns[-1].elts) >= 2:
        first, second = vi_assigns[-1].elts[0], vi_assigns[-1].elts[1]
        if isinstance(first, ast.Constant) and isinstance(second, ast.Constant):
            ok_first = (type(first.value) is int) or isinstance(first.value, str)
            ok_second = (type(second.value) is int) or isinstance(second.value, str)
            if ok_first and ok_second:
                return f"{first.value}.{second.value}".replace("~", "-")

    return None


def read_demo_capability(core_path: Path) -> bool | None:
    """True when config.py declares `--with-demo`, False for `--without-demo` or neither."""
    config_path = core_path / "odoo" / "tools" / "config.py"
    tree = _parse_module(config_path)
    if tree is None:
        return None

    declared: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"add_option", "add_argument"}:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if arg.value in ("--with-demo", "--without-demo"):
                            declared.add(arg.value)
    return "--with-demo" in declared


def get_core(ws: Workspace) -> tuple[Repo | None, list[Diagnostic]]:
    """Find the workspace's single readable Odoo core.

    Returns `(core, [])` with exactly one core and readable metadata, or
    `(None, [warning])` with one diagnostic naming the cause (two cores,
    unparsable files, malformed bounds), or `(None, [])` with no core at all.
    """
    cores = [repo for repo in ws.repos if is_odoo_core(repo.path)]

    if len(cores) > 1:
        names = ", ".join(repo.name for repo in cores)
        return None, [
            Diagnostic(level="warning", text=f"Multiple Odoo cores found: {names}; keep exactly one and remove the rest")
        ]

    if not cores:
        return None, []

    core = cores[0]
    if read_python_bounds(core.path) is None:
        return None, [
            Diagnostic(level="warning", text=f"{core.name}: cannot read Python version bounds from odoo/release.py;"
            " restore or fix the file")
        ]
    if read_demo_capability(core.path) is None:
        return None, [
            Diagnostic(level="warning", text=f"{core.name}: cannot parse odoo/tools/config.py; restore or fix the file")
        ]

    return core, []
