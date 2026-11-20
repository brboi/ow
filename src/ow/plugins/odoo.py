"""Odoo plugin: project configuration and debugging support."""

from __future__ import annotations

import ast
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from configparser import ConfigParser
from dataclasses import replace
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

import tomli_w

from ow.core import bases, config, git, stacks
from ow.core.fs import json_text
from ow.core.paths import repos_dir
from ow.core.plugins import (
    Diagnostic,
    GeneratedFile,
    KnownRepo,
    ResolvedRepo,
    ResolvedTarget,
    RunCommand,
    Setting,
    StackInfo,
    hookimpl,
)
from ow.errors import UsageError
from ow.plugins._github import bare_gh_info, parse_git_slug, parse_pr_identity
from ow.plugins._odoo_repos import (
    ODOO_ADDON_NAMES,
    ODOO_ADDON_SLUGS,
    ODOO_CORE,
    ODOO_CORE_NAMES,
    ODOO_CORE_SLUGS,
    ODOO_NAMES,
    ODOO_REPOS,
    ODOO_SLUGS,
    is_odoo_series,
)
from ow.specs.editors import DebugLaunch, EditorContribution
from ow.specs.python_env import PythonEnvironment, WorkspaceVenv, workspace_venv

if TYPE_CHECKING:
    from ow.core.workspace import Repo, Workspace


# Markers for Odoo core detection
def _is_odoo_core(repo_path: Path) -> bool:
    """Check if a repo contains odoo-bin, addons/ and odoo/addons/."""
    return (
        (repo_path / "odoo-bin").exists()
        and (repo_path / "addons").is_dir()
        and (repo_path / "odoo" / "addons").is_dir()
    )


# Python bounds reading
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


def _read_python_bounds(core_path: Path) -> tuple[tuple[int, int], tuple[int, int]] | None:
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


def _read_odoo_series(core_path: Path) -> str | None:
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


def _python_environment(core: Repo) -> PythonEnvironment:
    """The Python environment the Odoo checkout `core` requires."""
    return PythonEnvironment(
        env_paths={"ODOO_RC": "odoorc"},
        path=(core.name,),
        python=_read_python_bounds(core.path),
        venv_key=_read_odoo_series(core.path),
        requirements=(f"{core.name}/requirements.txt", "requirements-dev.txt"),
        aliases={"osh": "odoo-bin shell --no-http -c \"$ODOO_RC\""},
    )


def _workspace_venv(ws: Workspace, core: Repo) -> WorkspaceVenv:
    """Virtualenv of an Odoo workspace."""
    return workspace_venv(ws, _python_environment(core))


def _read_demo_capability(core_path: Path) -> bool | None:
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


def _find_addon_paths(
    path: Path, exclude: set[Path] | None = None
) -> list[Path]:
    """Addons_path directories under `path`, tolerating unreadable ones.

    An addons_path is a directory whose immediate children include an addon (a
    directory holding `__manifest__.py`/`__openerp__.py`); the walk stops
    descending there. Hidden, `node_modules` and `__pycache__` directories are
    never entered, nor are the `exclude` trees the caller handles itself.
    """
    exclude = set() if exclude is None else exclude
    if not _is_dir(path):
        return []

    result: list[Path] = []
    seen: set[int] = set()
    stack = [path]

    while stack:
        current = stack.pop()
        try:
            st = current.stat()
        except OSError:
            continue
        if st.st_ino in seen:
            continue
        seen.add(st.st_ino)

        try:
            if current.resolve() in exclude:
                continue
        except OSError:
            pass

        try:
            children = list(current.iterdir())
        except OSError:
            continue

        if any(_has_manifest(child) for child in children):
            result.append(current)
            continue

        for child in children:
            if not _is_dir(child):
                continue
            if child.name.startswith(".") or child.name in ("node_modules", "__pycache__"):
                continue
            stack.append(child)

    return sorted(result)


def _is_dir(path: Path) -> bool:
    """`path.is_dir()`, with unreadable paths simply not directories."""
    try:
        return path.is_dir()
    except OSError:
        return False


def _has_manifest(path: Path) -> bool:
    """`path` is a directory holding an addon manifest, unreadable ones excluded."""
    if not _is_dir(path):
        return False
    for name in ("__manifest__.py", "__openerp__.py"):
        try:
            if (path / name).is_file():
                return True
        except OSError:
            return False
    return False


def _workspace_addon_paths(ws: Workspace, core: Repo) -> list[Path]:
    """Every addons_path this workspace exposes, in Odoo's resolution order.

    `.local` and loose root addons come first: they exist to shadow the module
    they replace. Then each non-core repo's own addons_paths in workspace order,
    then the core's two fixed directories. Deduplicated without reordering.
    """
    repo_paths = {repo.path for repo in ws.repos}
    candidates: list[Path] = list(_find_addon_paths(ws.root / ".local"))
    candidates += _find_addon_paths(ws.root, exclude=repo_paths)
    for repo in ws.repos:
        if repo.path == core.path:
            continue
        candidates += _find_addon_paths(repo.path)
    candidates.append(core.path / "addons")
    candidates.append(core.path / "odoo" / "addons")

    seen: set[Path] = set()
    result: list[Path] = []
    for path in candidates:
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


_PORT_KEYS = ("odoo.http_port", "odoo.gevent_port", "odoo.db_port", "odoo.smtp_port")
_PLAIN_KEYS = ("odoo.db_name", "odoo.db_host", "odoo.db_user", "odoo.smtp_server")
_PASSWORD_KEYS = ("odoo.db_password", "odoo.admin_passwd")
_LIST_KEYS = ("odoo.debug_args", "odoo.debug_test_args")
_VALIDATED_KEYS = _PORT_KEYS + _PLAIN_KEYS + _PASSWORD_KEYS + _LIST_KEYS

_SETTING_DEFAULTS: dict[str, object] = {}


def _setting_default(key: str, ws: Workspace) -> object:
    """The `Setting` table's default for `key`, resolving a per-workspace callable with `ws`."""
    if not _SETTING_DEFAULTS:
        for setting in ow_settings():
            _SETTING_DEFAULTS[setting.key] = setting.default
    default = _SETTING_DEFAULTS.get(key)
    return default(ws) if callable(default) else default


def _get_setting(
    key: str, settings: Mapping[str, object], ws: Workspace
) -> tuple[object, Diagnostic | None]:
    """Validated setting value; an unusable one falls back to its `Setting` default with a warning."""
    val = settings.get(key)
    default = _setting_default(key, ws)

    if key in _PORT_KEYS:
        port = config.as_int(val)
        if port is not None:
            if not 1 <= port <= 65535:
                return default, Diagnostic("warning", f"{key} must be 1-65535, using default")
            return port, None
        return default, None

    if key in _LIST_KEYS:
        if val is None:
            return default, None
        cleaned = _clean_args(val)
        if cleaned is None:
            return default, Diagnostic("warning", f"{key} contains invalid item, using default")
        return cleaned, None

    if not isinstance(val, str):
        return default, None
    if "\n" in val or "\r" in val or "\x00" in val or (key in _PLAIN_KEYS and not val):
        return default, Diagnostic("warning", f"{key} invalid, using default")
    return val, None


@hookimpl
def ow_settings() -> list[Setting]:
    """Provide Odoo settings."""
    return [
        Setting(
            key="odoo.http_port",
            type=int,
            default=8069,
            help="HTTP port for Odoo",
        ),
        Setting(
            key="odoo.gevent_port",
            type=int,
            default=8072,
            help="Gevent/longpolling port for Odoo",
        ),
        Setting(
            key="odoo.db_name",
            type=str,
            default=lambda ws: ws.name,
            help="Database name (defaults to workspace name)",
        ),
        Setting(
            key="odoo.db_host",
            type=str,
            default="localhost",
            help="Database host",
        ),
        Setting(
            key="odoo.db_port",
            type=int,
            default=5432,
            help="Database port",
        ),
        Setting(
            key="odoo.db_user",
            type=str,
            default="odoo",
            help="Database user",
        ),
        Setting(
            key="odoo.db_password",
            type=str,
            default="odoo",
            help="Database password",
        ),
        Setting(
            key="odoo.admin_passwd",
            type=str,
            default="Password",
            help="Odoo admin password",
        ),
        Setting(
            key="odoo.smtp_server",
            type=str,
            default="localhost",
            help="SMTP server",
        ),
        Setting(
            key="odoo.smtp_port",
            type=int,
            default=25,
            help="SMTP port",
        ),
        Setting(
            key="odoo.debug_args",
            type=list,
            default=_default_debug_args,
            help='Arguments of the editor "Run Instance With Debug" launch',
        ),
        Setting(
            key="odoo.debug_test_args",
            type=list,
            default=lambda ws: [f"--test-tags={ws.name}"],
            help='Arguments of the editor "Debug Tests" launch',
        ),
        Setting(
            key="odoo.default_repos",
            type=list,
            default=["community", "enterprise"],
            help="Default repos expanded by `@<base>` targets in `ow init` / `ow add` / `ow run --tmp`",
        ),
    ]


def _get_core(ws: Workspace) -> tuple[Repo | None, list[Diagnostic]]:
    """Find the workspace's single readable Odoo core.

    Returns `(core, [])` with exactly one core and readable metadata, or
    `(None, [warning])` with one diagnostic naming the cause (two cores,
    unparsable files, malformed bounds), or `(None, [])` with no core at all.
    """
    cores = [repo for repo in ws.repos if _is_odoo_core(repo.path)]

    if len(cores) > 1:
        names = ", ".join(repo.name for repo in cores)
        return None, [
            Diagnostic(
                "warning",
                f"Multiple Odoo cores found: {names}; keep exactly one and remove the rest",
            )
        ]

    if not cores:
        return None, []

    core = cores[0]
    if _read_python_bounds(core.path) is None:
        return None, [
            Diagnostic(
                "warning",
                f"{core.name}: cannot read Python version bounds from odoo/release.py;"
                " restore or fix the file",
            )
        ]
    if _read_demo_capability(core.path) is None:
        return None, [
            Diagnostic(
                "warning",
                f"{core.name}: cannot parse odoo/tools/config.py; restore or fix the file",
            )
        ]

    return core, []


def _default_debug_args(ws: Workspace) -> list[str]:
    """`--dev=all`, plus `--without-demo=all` for a pre-19 core.

    Old cores spell the flag `--without-demo`; without it a freshly created
    database would load demo data.
    """
    args = ["--dev=all"]
    core, _ = _get_core(ws)
    if core is not None and _read_demo_capability(core.path) is False:
        args.append("--without-demo=all")
    return args


def _clean_args(value: object) -> list[str] | None:
    """A usable argument list, or None when a string arg could break `argv`."""
    if not isinstance(value, list):
        return None
    if any(
        not isinstance(item, str) or "\n" in item or "\r" in item or "\x00" in item
        for item in value
    ):
        return None
    return list(value)


def _validate_settings(ws: Workspace) -> list[Diagnostic]:
    """Warn about semantically invalid settings, each with a per-key fallback."""
    diagnostics: list[Diagnostic] = []
    for key in _VALIDATED_KEYS:
        _, diag = _get_setting(key, ws.settings, ws)
        if diag is not None:
            diagnostics.append(diag)
    return diagnostics


@hookimpl
def ow_diagnostics(ws: Workspace) -> list[Diagnostic]:
    """Name the core problem, else validate the readable core's settings."""
    core, core_diagnostics = _get_core(ws)
    if core_diagnostics:
        return core_diagnostics
    if core is None:
        return []
    return _validate_settings(ws)


@hookimpl
def ow_generated_files(
    ws: Workspace, answers: Callable[[str], list]
) -> list[GeneratedFile | Diagnostic]:
    """Generate Odoo configuration files."""
    # `ow_diagnostics` already reported a broken core; contribute nothing.
    core, _ = _get_core(ws)
    if core is None:
        return []

    diagnostics: list[Diagnostic] = []
    files: list[GeneratedFile] = []

    # `addons_path` is comma-separated and absolute: a path holding a comma or a
    # line break could never round-trip through it.
    addon_paths: list[Path] = []
    for path in _workspace_addon_paths(ws, core):
        text = path.as_posix()
        if "," in text or "\n" in text or "\r" in text:
            diagnostics.append(
                Diagnostic("warning", f"addon path cannot be represented in addons_path: {text}")
            )
            continue
        addon_paths.append(path)

    settings = ws.settings

    http_port, _ = _get_setting("odoo.http_port", settings, ws)
    gevent_port, _ = _get_setting("odoo.gevent_port", settings, ws)
    db_name, _ = _get_setting("odoo.db_name", settings, ws)
    db_host, _ = _get_setting("odoo.db_host", settings, ws)
    db_port, _ = _get_setting("odoo.db_port", settings, ws)
    db_user, _ = _get_setting("odoo.db_user", settings, ws)
    db_password, _ = _get_setting("odoo.db_password", settings, ws)
    admin_passwd, _ = _get_setting("odoo.admin_passwd", settings, ws)
    smtp_server, _ = _get_setting("odoo.smtp_server", settings, ws)
    smtp_port, _ = _get_setting("odoo.smtp_port", settings, ws)

    parser = ConfigParser(interpolation=None)
    parser["options"] = {
        "http_port": str(http_port),
        "gevent_port": str(gevent_port),
        "addons_path": ",".join(p.as_posix() for p in addon_paths),
        "data_dir": str(ws.root / ".odoo"),
        "admin_passwd": str(admin_passwd),
        "db_name": str(db_name),
        "dbfilter": f"^{re.escape(str(db_name))}$",
        "db_host": str(db_host),
        "db_port": str(db_port),
        "db_user": str(db_user),
        "db_password": str(db_password),
        "smtp_server": str(smtp_server),
        "smtp_port": str(smtp_port),
    }
    buf = StringIO()
    parser.write(buf, space_around_delimiters=True)
    odoorc_content = buf.getvalue()

    files.append(
        GeneratedFile(
            path=PurePosixPath("odoorc"),
            data=odoorc_content.encode("utf-8"),
            mode=0o600,
        )
    )

    venv = _workspace_venv(ws, core)
    is_local_venv = venv.is_local(ws)
    python_path_str = ".venv/bin/python" if is_local_venv else str(venv.path / "bin" / "python")

    odools_content = tomli_w.dumps({
        "config": [
            {
                "name": f"[Odoo Workspace] {ws.name}",
                "python_path": python_path_str,
                "odoo_path": f"./{core.name}",
                "addons_paths": [
                    "." if (rel := p.relative_to(ws.root).as_posix()) == "." else f"./{rel}"
                    for p in addon_paths
                ],
            }
        ]
    })
    files.append(
        GeneratedFile(
            path=PurePosixPath("odools.toml"),
            data=odools_content.encode("utf-8"),
        )
    )

    pyright_content = json_text({
        "venvPath": "." if is_local_venv else str(venv.path.parent),
        "venv": ".venv" if is_local_venv else venv.path.name,
    })
    files.append(
        GeneratedFile(
            path=PurePosixPath("pyrightconfig.json"),
            data=pyright_content.encode("utf-8"),
        )
    )

    files.append(
        GeneratedFile(
            path=PurePosixPath("requirements-dev.txt"),
            data=b"inotify\n",
        )
    )

    return files + diagnostics


@hookimpl
def python_environment(ws: Workspace) -> PythonEnvironment | None:
    """Contribute tool-agnostic Python environment from Odoo."""
    core, core_diagnostics = _get_core(ws)
    if core_diagnostics or not core:
        return None

    return _python_environment(core)


@hookimpl
def editors_contribution(ws: Workspace) -> EditorContribution | None:
    """Contribute debug launches and settings for editors."""
    core, core_diagnostics = _get_core(ws)
    if core_diagnostics or not core:
        return None

    settings = ws.settings
    debug_args, _ = _get_setting("odoo.debug_args", settings, ws)
    debug_test_args, _ = _get_setting("odoo.debug_test_args", settings, ws)
    venv = _workspace_venv(ws, core)

    core_path = core.path
    program = core_path / "odoo-bin"
    cwd = core_path
    env = {
        "ODOO_RC": str(ws.root / "odoorc"),
        "PYDEVD_DISABLE_FILE_VALIDATION": "1",
        "PYTHONPATH": str(core_path),
    }

    launches = (
        DebugLaunch(
            name="Run Instance With Debug",
            program=program,
            args=tuple(debug_args),
            cwd=cwd,
            env=env,
        ),
        DebugLaunch(
            name=f"Debug Tests ({ws.name})",
            program=program,
            args=tuple(debug_test_args),
            cwd=cwd,
            env=env,
        ),
    )

    language_servers = {
        "odoo": {
            "Odoo.selectedProfile": f"[Odoo Workspace] {ws.name}",
        }
    }

    excludes = ("**/*.po", "**/*.pot")

    return EditorContribution(
        launches=launches,
        language_servers=language_servers,
        excludes=excludes,
        python=venv.path / "bin" / "python",
    )


ODOO_SERIES_PREFIX_RE = re.compile(r"^(?P<series>master|saas-\d+\.\d+|\d+\.\d+)(?:-|$)")


def _repo_slugs(repo: ResolvedRepo) -> set[str]:
    slugs: set[str] = set()
    parsed = parse_git_slug(repo.url)
    if parsed is not None:
        slugs.add(f"{parsed[0]}/{parsed[1]}")
    if repo.pr is not None and (parsed_pr := parse_pr_identity(repo.pr)) is not None:
        slugs.add(parsed_pr[0].lower())
    info = bare_gh_info(repos_dir() / f"{repo.name}.git")
    if info is not None and info.gh_slug:
        slugs.add(info.gh_slug.lower())
    return slugs


def _infer_odoo_base(repo: ResolvedRepo) -> str | None:
    """Infer `origin/<series>` from an Odoo branch name (e.g. `18.0-fix` -> `origin/18.0`)."""
    if repo.base is not None or not repo.branch:
        return None
    if repo.name not in ODOO_NAMES and not (_repo_slugs(repo) & ODOO_SLUGS):
        return None
    match = ODOO_SERIES_PREFIX_RE.match(repo.branch)
    if not match:
        return None
    series = match.group("series")
    bare = repos_dir() / f"{repo.name}.git"
    if bare.is_dir() and git.remote_has_branch_or_cached(bare, "origin", series, prefer_cache=True):
        return f"origin/{series}"
    return None


@hookimpl
def ow_resolve_target(
    target: str,
    known_repos: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
) -> ResolvedTarget | None:
    """Resolve `@<base>` into `odoo.default_repos` detached on `<base>`."""
    part = target.strip()
    if not part.startswith("@"):
        return None
    raw_base = part[1:].strip()
    if not raw_base:
        raise UsageError(f"base ref cannot be empty in {part!r}")
    default_repos = tuple(dict.fromkeys(cast(list[str], settings["odoo.default_repos"])))
    if not default_repos:
        raise UsageError(
            f"cannot expand {part!r}: no default repos configured (set odoo.default_repos or specify <repo>@{raw_base})"
        )
    resolved_repos: list[ResolvedRepo] = []
    for rname in default_repos:
        known = known_repos.get(rname)
        bare = repos_dir() / f"{rname}.git"
        if not bare.is_dir() and known is None:
            raise UsageError(
                f"unknown default repo {rname!r}; register it with `ow repo add {rname} <url>`"
            )
        qualified_base = bases.parse_base_for(raw_base, rname)
        resolved_repos.append(
            ResolvedRepo(
                name=rname,
                url=known.url if known else None,
                branch=None,
                base=qualified_base,
                explicit=True,
            )
        )
    short_name = raw_base.split("/", 1)[-1]
    return ResolvedTarget(
        name=stacks.sanitize_slug(short_name, "workspace"),
        repos=tuple(resolved_repos),
        source=f"base:{raw_base}",
    )


@hookimpl
def ow_is_base_branch(repo: str, branch: str) -> bool | None:
    """Recognize Odoo release/series branches (`18.0`, `saas-18.3`, `master`, `main`)."""
    if is_odoo_series(branch):
        return True
    return None


_STACK_PORT_DEFAULTS = (("odoo.http_port", 8069), ("odoo.gevent_port", 8072))


@hookimpl
def ow_stack_values(
    ws: Workspace | None,
    ws_values: Mapping[str, object],
    siblings: Sequence[Workspace],
    pending: Sequence[Mapping[str, object]],
    global_settings: Mapping[str, object],
) -> dict[str, object] | None:
    """Pin `odoo.http_port` and `odoo.gevent_port` (non-colliding), and `odoo.db_name` on promotion.

    A promoted workspace (`ws`) keeps its current database name and ports; a new stack gets the
    next free ports not used by `siblings`, `pending` stacks or its own explicit values.
    """
    if ws_values.get("odoo.enabled") is False:
        return None

    pinned: dict[str, object] = {}
    if ws is not None and "odoo.db_name" not in ws_values:
        effective_db = ws.settings.get("odoo.db_name")
        pinned["odoo.db_name"] = (
            effective_db if isinstance(effective_db, str) and effective_db else ws.name
        )

    used_ports: set[int] = set()
    for settings in (*(s.settings for s in siblings), *pending, ws_values):
        for key, _fallback in _STACK_PORT_DEFAULTS:
            if (port := config.as_int(settings.get(key))) is not None:
                used_ports.add(port)

    for key, fallback in _STACK_PORT_DEFAULTS:
        if key in ws_values:
            continue
        current = config.as_int(ws.settings.get(key)) if ws is not None else None
        if current is not None:
            port = current
        else:
            base_port = config.as_int(global_settings.get(key))
            port = stacks.next_free_port(
                used_ports, base_port=base_port if base_port is not None else fallback
            )
        pinned[key] = port
        used_ports.add(port)
    return pinned or None


@hookimpl
def ow_stack_name(ws: Workspace) -> str | None:
    """Suggest `odoo` when promoting a workspace containing an Odoo core checkout."""
    if any(_is_odoo_core(repo.path) for repo in ws.repos):
        return "odoo"
    return None


@hookimpl
def ow_stack_info(ws: Workspace) -> StackInfo | None:
    """Expose the HTTP and gevent ports."""
    ports = {
        label: port
        for label, key in (("http", "odoo.http_port"), ("gevent", "odoo.gevent_port"))
        if (port := config.as_int(ws.settings.get(key))) is not None
    }
    if not ports:
        return None
    return StackInfo(ports=ports, note=f"port {ports['http']}" if "http" in ports else None)


@hookimpl
def ow_expand_workspace_repos(
    repos: tuple[ResolvedRepo, ...],
    known_repos: Mapping[str, KnownRepo],
    settings: Mapping[str, object],
) -> tuple[ResolvedRepo, ...] | None:
    """Infer Odoo series bases for branch targets and ensure addon targets get an Odoo core repo."""
    updated: list[ResolvedRepo] = []
    changed = False
    for r in repos:
        inferred = _infer_odoo_base(r)
        if inferred is not None:
            updated.append(replace(r, base=inferred))
            changed = True
        else:
            updated.append(r)

    names = {r.name for r in updated}
    slugs: set[str] = set()
    for r in updated:
        slugs.update(_repo_slugs(r))
    if names & ODOO_CORE_NAMES or slugs & ODOO_CORE_SLUGS:
        return tuple(updated) if changed else None
    if not (names & ODOO_ADDON_NAMES) and not (slugs & ODOO_ADDON_SLUGS):
        return tuple(updated) if changed else None

    base = next((r.base for r in updated if r.base), None)
    core_known = known_repos.get(ODOO_CORE.name)
    core_url = core_known.url if core_known is not None else ODOO_CORE.clone_url
    core_name = ODOO_CORE.name
    if not (repos_dir() / f"{ODOO_CORE.name}.git").is_dir() and (repos_dir() / f"{ODOO_CORE.repo}.git").is_dir():
        core_name = ODOO_CORE.repo
    core_repo = ResolvedRepo(name=core_name, url=core_url, branch=None, base=base, explicit=False)
    return (core_repo, *updated)


@hookimpl
def ow_runner(ws: Workspace, args: tuple[str, ...]) -> RunCommand | None:
    """Run Odoo locally using the workspace's resolved Python virtualenv (or `mise exec`)."""
    core, core_diagnostics = _get_core(ws)
    if core_diagnostics or core is None:
        return None

    venv_python = _workspace_venv(ws, core).path / "bin" / "python"
    odoorc = ws.root / "odoorc"
    odoo_bin = core.path / "odoo-bin"
    env = {"ODOO_RC": str(odoorc)}

    if venv_python.exists():
        argv = (str(venv_python), str(odoo_bin), "-c", str(odoorc), *args)
    elif ws.settings.get("mise.enabled", True) and shutil.which("mise"):
        argv = ("mise", "exec", "--", "python", str(odoo_bin), "-c", str(odoorc), *args)
    else:
        argv = ("python3", str(odoo_bin), "-c", str(odoorc), *args)

    return RunCommand(argv=argv, cwd=ws.root, env=env)


@hookimpl
def ow_known_repos() -> list[KnownRepo]:
    """Provide known Odoo repositories."""
    return [KnownRepo(name=repo.name, url=repo.clone_url) for repo in ODOO_REPOS]
