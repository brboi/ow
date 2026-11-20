"""Odoo's workspace files and Python environment: `odoorc`, `odools.toml`, `pyrightconfig.json`, editors."""

from __future__ import annotations

import re
from configparser import ConfigParser
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import tomli_w

from ow.core.fs import json_text
from ow.core.plugins import Diagnostic, GeneratedFile
from ow.plugins.odoo.addons import workspace_addon_paths
from ow.plugins.odoo.release import read_odoo_series, read_python_bounds
from ow.plugins.odoo.settings import get_setting
from ow.specs.editors import DebugLaunch, EditorContribution
from ow.specs.python_env import PythonEnvironment, WorkspaceVenv, workspace_venv

if TYPE_CHECKING:
    from ow.core.workspace import Repo, Workspace


def python_environment(core: Repo) -> PythonEnvironment:
    """The Python environment the Odoo checkout `core` requires."""
    return PythonEnvironment(
        env_paths={"ODOO_RC": "odoorc"},
        path=(core.name,),
        python=read_python_bounds(core.path),
        venv_key=read_odoo_series(core.path),
        requirements=(f"{core.name}/requirements.txt", "requirements-dev.txt"),
        aliases={"osh": "odoo-bin shell --no-http -c \"$ODOO_RC\""},
    )


def odoo_venv(ws: Workspace, core: Repo) -> WorkspaceVenv:
    """Virtualenv of an Odoo workspace."""
    return workspace_venv(ws, python_environment(core))


def generated_files(ws: Workspace, core: Repo) -> list[GeneratedFile | Diagnostic]:
    """`odoorc`, `odools.toml`, `pyrightconfig.json` and `requirements-dev.txt` for `ws`."""
    diagnostics: list[Diagnostic] = []
    files: list[GeneratedFile] = []

    # `addons_path` is comma-separated and absolute: a path holding a comma or a
    # line break could never round-trip through it.
    addon_paths: list[Path] = []
    for path in workspace_addon_paths(ws, core):
        text = path.as_posix()
        if "," in text or "\n" in text or "\r" in text:
            diagnostics.append(
                Diagnostic("warning", f"addon path cannot be represented in addons_path: {text}")
            )
            continue
        addon_paths.append(path)

    settings = ws.settings

    http_port, _ = get_setting("odoo.http_port", settings, ws)
    gevent_port, _ = get_setting("odoo.gevent_port", settings, ws)
    db_name, _ = get_setting("odoo.db_name", settings, ws)
    db_host, _ = get_setting("odoo.db_host", settings, ws)
    db_port, _ = get_setting("odoo.db_port", settings, ws)
    db_user, _ = get_setting("odoo.db_user", settings, ws)
    db_password, _ = get_setting("odoo.db_password", settings, ws)
    admin_passwd, _ = get_setting("odoo.admin_passwd", settings, ws)
    smtp_server, _ = get_setting("odoo.smtp_server", settings, ws)
    smtp_port, _ = get_setting("odoo.smtp_port", settings, ws)

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

    venv = odoo_venv(ws, core)
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


def editor_contribution(ws: Workspace, core: Repo) -> EditorContribution:
    """Debug launches and settings for editors."""
    settings = ws.settings
    debug_args, _ = get_setting("odoo.debug_args", settings, ws)
    debug_test_args, _ = get_setting("odoo.debug_test_args", settings, ws)
    venv = odoo_venv(ws, core)

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
