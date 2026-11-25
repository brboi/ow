"""Built-in plugins import what `ow.plugin_api` exports from `ow.plugin_api`, like external plugins do."""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import ow.plugin_api as plugin_api
import ow.plugins

_PLUGINS_DIR = Path(ow.plugins.__file__).parent


def _api_objects() -> dict[int, str]:
    return {id(getattr(plugin_api, name)): name for name in plugin_api.__all__}


def _offending_imports() -> list[str]:
    api = _api_objects()
    offenders = []
    for path in sorted(_PLUGINS_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or node.level or node.module is None:
                continue
            if node.module == "ow.plugin_api" or not node.module.startswith("ow."):
                continue
            module = importlib.import_module(node.module)
            for alias in node.names:
                name = api.get(id(getattr(module, alias.name, None)))
                if name is not None:
                    offenders.append(
                        f"{path.relative_to(_PLUGINS_DIR.parent.parent)}:{node.lineno}: "
                        f"`from {node.module} import {alias.name}` -> use `from ow.plugin_api import {name}`"
                    )
    return offenders


def test_builtins_import_api_names_from_plugin_api() -> None:
    assert _offending_imports() == []


def test_guard_detects_core_imports(tmp_path: Path, monkeypatch) -> None:
    fake = tmp_path / "fake.py"
    fake.write_text("from ow.core.plugins import hookimpl\nfrom ow.specs.services import SmtpService\n")
    monkeypatch.setattr(sys.modules[__name__], "_PLUGINS_DIR", tmp_path)
    offenders = _offending_imports()
    assert len(offenders) == 2
    assert "ow.plugin_api import hookimpl" in offenders[0]
    assert "ow.plugin_api import SmtpService" in offenders[1]


def test_builtins_import_in_fresh_process() -> None:
    code = "import ow.plugin_api, ow.plugins; assert len(ow.plugins.builtin_plugins()) > 0"
    subprocess.run([sys.executable, "-c", code], check=True)
    subprocess.run([sys.executable, "-c", "import ow.plugins.mise"], check=True)


def _imported_modules(path: Path) -> list[tuple[int, str]]:
    modules = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module is not None:
            modules.append((node.lineno, node.module))
            modules.extend((node.lineno, f"{node.module}.{alias.name}") for alias in node.names)
    return modules


def test_core_does_not_import_plugins() -> None:
    """Nothing outside `ow.plugins` imports `ow.plugins.*`, except `load_plugins` calling `builtin_plugins`."""
    ow_dir = _PLUGINS_DIR.parent
    offenders = []
    for path in sorted(ow_dir.rglob("*.py")):
        if path.is_relative_to(_PLUGINS_DIR):
            continue
        for lineno, module in _imported_modules(path):
            if module == "ow.plugins" or module.startswith("ow.plugins."):
                if (path.relative_to(ow_dir).as_posix(), module) in {
                    ("core/plugins.py", "ow.plugins"),
                    ("core/plugins.py", "ow.plugins.builtin_plugins"),
                }:
                    continue
                offenders.append(f"{path.relative_to(ow_dir)}:{lineno}: imports {module}")
    assert offenders == []
