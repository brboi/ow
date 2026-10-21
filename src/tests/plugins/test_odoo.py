"""Tests for the odoo plugin."""

from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
from types import MappingProxyType

import pytest
import tomllib

from ow.core.config import Layer, resolve_settings
from ow.core.plugins import GeneratedFile, load_plugins
from ow.core.workspace import Repo, Workspace
from ow.plugins import odoo

# The default `odoo.admin_passwd`.
_PASSWORD = "Password"


def _write(root: Path, files: dict[str, str]) -> None:
    """Materialise `lab.odoo_files(...)` (or any tree) under `root`."""
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def _repos(*specs: tuple[str, Path]) -> tuple[Repo, ...]:
    return tuple(
        Repo(name=name, path=path, branch="16.0", base="origin/16.0")
        for name, path in specs
    )


def _workspace(
    root: Path,
    repos: tuple[Repo, ...],
    overrides: dict[str, object] | None = None,
) -> Workspace:
    """A workspace whose settings come from `resolve_settings` over empty layers."""
    declared = {setting.key: setting for setting in load_plugins().settings()}
    empty = Workspace(root=root, name=root.name, repos=repos, settings=MappingProxyType({}))
    values, _ = resolve_settings(
        declared,
        Layer(values={}, unknown={}, wrong_type=()),
        Layer(values=dict(overrides or {}), unknown={}, wrong_type=()),
        empty,
    )
    return Workspace(
        root=root, name=root.name, repos=repos, settings=MappingProxyType(values)
    )


def _files(result: list) -> list[GeneratedFile]:
    return [item for item in result if isinstance(item, GeneratedFile)]


def _diagnostics(result: list) -> list:
    return [item for item in result if not isinstance(item, GeneratedFile)]


def _core_workspace(
    lab,
    name: str = "myws",
    python: tuple[tuple[int, int], ...] = ((3, 10), (3, 12)),
    with_demo: bool = True,
    overrides: dict[str, object] | None = None,
) -> Workspace:
    """A workspace holding one Odoo core repo named `community`."""
    root = lab.ws(name)
    core = root / "community"
    _write(core, lab.odoo_files(python=python, with_demo=with_demo))
    return _workspace(root, _repos(("community", core)), overrides)


def test_odoo_writes_the_four_files_in_order(lab):
    """The four outputs, in order, with the odoorc mode."""
    ws = _core_workspace(lab)

    result = odoo.ow_generated_files(ws, lambda hook: [])
    files = _files(result)

    assert [f.path.as_posix() for f in files] == [
        "odoorc",
        "odools.toml",
        "pyrightconfig.json",
        "requirements-dev.txt",
    ]
    assert files[0].mode == 0o600
    assert _diagnostics(result) == []


def test_odoo_odoorc_holds_the_expected_keys(lab):
    """odoorc parses as configparser and holds the v3.0.0a1 key set."""
    ws = _core_workspace(lab, name="myws")
    root = ws.root
    core = root / "community"

    result = odoo.ow_generated_files(ws, lambda hook: [])
    odoorc = next(f for f in _files(result) if f.path.as_posix() == "odoorc")

    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(odoorc.data.decode("utf-8"))
    options = parser["options"]

    assert options["http_port"] == "8069"
    assert options["addons_path"] == (
        f"{core}/addons,{core}/odoo/addons"
    )
    assert options["data_dir"] == f"{root}/.odoo"
    assert options["admin_passwd"] == _PASSWORD
    assert options["db_name"] == "myws"
    assert options["dbfilter"] == "^myws$"
    assert options["db_host"] == "localhost"
    assert options["db_port"] == "5432"
    assert options["db_user"] == "odoo"
    assert options["db_password"] == "odoo"
    assert options["smtp_server"] == "localhost"
    assert options["smtp_port"] == "25"
    assert set(options) == {
        "http_port",
        "addons_path",
        "data_dir",
        "admin_passwd",
        "db_name",
        "dbfilter",
        "db_host",
        "db_port",
        "db_user",
        "db_password",
        "smtp_server",
        "smtp_port",
    }


def test_odoo_odoorc_escapes_dbfilter(lab):
    """`dbfilter` is `^<re.escape(db_name)>$`."""
    ws = _core_workspace(lab, name="myws", overrides={"odoo.db_name": "a.b"})

    result = odoo.ow_generated_files(ws, lambda hook: [])
    odoorc = next(f for f in _files(result) if f.path.as_posix() == "odoorc")

    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(odoorc.data.decode("utf-8"))
    assert parser["options"]["dbfilter"] == "^a\\.b$"
    assert parser["options"]["db_name"] == "a.b"


def test_odoo_odools_and_pyright_shapes(lab):
    """odools.toml paths are relative to the file, pyright pins the venv only."""
    ws = _core_workspace(lab, name="myws")

    result = odoo.ow_generated_files(ws, lambda hook: [])
    files = {f.path.as_posix(): f for f in _files(result)}

    odools = tomllib.loads(files["odools.toml"].data.decode("utf-8"))
    assert odools["config"][0]["name"] == "[Odoo Workspace] myws"
    assert odools["config"][0]["python_path"] == ".venv/bin/python"
    assert odools["config"][0]["odoo_path"] == "./community"
    assert odools["config"][0]["addons_paths"] == [
        "./community/addons",
        "./community/odoo/addons",
    ]

    pyright = json.loads(files["pyrightconfig.json"].data.decode("utf-8"))
    assert pyright == {"venvPath": ".", "venv": ".venv"}
    assert "pythonVersion" not in pyright

    assert files["requirements-dev.txt"].data == b"inotify\n"


def test_odoo_addons_path_order(lab):
    """.local, loose root, non-core repos in order, the core's two dirs."""
    root = lab.ws("ordered")
    core = root / "community"
    _write(core, lab.odoo_files())
    _write(root / ".local" / "local_addon", {"__manifest__.py": "{'name': 'Local'}"})
    _write(root / "loose_addon", {"__manifest__.py": "{'name': 'Loose'}"})
    _write(root / "enterprise" / "my_addon", {"__manifest__.py": "{'name': 'My'}"})

    repos = _repos(
        ("community", core),
        ("enterprise", root / "enterprise"),
        # Two names for one directory: the walk must not list it twice.
        ("enterprise-again", root / "enterprise"),
    )
    ws = _workspace(root, repos)

    result = odoo.ow_generated_files(ws, lambda hook: [])
    odoorc = next(f for f in _files(result) if f.path.as_posix() == "odoorc")

    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(odoorc.data.decode("utf-8"))
    paths = parser["options"]["addons_path"].split(",")

    assert paths == [
        f"{root}/.local",
        f"{root}",
        f"{root}/enterprise",
        f"{core}/addons",
        f"{core}/odoo/addons",
    ]
    assert len(paths) == len(set(paths))


def test_odoo_debug_args_default_with_demo_core(lab):
    """A core declaring `--with-demo` gets `--dev=all` only."""
    ws = _core_workspace(lab, with_demo=True)

    contribution = odoo.editors_contribution(ws)
    assert contribution is not None
    assert contribution.launches[0].name == "Run Instance With Debug"
    assert contribution.launches[0].args == ("--dev=all",)
    assert contribution.launches[1].args == (f"--test-tags={ws.name}",)


def test_odoo_debug_args_default_without_demo_core(lab):
    """A core declaring `--without-demo` gets `--without-demo=all` too."""
    ws = _core_workspace(lab, with_demo=False)

    contribution = odoo.editors_contribution(ws)
    assert contribution is not None
    assert contribution.launches[0].args == ("--dev=all", "--without-demo=all")


def test_odoo_two_cores_one_warning_and_nothing_else(lab):
    """Two cores: one warning, no files, no answers anywhere."""
    root = lab.ws("two")
    _write(root / "community", lab.odoo_files())
    _write(root / "enterprise", lab.odoo_files())
    ws = _workspace(
        root,
        _repos(("community", root / "community"), ("enterprise", root / "enterprise")),
        # An invalid port must not add a second warning while the core is broken.
        {"odoo.http_port": 0},
    )

    diagnostics = odoo.ow_diagnostics(ws)
    assert len(diagnostics) == 1
    assert diagnostics[0].level == "warning"
    assert "Multiple Odoo cores" in diagnostics[0].text
    assert "community" in diagnostics[0].text and "enterprise" in diagnostics[0].text

    assert odoo.ow_generated_files(ws, lambda hook: []) == []
    assert odoo.mise_environment(ws) is None
    assert odoo.editors_contribution(ws) is None


def test_odoo_malformed_bound_one_warning_and_nothing_else(lab):
    """An unreadable Python range: one warning, no contribution."""
    root = lab.ws("malformed")
    files = lab.odoo_files()
    del files["odoo/release.py"]
    _write(root / "community", files)
    ws = _workspace(root, _repos(("community", root / "community")))

    diagnostics = odoo.ow_diagnostics(ws)
    assert len(diagnostics) == 1
    assert diagnostics[0].level == "warning"
    assert "Python version bounds" in diagnostics[0].text

    assert odoo.ow_generated_files(ws, lambda hook: []) == []
    assert odoo.mise_environment(ws) is None
    assert odoo.editors_contribution(ws) is None


def test_odoo_unreadable_config_one_warning_and_nothing_else(lab):
    """A config.py without a demo flag: one warning naming that cause."""
    root = lab.ws("nodesc")
    files = lab.odoo_files()
    files["odoo/tools/config.py"] = "# nothing declares --with-demo here\n"
    _write(root / "community", files)
    ws = _workspace(root, _repos(("community", root / "community")))

    diagnostics = odoo.ow_diagnostics(ws)
    assert len(diagnostics) == 1
    assert diagnostics[0].level == "warning"
    assert "--with-demo" in diagnostics[0].text

    assert odoo.ow_generated_files(ws, lambda hook: []) == []
    assert odoo.editors_contribution(ws) is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_odoo_unreadable_directory_skipped_silently(lab):
    """A directory the walk cannot read contributes nothing and no warning."""
    root = lab.ws("unreadable")
    _write(root / "community", lab.odoo_files())
    locked = root / "enterprise" / "locked"
    locked.mkdir(parents=True)
    (locked / "hidden_addon").mkdir()
    (locked / "hidden_addon" / "__manifest__.py").write_text("{'name': 'Hidden'}")
    locked.chmod(0o000)

    try:
        ws = _workspace(
            root,
            _repos(("community", root / "community"), ("enterprise", root / "enterprise")),
        )
        result = odoo.ow_generated_files(ws, lambda hook: [])

        assert _diagnostics(result) == []
        odoorc = next(f for f in _files(result) if f.path.as_posix() == "odoorc")
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(odoorc.data.decode("utf-8"))
        assert "locked" not in parser["options"]["addons_path"]
    finally:
        locked.chmod(0o755)


def test_odoo_addon_path_with_comma_skipped_with_warning(lab):
    """A path that cannot round-trip through addons_path is skipped, with a warning."""
    root = lab.ws("comma")
    core = root / "community"
    _write(core, lab.odoo_files())
    _write(
        root / "enterprise" / "odd,addons" / "my_addon",
        {"__manifest__.py": "{'name': 'My'}"},
    )
    ws = _workspace(
        root,
        _repos(("community", core), ("enterprise", root / "enterprise")),
    )

    result = odoo.ow_generated_files(ws, lambda hook: [])

    diagnostics = _diagnostics(result)
    assert len(diagnostics) == 1
    assert diagnostics[0].level == "warning"
    assert "addons_path" in diagnostics[0].text

    odoorc = next(f for f in _files(result) if f.path.as_posix() == "odoorc")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(odoorc.data.decode("utf-8"))
    assert "," in parser["options"]["addons_path"]
    assert "my_addon" not in parser["options"]["addons_path"]
    assert parser["options"]["addons_path"] == (
        f"{core}/addons,{core}/odoo/addons"
    )


def test_odoo_invalid_debug_args_warn_and_fall_back(lab):
    """A debug_args item holding a line break warns and falls back per key."""
    ws = _core_workspace(lab, overrides={"odoo.debug_args": ["--dev=all", "bad\narg"]})

    diagnostics = odoo.ow_diagnostics(ws)
    assert [d.level for d in diagnostics] == ["warning"]
    assert "odoo.debug_args" in diagnostics[0].text

    contribution = odoo.editors_contribution(ws)
    assert contribution is not None
    assert contribution.launches[0].args == ("--dev=all",)


@pytest.mark.parametrize("port", [0, 70000])
def test_odoo_invalid_port_warns_and_falls_back(lab, port):
    """An out-of-range port warns and the odoorc carries the default."""
    ws = _core_workspace(lab, overrides={"odoo.http_port": port})

    diagnostics = odoo.ow_diagnostics(ws)
    assert [d.level for d in diagnostics] == ["warning"]
    assert "odoo.http_port" in diagnostics[0].text

    result = odoo.ow_generated_files(ws, lambda hook: [])
    odoorc = next(f for f in _files(result) if f.path.as_posix() == "odoorc")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(odoorc.data.decode("utf-8"))
    assert parser["options"]["http_port"] == "8069"


def test_odoo_no_core_is_silent(lab):
    """Without an Odoo core the plugin says and does nothing at all."""
    root = lab.ws("plain")
    root.mkdir(parents=True)
    ws = _workspace(root, (), {"odoo.http_port": 0})

    assert odoo.ow_diagnostics(ws) == []
    assert odoo.ow_generated_files(ws, lambda hook: []) == []
    assert odoo.mise_environment(ws) is None
    assert odoo.editors_contribution(ws) is None


def test_odoo_mise_environment_answers(lab):
    """The mise answer carries the core path, range, postinstall and shell alias."""
    ws = _core_workspace(lab)

    answer = odoo.mise_environment(ws)
    assert answer is not None
    assert answer.env == {"ODOO_RC": "{{config_root}}/odoorc"}
    assert answer.path == ("{{config_root}}/community",)
    assert answer.python == ((3, 10), (3, 12))
    assert answer.postinstall == (
        "pip install -r {{config_root}}/community/requirements.txt"
        " -r {{config_root}}/requirements-dev.txt",
    )
    assert answer.aliases == {"osh": "odoo-bin shell --no-http -c $ODOO_RC"}


def test_odoo_known_repos():
    """The built-in known repos are community, enterprise and design-themes."""
    names = [repo.name for repo in odoo.ow_known_repos()]
    assert names == ["community", "enterprise", "design-themes"]


def test_odoo_debug_args_setting_default_tracks_the_core(lab):
    """The resolved setting itself carries the demo-aware default."""
    with_demo = _core_workspace(lab, name="demo", with_demo=True)
    without_demo = _core_workspace(lab, name="nodemo", with_demo=False)

    assert with_demo.settings["odoo.debug_args"] == ["--dev=all"]
    assert without_demo.settings["odoo.debug_args"] == [
        "--dev=all",
        "--without-demo=all",
    ]
    assert with_demo.settings["odoo.db_name"] == "demo"
