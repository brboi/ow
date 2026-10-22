"""Odoo plugin: project configuration and debugging support."""

from __future__ import annotations

import ast
import re
from configparser import ConfigParser
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Mapping

import tomli_w

from ow.core.plugins import (
    Diagnostic,
    GeneratedFile,
    KnownRepo,
    Setting,
    hookimpl,
)
from ow.specs.editors import DebugLaunch, EditorContribution, json_text
from ow.specs.mise import MiseEnvironment

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


def _read_python_bounds(core_path: Path) -> tuple[int, int] | None:
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
                python_min = _literal_int_pair(min_assigns[0])
            if max_assigns:
                python_max = _literal_int_pair(max_assigns[0])

    # Fall back to odoo/__init__.py if needed
    if (python_min is None or python_max is None) and init_path.exists():
        tree = _parse_module(init_path)
        if tree:
            if python_min is None:
                assigns = _module_assignments(tree, "MIN_PY_VERSION")
                if assigns:
                    python_min = _literal_int_pair(assigns[0])
            if python_max is None:
                assigns = _module_assignments(tree, "MAX_PY_VERSION")
                if assigns:
                    python_max = _literal_int_pair(assigns[0])

    if python_min and python_max:
        return (python_min, python_max)
    return None


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


_PORT_KEYS = ("odoo.http_port", "odoo.db_port", "odoo.smtp_port")
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
        if isinstance(val, int) and not 1 <= val <= 65535:
            return default, Diagnostic("warning", f"{key} must be 1-65535, using default")
        return (val if isinstance(val, int) else default), None

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
            help="Debug arguments for Odoo",
        ),
        Setting(
            key="odoo.debug_test_args",
            type=list,
            default=lambda ws: [f"--test-tags={ws.name}"],
            help="Debug test arguments",
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
    ws: Workspace, answers: callable
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

    odools_content = tomli_w.dumps({
        "config": [
            {
                "name": f"[Odoo Workspace] {ws.name}",
                "python_path": ".venv/bin/python",
                "odoo_path": f"./{core.name}",
                "addons_paths": [
                    f"./{p.relative_to(ws.root).as_posix()}" for p in addon_paths
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
        "venvPath": ".",
        "venv": ".venv",
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
def mise_environment(ws: Workspace) -> MiseEnvironment | None:
    """Contribute mise environment from Odoo."""
    core, core_diagnostics = _get_core(ws)
    if core_diagnostics or not core:
        return None

    env = {"ODOO_RC": "{{config_root}}/odoorc"}
    path = (f"{{{{config_root}}}}/{core.name}",)
    postinstall = (f"python -m ensurepip --default-pip && pip install -r \"{{{{config_root}}}}/{core.name}/requirements.txt\" -r \"{{{{config_root}}}}/requirements-dev.txt\"",)
    aliases = {"osh": "odoo-bin shell --no-http -c \"$ODOO_RC\""}

    return MiseEnvironment(
        env=env,
        path=path,
        python=_read_python_bounds(core.path),
        postinstall=postinstall,
        aliases=aliases,
    )


@hookimpl
def editors_contribution(ws: Workspace) -> EditorContribution | None:
    """Contribute debug launches and settings for editors."""
    core, core_diagnostics = _get_core(ws)
    if core_diagnostics or not core:
        return None

    settings = ws.settings
    debug_args, _ = _get_setting("odoo.debug_args", settings, ws)
    debug_test_args, _ = _get_setting("odoo.debug_test_args", settings, ws)

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
    )


@hookimpl
def ow_known_repos() -> list[KnownRepo]:
    """Provide known Odoo repositories."""
    return [
        KnownRepo(name="community", url="git@github.com:odoo/odoo.git"),
        KnownRepo(name="enterprise", url="git@github.com:odoo/enterprise.git"),
        KnownRepo(name="design-themes", url="git@github.com:odoo/design-themes.git"),
    ]
