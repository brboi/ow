"""Fixed, ordered workspace-file generators.

One typed `GenerationContext` in, a fixed tuple of `GeneratedFile` out. Pure
serialization: no disk reads, no writes, no Jinja, no user template tree, no
dynamic output discovery. Odoo-only outputs return `data=None` when
`context.core is None`; both editor settings files are always generated.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from configparser import ConfigParser
from dataclasses import dataclass
from io import StringIO
from pathlib import Path, PurePosixPath

from ow.utils.odoo import OdooInfo
from ow.utils.options import EffectiveOptions

import tomli_w


@dataclass(frozen=True)
class GeneratedFile:
    path: PurePosixPath
    data: bytes | None
    mode: int = 0o644


@dataclass(frozen=True)
class GenerationContext:
    root: Path
    repos: tuple[str, ...]
    core: OdooInfo | None
    options: EffectiveOptions
    addon_paths: tuple[Path, ...]
    services_compose: Path
    git_common_dirs: tuple[Path, ...]
    use_dev_requirements: bool


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def exec_task(argv: list[str]) -> str:
    return "#!/bin/sh\nexec " + shlex.join(argv) + ' "$@"\n'


_SEARCH_EXCLUDE_GLOBS: dict[str, bool] = {
    "**/node_modules": True,
    "**/bower_components": True,
    "**/*.code-search": True,
    "**/.git": True,
    "**/.svn": True,
    "**/.hg": True,
    "**/.jj": True,
    "**/CVS": True,
    "**/.DS_Store": True,
    "**/Thumbs.db": True,
    "**/.classpath": True,
    "**/.settings": True,
    "**/*.po": True,
    "**/*.pot": True,
    "**/.venv": True,
}

_ZED_FILE_SCAN_EXCLUSIONS: tuple[str, ...] = (
    "**/.git", "**/.svn", "**/.hg", "**/.jj", "**/CVS", "**/.DS_Store",
    "**/Thumbs.db", "**/.classpath", "**/.settings", "**/*.po", "**/*.pot",
    "**/node_modules", "**/.venv",
)

_REQUIREMENTS_DEV_CONTENT = "inotify\n"

_DEBUGPY_PYTHON_ARGS: tuple[str, ...] = ("-Xfrozen_modules=off",)


def _relative_addon_paths(context: GenerationContext) -> tuple[str, ...]:
    """Workspace-relative POSIX addon paths, rejecting comma/CR/LF.

    Odoo's `addons_path` is comma-separated: a path containing a comma or a
    literal newline could never round-trip through it.
    """
    relative: list[str] = []
    for path in context.addon_paths:
        rel = path.relative_to(context.root).as_posix()
        if "," in rel or "\r" in rel or "\n" in rel:
            raise ValueError(
                f"addon path cannot be represented in addons_path: {path}"
            )
        relative.append(rel)
    return tuple(relative)


def _sandbox_task(
    task_name: str, script: Path, context: GenerationContext
) -> dict[str, object]:
    if task_name == "firejail-claude":
        argv = (
            ["firejail", "--profile=" + str(script), "--whitelist=" + str(context.root)]
            + ["--whitelist=" + str(directory) for directory in context.git_common_dirs]
            + ["claude"]
        )
        return {"run": exec_task(argv)}
    argv = [str(script)] + [
        part
        for directory in context.git_common_dirs
        for part in ("--add-dir", str(directory))
    ]
    return {"run": exec_task(argv), "env": {"ODOO_BASE": "{{config_root}}"}}


def _mise_fragment(context: GenerationContext) -> str:
    workspace_root = "{{config_root}}"
    path_entries: list[str] = [workspace_root]
    underscore: dict[str, object] = {}
    postinstall: str | None = None

    tools: dict[str, object] = {}
    if context.options.python is not None:
        tools["python"] = context.options.python
        underscore["python"] = {"venv": {"path": ".venv", "create": True}}
        postinstall = "python -m ensurepip --default-pip"

    env: dict[str, object] = {"OW_WORKSPACE": workspace_root}
    shell_alias: dict[str, object] = {}
    tasks: dict[str, object] = {}

    if context.core is not None:
        core = context.core
        env["ODOO_RC"] = f"{workspace_root}/odoorc"
        env["COMPOSE_FILE"] = str(context.services_compose)
        path_entries.append(f"{workspace_root}/{core.alias}")
        shell_alias["osh"] = "odoo-bin shell --no-http -c $ODOO_RC"

        core_requirements = '"$OW_WORKSPACE"/' + shlex.quote(core.alias + "/requirements.txt")
        pip_operands = ["-r", core_requirements]
        if context.use_dev_requirements:
            pip_operands += ["-r", '"$OW_WORKSPACE/requirements-dev.txt"']
        postinstall = "python -m ensurepip --default-pip && pip install " + " ".join(pip_operands)

        for task_name, script in core.sandbox_paths.items():
            tasks[task_name] = _sandbox_task(task_name, script, context)

    underscore["path"] = path_entries
    env["_"] = underscore

    doc: dict[str, object] = {}
    if tools:
        doc["tools"] = tools
    if postinstall is not None:
        doc["hooks"] = {"postinstall": postinstall}
    doc["env"] = env
    if shell_alias:
        doc["shell_alias"] = shell_alias
    if tasks:
        doc["tasks"] = tasks
    return tomli_w.dumps(doc)


def _vscode_settings(context: GenerationContext) -> str:
    data: dict[str, object] = {}
    if context.core is not None:
        data["Odoo.selectedProfile"] = f"[Odoo Workspace] {context.root.name}"
    data["search.exclude"] = dict(_SEARCH_EXCLUDE_GLOBS)
    return json_text(data)


def _zed_settings(context: GenerationContext) -> str:
    inclusions = [f"{alias}/**" for alias in context.repos] + ["mise/conf.d/*.toml"]
    if context.core is not None:
        inclusions += ["odools.toml", "pyrightconfig.json"]
    data: dict[str, object] = {
        "file_scan_inclusions": inclusions,
        "file_scan_exclusions": list(_ZED_FILE_SCAN_EXCLUSIONS),
    }
    if context.core is not None:
        data["lsp"] = {
            "odoo": {
                "settings": {
                    "Odoo.selectedProfile": f"[Odoo Workspace] {context.root.name}",
                },
            },
        }
    return json_text(data)


def _odoorc(context: GenerationContext) -> str | None:
    if context.core is None:
        return None
    settings = context.options.odoo
    ws_name = context.root.name

    parser = ConfigParser(interpolation=None)
    parser["options"] = {
        "http_port": str(settings.http_port),
        "addons_path": ",".join(_relative_addon_paths(context)),
        "data_dir": str(context.root / ".odoo"),
        "admin_passwd": settings.admin_passwd,
        "db_name": ws_name,
        "dbfilter": f"^{re.escape(ws_name)}$",
        "db_host": settings.db_host,
        "db_port": str(settings.db_port),
        "db_user": settings.db_user,
        "db_password": settings.db_password,
        "smtp_server": settings.smtp_server,
        "smtp_port": str(settings.smtp_port),
    }
    buf = StringIO()
    parser.write(buf, space_around_delimiters=True)
    return buf.getvalue()


def _odools_toml(context: GenerationContext) -> str | None:
    if context.core is None:
        return None
    doc = {
        "config": [
            {
                "name": f"[Odoo Workspace] {context.root.name}",
                "python_path": ".venv/bin/python",
                "odoo_path": f"./{context.core.alias}",
                "addons_paths": [f"./{item}" for item in _relative_addon_paths(context)],
            }
        ]
    }
    return tomli_w.dumps(doc)


def _pyrightconfig(context: GenerationContext) -> str | None:
    if context.core is None:
        return None
    data = {
        "venvPath": ".",
        "venv": ".venv",
        "pythonVersion": context.options.python,
        "extraPaths": [f"./{context.core.alias}"],
        "reportArgumentType": "none",
        "reportAttributeAccessIssue": "none",
        "reportMissingImports": "none",
        "reportMissingModuleSource": "none",
        "reportPrivateLocalImportUsage": "none",
        "typeCheckingMode": "off",
    }
    return json_text(data)


def _requirements_dev(context: GenerationContext) -> str | None:
    if context.core is None:
        return None
    return _REQUIREMENTS_DEV_CONTENT


def _vscode_debug_configuration(
    *, name: str, cwd: str, args: tuple[str, ...], python: str, env: dict[str, str]
) -> dict[str, object]:
    return {
        "name": name,
        "type": "debugpy",
        "request": "launch",
        "cwd": cwd,
        "program": "odoo-bin",
        "justMyCode": False,
        "args": list(args),
        "python": python,
        "pythonArgs": list(_DEBUGPY_PYTHON_ARGS),
        "env": env,
    }


def _zed_debug_configuration(
    *, label: str, cwd: str, args: tuple[str, ...], python: str, env: dict[str, str]
) -> dict[str, object]:
    return {
        "label": label,
        "adapter": "Debugpy",
        "request": "launch",
        "console": "integratedTerminal",
        "cwd": cwd,
        "program": "odoo-bin",
        "justMyCode": False,
        "args": list(args),
        "python": python,
        "pythonArgs": list(_DEBUGPY_PYTHON_ARGS),
        "env": env,
    }


def _vscode_launch(context: GenerationContext) -> str | None:
    if context.core is None:
        return None
    settings = context.options.odoo
    alias = context.core.alias
    ws_name = context.root.name
    cwd = "${workspaceFolder}/" + alias
    python = "${workspaceFolder}/.venv/bin/python"
    env = {
        "ODOO_RC": "${workspaceFolder}/odoorc",
        "PYDEVD_DISABLE_FILE_VALIDATION": "1",
        "PYTHON_PATH": "${workspaceFolder}/" + alias,
    }
    data = {
        "version": "0.2.0",
        "configurations": [
            _vscode_debug_configuration(
                name="Run Instance With Debug",
                cwd=cwd,
                args=settings.debug_args,
                python=python,
                env=env,
            ),
            _vscode_debug_configuration(
                name=f"Debug Tests ({ws_name})",
                cwd=cwd,
                args=settings.debug_test_args,
                python=python,
                env=env,
            ),
        ],
    }
    return json_text(data)


def _zed_debug(context: GenerationContext) -> str | None:
    if context.core is None:
        return None
    settings = context.options.odoo
    alias = context.core.alias
    ws_name = context.root.name
    cwd = "${ZED_WORKTREE_ROOT}/" + alias
    python = "${ZED_WORKTREE_ROOT}/.venv/bin/python"
    env = {
        "ODOO_RC": "${ZED_WORKTREE_ROOT}/odoorc",
        "PYDEVD_DISABLE_FILE_VALIDATION": "1",
        "PYTHONPATH": "${ZED_WORKTREE_ROOT}/" + alias,
    }
    data = [
        _zed_debug_configuration(
            label="Run Instance With Debug",
            cwd=cwd,
            args=settings.debug_args,
            python=python,
            env=env,
        ),
        _zed_debug_configuration(
            label=f"Debug Tests ({ws_name})",
            cwd=cwd,
            args=settings.debug_test_args,
            python=python,
            env=env,
        ),
    ]
    return json_text(data)


_GENERATORS: tuple[tuple[PurePosixPath, Callable[[GenerationContext], str | None], int], ...] = (
    (PurePosixPath("mise/conf.d/00-ow.toml"), _mise_fragment, 0o644),
    (PurePosixPath(".vscode/settings.json"), _vscode_settings, 0o644),
    (PurePosixPath(".zed/settings.json"), _zed_settings, 0o644),
    (PurePosixPath("odoorc"), _odoorc, 0o600),
    (PurePosixPath("odools.toml"), _odools_toml, 0o644),
    (PurePosixPath("pyrightconfig.json"), _pyrightconfig, 0o644),
    (PurePosixPath("requirements-dev.txt"), _requirements_dev, 0o644),
    (PurePosixPath(".vscode/launch.json"), _vscode_launch, 0o644),
    (PurePosixPath(".zed/debug.json"), _zed_debug, 0o644),
)


def generate_files(context: GenerationContext) -> tuple[GeneratedFile, ...]:
    """The fixed, ordered set of proposed workspace outputs.

    No disk access: every generator is a pure function of `context`. Odoo-
    only generators return `None` (surfaced as `GeneratedFile.data is None`)
    when `context.core` is `None`.
    """
    return tuple(
        GeneratedFile(
            path=path,
            data=None if text is None else text.encode("utf-8"),
            mode=mode,
        )
        for path, generator, mode in _GENERATORS
        for text in (generator(context),)
    )
