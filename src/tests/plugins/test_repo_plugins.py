"""Tests for in-repo plugins, `OW_API_VERSION` contract, `ow trust`, cross-stack hooks, and multi-stack config layers."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

from ow import term
from ow.commands import trust as trust_cmd
from ow.core import trust
from ow.core.plugins import load_plugins
from ow.errors import PluginError, UsageError
from tests.conftest import add_repo
from tests.lab import git

_VOIP_LIKE_PLUGIN = '''
from pathlib import PurePosixPath
from ow.plugin_api import (
    GeneratedFile,
    RunCommand,
    Setting,
    hookimpl,
    resolve_stack_python,
)

OW_API_VERSION = 1
PLUGIN_NAME = "voipdemo"


@hookimpl
def ow_settings():
    return [
        Setting(
            key="voipdemo.client_stack",
            type=str,
            default=None,
            help="Override client stack name",
        ),
        Setting(
            key="voipdemo.db",
            type=str,
            default="voip-local",
            help="Client DB name",
        ),
    ]


@hookimpl
def ow_generated_files(ws, answers, multi):
    if not any(r.name == "voip" for r in ws.repos):
        return []
    if multi is None:
        return []
    target_name = ws.settings.get("voipdemo.client_stack")
    client = None
    for s in multi.stacks:
        if target_name:
            if s.name == target_name:
                client = s
                break
        elif any(r.name == "enterprise" for r in s.repos):
            client = s
            break
    if client is None:
        return []
    py = resolve_stack_python(client, answers)
    db = ws.settings.get("voipdemo.db") or "voip-local"
    body = f"CLIENT={client.name}\\nPYTHON={py}\\nDB={db}\\n"
    return [GeneratedFile(path=PurePosixPath("voip/.dev/config"), data=body.encode("utf-8"))]


@hookimpl
def ow_runner(ws, multi, args):
    if ws is not None:
        return None
    voip_stack = next((s for s in multi.stacks if any(r.name == "voip" for r in s.repos)), None)
    if voip_stack is None:
        return None
    marker = multi.root / "multi_runner.json"
    code = (
        "import json, sys; "
        f"open({str(marker)!r}, 'w').write(json.dumps({{'stack': {voip_stack.name!r}, 'args': sys.argv[1:]}}))"
    )
    return RunCommand(argv=(sys.executable, "-c", code, *args), cwd=voip_stack.root)
'''


def test_in_repo_plugin_trust_cross_stack_files_config_layers_and_multi_runner(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "enterprise", {"18.0": {"voip/__manifest__.py": "{}"}})
    add_repo(
        lab,
        "voip",
        {"master": {"ow_plugin.py": _VOIP_LIKE_PLUGIN.replace("sys.executable", repr(sys.executable))}},
        base="master",
    )

    ws = lab.home / "porting"
    ws.mkdir(parents=True)
    init_res = lab.ow("init", "client=@18.0", "voipstack=voip", cwd=ws)
    assert init_res.exit_code == 0, init_res.stderr + init_res.stdout

    # 1. Before trusting: warning emitted, plugin does NOT run, `.dev/config` not written
    assert "untrusted plugin" in init_res.stderr
    assert "ow trust" in init_res.stderr
    assert not (ws / "voipstack" / "voip" / ".dev" / "config").exists()

    # Non-Odoo stack `voipstack` has NO phantom Odoo ports pinned in root `.ow/config.toml`
    root_cfg = tomllib.loads((ws / ".ow" / "config.toml").read_text())
    assert "odoo" not in root_cfg["stacks"]["voipstack"]
    assert root_cfg["stacks"]["client"]["odoo"]["http_port"] == 8069

    # 2. `ow trust` trusts the plugin and refreshes immediately -> generates `voip/.dev/config`
    tr = lab.ow("trust", "-y", cwd=ws)
    assert tr.exit_code == 0, tr.stderr + tr.stdout
    assert "trusted " in tr.stdout

    dev_cfg = (ws / "voipstack" / "voip" / ".dev" / "config").read_text()
    assert "CLIENT=client" in dev_cfg
    assert "DB=voip-local" in dev_cfg
    assert "py3.12-18.0" in dev_cfg

    # 3. `ow trust --list` shows the canonical path
    ls_tr = lab.ow("trust", "--list", cwd=ws)
    assert ls_tr.exit_code == 0
    assert "voipstack/voip/ow_plugin.py" in ls_tr.stdout

    # 4. Workspace-wide `ow config set` at multi-stack root flows down to stacks and refreshes
    cfg_set = lab.ow("config", "set", "voipdemo.db", "custom-voip", cwd=ws)
    assert cfg_set.exit_code == 0, cfg_set.stderr + cfg_set.stdout
    root_cfg2 = tomllib.loads((ws / ".ow" / "config.toml").read_text())
    assert root_cfg2["voipdemo"]["db"] == "custom-voip"
    assert "DB=custom-voip" in (ws / "voipstack" / "voip" / ".dev" / "config").read_text()

    # Stack-level override wins over workspace-level setting
    stack_set = lab.ow("config", "set", "voipdemo.db", "stack-only-db", cwd=ws / "voipstack")
    assert stack_set.exit_code == 0, stack_set.stderr + stack_set.stdout
    assert "DB=stack-only-db" in (ws / "voipstack" / "voip" / ".dev" / "config").read_text()

    # 5. `ow status` reports the active external plugin
    st_piped = lab.ow("status", cwd=ws)
    assert st_piped.exit_code == 0, st_piped.stderr
    assert "# plugin: voipdemo (" in st_piped.stdout

    # 6. `ow run` at multi-stack root uses the plugin's whole-root `ow_runner` answer, announces it, and forwards args
    run_res = lab.ow("run", "--", "--flag", cwd=ws)
    assert run_res.exit_code == 0, run_res.stderr + run_res.stdout
    assert "# starting porting (plugin voipdemo:" in run_res.stdout
    recorded = json.loads((ws / "multi_runner.json").read_text())
    assert recorded == {"stack": "voipstack", "args": ["--flag"]}

    # 7. `ow trust --untrust` revokes trust
    untr = lab.ow("trust", "--untrust", cwd=ws)
    assert untr.exit_code == 0
    assert "untrusted " in untr.stdout
    assert lab.ow("trust", "--list").stdout.strip() == ""


def test_in_repo_plugin_pyproject_discovery_and_api_version_contract(lab):
    bad_plugin = (
        "OW_API_VERSION = 99\n"
        "from ow.plugin_api import hookimpl\n"
        "@hookimpl\n"
        "def ow_settings(): return []\n"
    )
    good_plugin = (
        "OW_API_VERSION = 1\n"
        "PLUGIN_NAME = 'custompkg'\n"
        "from pathlib import PurePosixPath\n"
        "from ow.plugin_api import GeneratedFile, hookimpl\n"
        "@hookimpl\n"
        "def ow_generated_files(ws): return [GeneratedFile(path=PurePosixPath('hello.txt'), data=b'hi\\n')]\n"
    )

    add_repo(
        lab,
        "goodrepo",
        {
            "master": {
                "pyproject.toml": '[tool.ow]\nplugin = "tools/my_plugin.py"\n',
                "tools/my_plugin.py": good_plugin,
            }
        },
        base="master",
    )
    add_repo(
        lab,
        "badrepo",
        {"master": {"ow_plugin.py": bad_plugin}},
        base="master",
    )
    add_repo(
        lab,
        "escaperepo",
        {"master": {"pyproject.toml": '[tool.ow]\nplugin = "../outside.py"\n'}},
        base="master",
    )

    ws_good = lab.home / "ws-good"
    ws_good.mkdir(parents=True)
    assert lab.ow("init", "goodrepo", cwd=ws_good).exit_code == 0
    assert lab.ow("trust", "-y", cwd=ws_good).exit_code == 0
    assert (ws_good / "hello.txt").read_text() == "hi\n"

    ws_bad = lab.home / "ws-bad"
    ws_bad.mkdir(parents=True)
    assert lab.ow("init", "badrepo", cwd=ws_bad).exit_code == 0
    tr_bad = lab.ow("trust", "-y", cwd=ws_bad)
    assert tr_bad.exit_code == 0
    assert "incompatible OW_API_VERSION=99" in tr_bad.stderr

    # Path traversal in pyproject.toml is silently ignored
    ws_esc = lab.home / "ws-esc"
    ws_esc.mkdir(parents=True)
    esc_res = lab.ow("init", "escaperepo", cwd=ws_esc)
    assert esc_res.exit_code == 0
    assert "untrusted plugin" not in esc_res.stderr


def _plugin_repo(parent: Path, name: str, source: str) -> Path:
    """A committed git repo `parent/name` whose root holds `ow_plugin.py` = `source`."""
    repo = parent / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "ow_plugin.py").write_text(source)
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "plugin")
    return repo


_PLAIN_PLUGIN = "OW_API_VERSION = 1\nPLUGIN_NAME = {name!r}\n"

_SHADOWING_PLUGIN = '''
from ow.plugin_api import Setting, hookimpl

OW_API_VERSION = 1
PLUGIN_NAME = "sneaky"


@hookimpl
def ow_settings():
    return [
        Setting(key="odoo.http_port", type=int, default=1, help="shadow"),
        Setting(key="sneaky.enabled", type=bool, default=False, help="redeclared"),
        Setting(key="sneaky.ok", type=str, default="x", help="fine"),
    ]
'''


def test_loading_a_trusted_plugin_writes_nothing_into_its_repo(tmp_path):
    repo = _plugin_repo(tmp_path / "src", "pkg", _PLAIN_PLUGIN.format(name="pkg"))
    trust.trust_path(repo / "ow_plugin.py")

    plugins, warnings = load_plugins().with_repo_plugins([("pkg", repo)])

    assert warnings == ()
    assert [p for _name, p in plugins.external_plugins] == [(repo / "ow_plugin.py").resolve()]
    assert not (repo / "__pycache__").exists()
    assert git(repo, "status", "--porcelain") == ""


def test_loading_a_plugin_restores_bytecode_writing(tmp_path):
    repo = _plugin_repo(tmp_path / "src", "pkg", "OW_API_VERSION = 99\n")
    trust.trust_path(repo)
    before = sys.dont_write_bytecode

    load_plugins().with_repo_plugins([("pkg", repo)])

    assert sys.dont_write_bytecode is before


def test_off_prefix_setting_is_dropped_with_a_warning_and_cannot_shadow_a_builtin(tmp_path):
    repo = _plugin_repo(tmp_path / "src", "sneaky", _SHADOWING_PLUGIN)
    trust.trust_path(repo)
    base = load_plugins()

    plugins, warnings = base.with_repo_plugins([("sneaky", repo)])

    assert "sneaky" in plugins.names
    assert any("'odoo.http_port'" in w and "'sneaky.'" in w for w in warnings), warnings
    assert any("'sneaky.enabled'" in w and "already declared" in w for w in warnings), warnings
    by_key = {s.key: s for s in plugins.settings()}
    assert by_key["odoo.http_port"] == {s.key: s for s in base.settings()}["odoo.http_port"]
    assert by_key["sneaky.enabled"].default is True
    assert by_key["sneaky.ok"].default == "x"
    assert [s.key for s in plugins.settings()].count("odoo.http_port") == 1


def test_failing_ow_settings_skips_only_that_in_repo_plugin(tmp_path):
    good = _plugin_repo(tmp_path / "src", "good", _PLAIN_PLUGIN.format(name="good"))
    broken = _plugin_repo(
        tmp_path / "src",
        "broken",
        "OW_API_VERSION = 1\nPLUGIN_NAME = 'broken'\n"
        "from ow.plugin_api import hookimpl\n"
        "@hookimpl\n"
        "def ow_settings():\n"
        "    raise RuntimeError('boom')\n",
    )
    trust.trust_path(tmp_path / "src")

    plugins, warnings = load_plugins().with_repo_plugins([("good", good), ("broken", broken)])

    assert [name for name, _p in plugins.external_plugins] == ["good"]
    assert "good" in plugins.names and "broken" not in plugins.names
    assert any("broken" in w and "boom" in w for w in warnings), warnings
    assert "good.enabled" in {s.key for s in plugins.settings()}


def test_ow_settings_returning_something_that_is_not_a_setting_skips_only_that_in_repo_plugin(tmp_path):
    good = _plugin_repo(tmp_path / "src", "good", _PLAIN_PLUGIN.format(name="good"))
    wrong_item = _plugin_repo(
        tmp_path / "src",
        "wrongitem",
        "OW_API_VERSION = 1\nPLUGIN_NAME = 'wrongitem'\n"
        "from ow.plugin_api import hookimpl\n"
        "@hookimpl\n"
        "def ow_settings():\n"
        "    return ['wrongitem.key']\n",
    )
    wrong_container = _plugin_repo(
        tmp_path / "src",
        "wrongcontainer",
        "OW_API_VERSION = 1\nPLUGIN_NAME = 'wrongcontainer'\n"
        "from ow.plugin_api import hookimpl\n"
        "@hookimpl\n"
        "def ow_settings():\n"
        "    return 3\n",
    )
    trust.trust_path(tmp_path / "src")

    plugins, warnings = load_plugins().with_repo_plugins(
        [("good", good), ("wrongitem", wrong_item), ("wrongcontainer", wrong_container)]
    )

    assert [name for name, _p in plugins.external_plugins] == ["good"]
    assert any("wrongitem" in w and "expected a list of Setting, got str" in w for w in warnings), warnings
    assert any("wrongcontainer" in w and "expected a list of Setting, got int" in w for w in warnings), warnings


def test_a_builtin_plugin_whose_ow_settings_returns_a_non_setting_is_a_plugin_error():
    import types

    from ow.core.plugins import hookimpl

    module = types.ModuleType("bad")

    @hookimpl
    def ow_settings():
        return ["bad.key"]

    module.ow_settings = ow_settings

    with pytest.raises(PluginError, match="plugin bad failed in ow_settings: expected a list of Setting"):
        load_plugins((("bad", module),)).settings()


def test_every_builtin_plugin_declares_only_prefixed_settings():
    plugins = load_plugins()
    settings, warnings = plugins._collect_settings()

    assert warnings == ()
    assert plugins.names == (
        "mise", "odoo", "postgres", "mailsink", "github", "runbot", "vscode", "zed", "local", "sandbox",
    )
    # `python.` is ow's own namespace; every other key belongs to the plugin named by its prefix.
    assert [s.key for s in settings if s.key.startswith("python.")] == ["python.version", "python.shared_venv"]
    for setting in settings:
        if not setting.key.startswith("python."):
            assert setting.key.split(".", 1)[0] in plugins.names, setting.key


def test_the_python_namespace_is_reserved_for_ow(tmp_path):
    import types

    squatter = _plugin_repo(tmp_path / "src", "squatter", _PLAIN_PLUGIN.format(name="python"))
    trust.trust_path(tmp_path / "src")

    plugins, warnings = load_plugins().with_repo_plugins([("squatter", squatter)])

    assert plugins.external_plugins == ()
    assert any("'python' is reserved by ow" in w for w in warnings), warnings
    with pytest.raises(PluginError, match="reserved"):
        load_plugins((("python", types.ModuleType("python")),))


def test_duplicate_in_repo_plugin_name_is_refused(tmp_path):
    first = _plugin_repo(tmp_path / "src", "one", _PLAIN_PLUGIN.format(name="dup"))
    second = _plugin_repo(tmp_path / "src", "two", _PLAIN_PLUGIN.format(name="dup"))
    shadow_builtin = _plugin_repo(tmp_path / "src", "three", _PLAIN_PLUGIN.format(name="odoo"))
    dotted = _plugin_repo(tmp_path / "src", "four", _PLAIN_PLUGIN.format(name="a.b"))
    trust.trust_path(tmp_path / "src")

    plugins, warnings = load_plugins().with_repo_plugins(
        [("one", first), ("two", second), ("three", shadow_builtin), ("four", dotted)]
    )

    assert [name for name, _p in plugins.external_plugins] == ["dup"]
    assert [p for _name, p in plugins.external_plugins] == [(first / "ow_plugin.py").resolve()]
    assert len([w for w in warnings if "invalid or already registered" in w]) == 3


def test_load_plugins_refuses_duplicate_and_malformed_names():
    import types

    with pytest.raises(PluginError, match="already registered"):
        load_plugins((("a", types.ModuleType("a")), ("a", types.ModuleType("b"))))
    with pytest.raises(PluginError, match="plugin name must match"):
        load_plugins((("a.b", types.ModuleType("a")),))


def test_plugin_api_exports_errors_facts_and_answers():
    import ow.plugin_api as api
    from ow.core.facts import RepoFacts
    from ow.core.plugins import Answers
    from ow.errors import PluginError as CorePluginError
    from ow.errors import UsageError as CoreUsageError

    assert api.UsageError is CoreUsageError
    assert api.PluginError is CorePluginError
    assert api.RepoFacts is RepoFacts
    assert api.Answers is Answers
    for name in api.__all__:
        assert hasattr(api, name), name


def test_plugin_api_records_are_keyword_only_and_the_venv_tuple_helper_is_not_public():
    import dataclasses

    import ow.plugin_api as api

    records = [getattr(api, n) for n in api.__all__ if dataclasses.is_dataclass(getattr(api, n))]
    assert len(records) >= 15
    for record in records:
        assert all(f.kw_only for f in dataclasses.fields(record)), record.__name__
    assert "resolve_workspace_python" not in api.__all__
    assert not hasattr(api, "StatusInfo") or "data" not in {f.name for f in dataclasses.fields(api.StatusInfo)}
    assert "data" not in {f.name for f in dataclasses.fields(api.PruneCandidate)}


def test_resolve_stack_python_needs_no_odoo_plugin_import():
    import inspect

    import ow.plugin_api as api

    assert "ow.plugins.odoo" not in inspect.getsource(api)
    assert list(inspect.signature(api.resolve_stack_python).parameters) == ["ws", "answers"]


def test_trust_directory_covers_plugins_beneath_it(tmp_path):
    base = tmp_path / "Odoo"
    plugin = _plugin_repo(base / "ws", "pkg", _PLAIN_PLUGIN.format(name="pkg")) / "ow_plugin.py"

    assert not trust.is_trusted(plugin)
    trust.trust_path(base)
    assert trust.is_trusted(plugin)
    assert trust.trusted_by(plugin) == base.resolve()
    assert not trust.is_trusted(tmp_path / "elsewhere" / "ow_plugin.py")
    assert not trust.is_trusted(tmp_path / "Odoo-sibling" / "ow_plugin.py")

    plugins, warnings = load_plugins().with_repo_plugins([("pkg", plugin.parent)])
    assert warnings == ()
    assert [p for _name, p in plugins.external_plugins] == [plugin.resolve()]


def test_untrust_directory_revokes_what_it_covered(tmp_path):
    base = tmp_path / "Odoo"
    plugin = _plugin_repo(base, "pkg", _PLAIN_PLUGIN.format(name="pkg")) / "ow_plugin.py"
    trust.trust_path(base)

    resolved, removed = trust.untrust_path(base)

    assert (resolved, removed) == (base.resolve(), True)
    assert not trust.is_trusted(plugin)
    assert trust.untrust_path(base) == (base.resolve(), False)


def test_untrusting_a_file_under_a_trusted_directory_keeps_the_directory_trust(tmp_path, capsys):
    base = tmp_path / "Odoo"
    plugin = _plugin_repo(base, "pkg", _PLAIN_PLUGIN.format(name="pkg")) / "ow_plugin.py"
    trust.trust_path(base)

    trust_cmd.run(None, path=str(plugin), untrust=True)  # type: ignore[arg-type]

    assert trust.is_trusted(plugin)
    captured = capsys.readouterr()
    assert "was not trusted" in captured.out
    assert "still trusted through" in captured.err


def test_existing_file_entries_stay_trusted(tmp_path):
    from ow.core.paths import trusted_plugins_file

    plugin = _plugin_repo(tmp_path, "pkg", _PLAIN_PLUGIN.format(name="pkg")) / "ow_plugin.py"
    registry = trusted_plugins_file()
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(f'trusted = ["{plugin.resolve()}"]\n')

    assert trust.is_trusted(plugin)
    assert trust.list_trusted() == (plugin.resolve(),)


def test_trust_path_requires_an_existing_file_or_directory(tmp_path):
    with pytest.raises(UsageError, match="path not found"):
        trust.trust_path(tmp_path / "missing")
    with pytest.raises(UsageError, match="path not found"):
        trust_cmd.run(None, path=str(tmp_path / "missing"))  # type: ignore[arg-type]


def test_trust_list_marks_directories(tmp_path, capsys):
    base = tmp_path / "Odoo"
    plugin = _plugin_repo(base, "pkg", _PLAIN_PLUGIN.format(name="pkg")) / "ow_plugin.py"
    trust.trust_path(base)
    trust.trust_path(plugin)

    assert trust_cmd.run(None, list_all=True) == 0  # type: ignore[arg-type]

    lines = capsys.readouterr().out.splitlines()
    assert any(line.endswith("Odoo/") for line in lines)
    assert any(line.endswith("pkg/ow_plugin.py") for line in lines)


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("\n", "files"), ("1\n", "files"), ("2\n", "root"), ("3\n", "parent")],
)
def test_trust_prompt_offers_files_root_and_parent(tmp_path, monkeypatch, capsys, answer, expected):
    import io

    root = tmp_path / "Odoo" / "ws"
    plugin = _plugin_repo(root, "pkg", _PLAIN_PLUGIN.format(name="pkg"))
    found = trust.discover_repo_plugins([("pkg", plugin)])
    monkeypatch.setattr("sys.stdin", io.StringIO(answer))

    chosen = trust_cmd._choose_locations(root, found)

    assert chosen == {
        "files": (plugin.resolve() / "ow_plugin.py",),
        "root": (root,),
        "parent": (root.parent,),
    }[expected]
    out = capsys.readouterr().out
    assert "1)" in out and "2)" in out and "3)" in out


def test_trust_prompt_rejects_garbage_and_eof(tmp_path, monkeypatch):
    import io

    for text in ("9\n", "x\n", ""):
        monkeypatch.setattr("sys.stdin", io.StringIO(text))
        with pytest.raises(UsageError):
            trust_cmd._choose_locations(tmp_path, ())


def test_trusting_the_workspace_root_from_a_terminal_enables_its_plugins(lab, monkeypatch):
    add_repo(
        lab,
        "goodrepo",
        {"master": {"ow_plugin.py": _PLAIN_PLUGIN.format(name="goodrepo")}},
        base="master",
    )
    ws = lab.home / "Odoo" / "ws"
    ws.mkdir(parents=True)
    init_res = lab.ow("init", "goodrepo", cwd=ws)
    assert "untrusted plugin" in init_res.stderr

    # 3) = the workspace's parent directory, so a sibling workspace is covered too
    monkeypatch.setattr(term, "interactive", lambda: True)
    tr = lab.ow("trust", cwd=ws, input="3\n")
    assert tr.exit_code == 0, tr.stderr + tr.stdout
    assert f"trusted {lab.home / 'Odoo'}" in tr.stdout.replace("~", str(lab.home))
    assert trust.list_trusted() == ((lab.home / "Odoo").resolve(),)

    other = lab.home / "Odoo" / "other"
    other.mkdir()
    again = lab.ow("init", "goodrepo", cwd=other)
    assert "untrusted plugin" not in again.stderr
