"""Tests for the odoo plugin."""

from __future__ import annotations

import configparser
import json
import os
import tomllib
from pathlib import Path

import pytest

from ow.core.workspace import Repo, Workspace
from ow.plugins import odoo
from ow.specs.services import DatabaseService, SmtpService
from tests.conftest import diagnostics_of, files_of, resolved_workspace
from tests.lab import write_files

# The default `odoo.admin_passwd`.
_PASSWORD = "Password"


def _answers(ws: Workspace):
    """The `answers` callable ow hands hooks, with only the odoo plugin answering `python_environment`."""

    def answers(hook: str) -> list:
        if hook == "python_environment":
            env = odoo.python_environment(ws)
            return [env] if env is not None else []
        return []

    return answers


def _repos(*specs: tuple[str, Path]) -> tuple[Repo, ...]:
    return tuple(
        Repo(name=name, path=path, branch="16.0", base="origin/16.0")
        for name, path in specs
    )


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
    write_files(core, lab.odoo_files(python=python, with_demo=with_demo))
    return resolved_workspace(root, _repos(("community", core)), overrides)


def test_odoo_writes_the_four_files_in_order(lab):
    """The four outputs, in order, with the odoorc mode."""
    ws = _core_workspace(lab)

    result = odoo.ow_generated_files(ws, _answers(ws))
    files = files_of(result)

    assert [f.path.as_posix() for f in files] == [
        "odoorc",
        "odools.toml",
        "pyrightconfig.json",
        "requirements-dev.txt",
    ]
    assert files[0].mode == 0o600
    assert diagnostics_of(result) == []


def test_odoo_odoorc_holds_the_expected_keys(lab):
    """odoorc parses as configparser and holds the v3.0.0a1 key set."""
    ws = _core_workspace(lab, name="myws")
    root = ws.root
    core = root / "community"

    result = odoo.ow_generated_files(ws, _answers(ws))
    odoorc = next(f for f in files_of(result) if f.path.as_posix() == "odoorc")

    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(odoorc.data.decode("utf-8"))
    options = parser["options"]

    assert options["http_port"] == "8069"
    assert options["gevent_port"] == "8072"
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
        "gevent_port",
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

    result = odoo.ow_generated_files(ws, _answers(ws))
    odoorc = next(f for f in files_of(result) if f.path.as_posix() == "odoorc")

    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(odoorc.data.decode("utf-8"))
    assert parser["options"]["dbfilter"] == "^a\\.b$"
    assert parser["options"]["db_name"] == "a.b"


def test_odoo_odools_and_pyright_shapes(lab):
    """odools.toml paths are relative to the file, pyright pins the venv only."""
    ws = _core_workspace(lab, name="myws")

    result = odoo.ow_generated_files(ws, _answers(ws))
    files = {f.path.as_posix(): f for f in files_of(result)}

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
    write_files(core, lab.odoo_files())
    write_files(root / ".local" / "local_addon", {"__manifest__.py": "{'name': 'Local'}"})
    write_files(root / "loose_addon", {"__manifest__.py": "{'name': 'Loose'}"})
    write_files(root / "enterprise" / "my_addon", {"__manifest__.py": "{'name': 'My'}"})

    repos = _repos(
        ("community", core),
        ("enterprise", root / "enterprise"),
        # Two names for one directory: the walk must not list it twice.
        ("enterprise-again", root / "enterprise"),
    )
    ws = resolved_workspace(root, repos)

    result = odoo.ow_generated_files(ws, _answers(ws))
    odoorc = next(f for f in files_of(result) if f.path.as_posix() == "odoorc")

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

    contribution = odoo.editors_contribution(ws, _answers(ws))
    assert contribution is not None
    assert contribution.launches[0].name == "Run Instance With Debug"
    assert contribution.launches[0].args == ("--dev=all",)
    assert contribution.launches[1].args == (f"--test-tags={ws.name}",)


def test_odoo_debug_args_default_without_demo_core(lab):
    """A core declaring `--without-demo` gets `--without-demo=all` too."""
    ws = _core_workspace(lab, with_demo=False)

    contribution = odoo.editors_contribution(ws, _answers(ws))
    assert contribution is not None
    assert contribution.launches[0].args == ("--dev=all", "--without-demo=all")


def test_odoo_demo_flag_prefers_with_demo(lab):
    """A core declaring both demo flags is treated as `--with-demo`."""
    root = lab.ws("both")
    files = lab.odoo_files()
    files["odoo/tools/config.py"] = (
        'group.add_option("--without-demo", dest="without_demo")\n'
        'group.add_option("--with-demo", dest="with_demo")\n'
    )
    write_files(root / "community", files)
    ws = resolved_workspace(root, _repos(("community", root / "community")))

    contribution = odoo.editors_contribution(ws, _answers(ws))
    assert contribution is not None
    assert contribution.launches[0].args == ("--dev=all",)


def test_odoo_two_cores_one_warning_and_nothing_else(lab):
    """Two cores: one warning, no files, no answers anywhere."""
    root = lab.ws("two")
    write_files(root / "community", lab.odoo_files())
    write_files(root / "enterprise", lab.odoo_files())
    ws = resolved_workspace(
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

    assert odoo.ow_generated_files(ws, _answers(ws)) == []
    assert odoo.python_environment(ws) is None
    assert odoo.editors_contribution(ws, _answers(ws)) is None


def test_odoo_malformed_bound_one_warning_and_nothing_else(lab):
    """An unreadable Python range: one warning, no contribution."""
    root = lab.ws("malformed")
    files = lab.odoo_files()
    del files["odoo/release.py"]
    write_files(root / "community", files)
    ws = resolved_workspace(root, _repos(("community", root / "community")))

    diagnostics = odoo.ow_diagnostics(ws)
    assert len(diagnostics) == 1
    assert diagnostics[0].level == "warning"
    assert "Python version bounds" in diagnostics[0].text

    assert odoo.ow_generated_files(ws, _answers(ws)) == []
    assert odoo.python_environment(ws) is None
    assert odoo.editors_contribution(ws, _answers(ws)) is None


def test_odoo_missing_demo_flag_produces_odoorc(lab):
    """A config.py without a demo flag is treated as False and generates odoorc."""
    root = lab.ws("nodesc")
    files = lab.odoo_files()
    files["odoo/tools/config.py"] = "# no demo flag here\n"
    write_files(root / "community", files)
    ws = resolved_workspace(root, _repos(("community", root / "community")))

    diagnostics = odoo.ow_diagnostics(ws)
    assert diagnostics == []

    result = odoo.ow_generated_files(ws, _answers(ws))
    assert len(files_of(result)) == 4
    assert odoo.editors_contribution(ws, _answers(ws)) is not None


def test_odoo_unparsable_config_warns_and_contributes_nothing(lab):
    """A config.py that fails to parse warns and blocks every contribution."""
    root = lab.ws("badconfig")
    files = lab.odoo_files()
    files["odoo/tools/config.py"] = "def (\n"
    write_files(root / "community", files)
    ws = resolved_workspace(root, _repos(("community", root / "community")))

    diagnostics = odoo.ow_diagnostics(ws)
    assert len(diagnostics) == 1
    assert "community" in diagnostics[0].text
    assert "config.py" in diagnostics[0].text

    assert odoo.ow_generated_files(ws, _answers(ws)) == []
    assert odoo.python_environment(ws) is None
    assert odoo.editors_contribution(ws, _answers(ws)) is None


def test_odoo_password_with_newline_rejected(lab):
    """A password with newline in db_password or admin_passwd is rejected."""
    root = lab.ws("badpass")
    write_files(root / "community", lab.odoo_files())
    ws = resolved_workspace(
        root,
        _repos(("community", root / "community")),
        overrides={"odoo.db_password": "pass\nword"},
    )

    diagnostics = odoo.ow_diagnostics(ws)
    assert len(diagnostics) == 1
    assert "odoo.db_password" in diagnostics[0].text
    assert "invalid" in diagnostics[0].text


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_odoo_unreadable_directory_skipped_silently(lab):
    """A directory the walk cannot read contributes nothing and no warning."""
    root = lab.ws("unreadable")
    write_files(root / "community", lab.odoo_files())
    locked = root / "enterprise" / "locked"
    locked.mkdir(parents=True)
    (locked / "hidden_addon").mkdir()
    (locked / "hidden_addon" / "__manifest__.py").write_text("{'name': 'Hidden'}")
    locked.chmod(0o000)

    try:
        ws = resolved_workspace(
            root,
            _repos(("community", root / "community"), ("enterprise", root / "enterprise")),
        )
        result = odoo.ow_generated_files(ws, _answers(ws))

        assert diagnostics_of(result) == []
        odoorc = next(f for f in files_of(result) if f.path.as_posix() == "odoorc")
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(odoorc.data.decode("utf-8"))
        assert "locked" not in parser["options"]["addons_path"]
    finally:
        locked.chmod(0o755)


def test_odoo_addon_path_with_comma_skipped_with_warning(lab):
    """A path that cannot round-trip through addons_path is skipped, with a warning."""
    root = lab.ws("comma")
    core = root / "community"
    write_files(core, lab.odoo_files())
    write_files(root / "enterprise" / "odd,addons" / "my_addon",
    {"__manifest__.py": "{'name': 'My'}"},)
    ws = resolved_workspace(
        root,
        _repos(("community", core), ("enterprise", root / "enterprise")),
    )

    result = odoo.ow_generated_files(ws, _answers(ws))

    diagnostics = diagnostics_of(result)
    assert len(diagnostics) == 1
    assert diagnostics[0].level == "warning"
    assert "addons_path" in diagnostics[0].text

    odoorc = next(f for f in files_of(result) if f.path.as_posix() == "odoorc")
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

    contribution = odoo.editors_contribution(ws, _answers(ws))
    assert contribution is not None
    assert contribution.launches[0].args == ("--dev=all",)


@pytest.mark.parametrize("port", [0, 70000])
def test_odoo_invalid_port_warns_and_falls_back(lab, port):
    """An out-of-range port warns and the odoorc carries the default."""
    ws = _core_workspace(lab, overrides={"odoo.http_port": port})

    diagnostics = odoo.ow_diagnostics(ws)
    assert [d.level for d in diagnostics] == ["warning"]
    assert "odoo.http_port" in diagnostics[0].text

    result = odoo.ow_generated_files(ws, _answers(ws))
    odoorc = next(f for f in files_of(result) if f.path.as_posix() == "odoorc")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(odoorc.data.decode("utf-8"))
    assert parser["options"]["http_port"] == "8069"


def test_odoo_no_core_is_silent(lab):
    """Without an Odoo core the plugin says and does nothing at all."""
    root = lab.ws("plain")
    root.mkdir(parents=True)
    ws = resolved_workspace(root, (), {"odoo.http_port": 0})

    assert odoo.ow_diagnostics(ws) == []
    assert odoo.ow_generated_files(ws, _answers(ws)) == []
    assert odoo.python_environment(ws) is None
    assert odoo.editors_contribution(ws, _answers(ws)) is None


def test_odoo_mise_environment_answers(lab):
    """The environment answer carries workspace-relative paths, the Python range, requirements and the alias."""
    ws = _core_workspace(lab)

    answer = odoo.python_environment(ws)
    assert answer is not None
    assert answer.env == {}
    assert answer.env_paths == {"ODOO_RC": "odoorc"}
    assert answer.path == ("community",)
    assert answer.python == ((3, 10), (3, 12))
    assert answer.requirements == ("community/requirements.txt", "requirements-dev.txt")
    assert answer.aliases == {"osh": "odoo-bin shell --no-http -c \"$ODOO_RC\""}


def test_odoo_known_repos():
    """The built-in known repos include community and companion repos."""
    repos = odoo.ow_known_repos()
    names = [repo.name for repo in repos]
    assert names == [
        "community",
        "enterprise",
        "design-themes",
        "iap-apps",
        "upgrade",
        "upgrade-util",
        "voip",
    ]


def test_odoo_repo_identity_table_drives_known_urls_and_runbot_names():
    from ow.plugins._odoo_repos import ODOO_REPOS, runbot_repo_name

    urls = {repo.name: repo.clone_url for repo in ODOO_REPOS}
    assert urls["community"] == "git@github.com:odoo/odoo.git"
    assert urls["iap-apps"] == "git@github.com:odoo/iap-apps.git"
    assert {repo.name: repo.url for repo in odoo.ow_known_repos()} == urls
    assert runbot_repo_name("community") == "odoo"
    assert runbot_repo_name("iap-apps") == "iap-apps"
    assert runbot_repo_name("Brboi-Addons") == "brboi-addons"


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


def _odoorc_options(ws: Workspace, answers) -> configparser.SectionProxy:
    python = _answers(ws)
    result = odoo.ow_generated_files(ws, lambda hook: python(hook) + answers(hook))
    odoorc = next(f for f in files_of(result) if f.path.as_posix() == "odoorc")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(odoorc.data.decode("utf-8"))
    return parser["options"]


def _service_answers(hook: str) -> list:
    if hook == "database_service":
        return [DatabaseService(host="127.0.0.1", port=5433, user="svc", password="svc-pw")]
    if hook == "smtp_service":
        return [SmtpService(host="127.0.0.1", port=1025)]
    return []


def test_odoorc_services_fill_values_left_at_default(lab):
    """Settings still at their default take the service values."""
    options = _odoorc_options(_core_workspace(lab), _service_answers)

    assert options["db_host"] == "127.0.0.1"
    assert options["db_port"] == "5433"
    assert options["db_user"] == "svc"
    assert options["db_password"] == "svc-pw"
    assert options["smtp_server"] == "127.0.0.1"
    assert options["smtp_port"] == "1025"


def test_odoorc_explicit_values_win_even_when_equal_to_the_default(lab):
    """A value set in config survives a service, including one equal to the declared default."""
    ws = _core_workspace(
        lab,
        overrides={
            "odoo.db_host": "localhost",
            "odoo.db_port": 5432,
            "odoo.db_user": "odoo",
            "odoo.db_password": "odoo",
            "odoo.smtp_server": "localhost",
            "odoo.smtp_port": 25,
        },
    )

    options = _odoorc_options(ws, _service_answers)

    assert options["db_host"] == "localhost"
    assert options["db_port"] == "5432"
    assert options["db_user"] == "odoo"
    assert options["db_password"] == "odoo"
    assert options["smtp_server"] == "localhost"
    assert options["smtp_port"] == "25"


def test_odoorc_service_only_overrides_unset_keys(lab):
    """With only `odoo.db_port` set, the other connection values still follow the service."""
    options = _odoorc_options(_core_workspace(lab, overrides={"odoo.db_port": 5432}), _service_answers)

    assert options["db_port"] == "5432"
    assert options["db_host"] == "127.0.0.1"
    assert options["db_user"] == "svc"



def _managed_answers(dropped: list[str]):
    def answers(hook: str) -> list:
        if hook == "database_service":
            return [
                DatabaseService(
                    host="localhost", port=5433, user="odoo", password="odoo", drop=dropped.append
                )
            ]
        return []

    return answers


def test_reset_drops_through_the_managed_service_on_its_port(lab):
    """With the service on 5433 (odoorc says 5433), `--reset` drops through it, not localhost:5432."""
    from ow.plugins.odoo import runner

    ws = _core_workspace(lab, name="rev")
    dropped: list[str] = []

    lines = runner.reset_workspace(ws, _managed_answers(dropped))

    assert dropped == ["rev"]
    assert lines == ["rev: dropped database 'rev'"]


def test_reset_with_an_explicit_default_port_leaves_the_managed_service_alone(lab, monkeypatch):
    """`odoo.db_port = 5432` set by the user points elsewhere than the service: drop on that server."""
    from ow.plugins.odoo import runner

    ws = _core_workspace(lab, name="rev", overrides={"odoo.db_port": 5432})
    dropped: list[str] = []
    host_drops: list[dict] = []
    monkeypatch.setattr(
        runner, "_drop_host_database", lambda name, **kw: host_drops.append({"name": name, **kw})
    )

    runner.reset_workspace(ws, _managed_answers(dropped))

    assert dropped == []
    assert host_drops == [
        {"name": "rev", "host": "localhost", "port": 5432, "user": "odoo", "password": "odoo"}
    ]


def test_reset_preview_matches_what_reset_removes(lab):
    """The preview names the odoorc's database and the filestore only when it exists."""
    ws = _core_workspace(lab, name="rev")
    answers = _managed_answers([])

    assert odoo.ow_reset_preview(ws, answers) == ["database odoo@localhost:5433/rev"]

    filestore = ws.root / ".odoo" / "filestore" / "rev"
    filestore.mkdir(parents=True)
    assert odoo.ow_reset_preview(ws, answers) == [
        "database odoo@localhost:5433/rev",
        f"filestore {filestore.resolve()}",
    ]
    odoorc = _odoorc_options(ws, answers)
    assert f"{odoorc['db_user']}@{odoorc['db_host']}:{odoorc['db_port']}/{odoorc['db_name']}" == (
        "odoo@localhost:5433/rev"
    )


def test_generated_files_and_mise_share_the_venv_of_the_merged_python_environment(lab):
    """A second plugin answering `python_environment` moves the venv for odoo's files and mise alike."""
    import types

    from ow.core.plugins import _get_builtins, hookimpl, load_plugins
    from ow.plugins import mise
    from ow.specs.python_env import PythonEnvironment, resolve_workspace_python

    extra = types.ModuleType("extra")

    @hookimpl
    def ow_settings():
        return []

    @hookimpl
    def python_environment(ws):
        return PythonEnvironment(python=((3, 11), (3, 11)), venv_key="extra-key")

    extra.ow_settings = ow_settings
    extra.python_environment = python_environment

    ws = _core_workspace(lab)
    answers = load_plugins(builtins=(*_get_builtins(), ("extra", extra))).answers(ws)

    resolved = resolve_workspace_python(ws, answers)
    assert resolved is not None
    venv = resolved[1]
    assert venv.path.name == "py3.11-extra-key"

    odools = next(f for f in files_of(odoo.ow_generated_files(ws, answers)) if f.path.as_posix() == "odools.toml")
    assert tomllib.loads(odools.data.decode("utf-8"))["config"][0]["python_path"] == str(venv.path / "bin" / "python")

    mise_file = files_of(mise.ow_generated_files(ws, answers))[0]
    fragment = tomllib.loads(mise_file.data.decode("utf-8"))
    assert fragment["env"]["_"]["python"]["venv"]["path"] == str(venv.path)
    assert fragment["tools"]["python"] == venv.python_version == "3.11"
