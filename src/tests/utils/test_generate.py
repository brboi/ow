"""Tests for ow.utils.generate: fixed, ordered workspace-file generation.

Both `ow.utils.odoo` and `ow.utils.options` are already landed (Wave A), so
real `OdooInfo`/`EffectiveOptions`/`OdooSettings` are used directly rather
than local stand-ins.
"""

import json
import re
import shlex
import tomllib
from configparser import ConfigParser
from pathlib import Path, PurePosixPath

import pytest

from ow.utils.generate import GeneratedFile, GenerationContext, generate_files
from ow.utils.odoo import OdooInfo
from ow.utils.options import EffectiveOptions, OdooSettings

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def make_core(**overrides) -> OdooInfo:
    defaults = dict(
        alias="community",
        series="19.0",
        major=19,
        minor=0,
        python_min=(3, 10),
        python_max=(3, 14),
        with_demo=True,
        sandbox_paths={},
    )
    defaults.update(overrides)
    return OdooInfo(**defaults)


def make_odoo_settings(**overrides) -> OdooSettings:
    defaults = dict(
        http_port=8069,
        db_host="localhost",
        db_port=5432,
        db_user="odoo",
        db_password="odoo",
        admin_passwd="Password",
        smtp_server="localhost",
        smtp_port=25,
        debug_args=("--dev=all",),
        debug_test_args=("--test-tags=myws",),
    )
    defaults.update(overrides)
    return OdooSettings(**defaults)


def make_context(**overrides) -> GenerationContext:
    defaults = dict(
        root=Path("/workspaces/myws"),
        repos=("community",),
        core=None,
        options=EffectiveOptions(python=None, odoo=None, warnings=()),
        addon_paths=(),
        services_compose=Path("/xdg/state/ow/services/compose.yml"),
        git_common_dirs=(),
        use_dev_requirements=False,
    )
    defaults.update(overrides)
    return GenerationContext(**defaults)


def make_odoo_context(**overrides) -> GenerationContext:
    """A supported-core context: community repo, resolved python, settings."""
    core = overrides.pop("core", make_core())
    settings = overrides.pop("settings", make_odoo_settings())
    root = overrides.pop("root", Path("/workspaces/myws"))
    python = overrides.pop("python", "3.12")
    addon_paths = overrides.pop(
        "addon_paths",
        (root / core.alias / "addons", root / core.alias / "odoo" / "addons"),
    )
    return make_context(
        root=root,
        core=core,
        options=EffectiveOptions(python=python, odoo=settings, warnings=()),
        addon_paths=addon_paths,
        **overrides,
    )


def by_path(files: tuple[GeneratedFile, ...], path: str) -> GeneratedFile:
    target = PurePosixPath(path)
    for f in files:
        if f.path == target:
            return f
    raise AssertionError(f"{path} not found among generated files")


def parse_ini(text: str) -> ConfigParser:
    parser = ConfigParser(interpolation=None)
    parser.read_string(text)
    return parser


# ---------------------------------------------------------------------------
# Fixed output table: order, paths, modes, Odoo-only gating
# ---------------------------------------------------------------------------


def test_generate_files_returns_nine_outputs_in_fixed_order():
    files = generate_files(make_odoo_context())
    assert [str(f.path) for f in files] == [
        "mise/conf.d/00-ow.toml",
        ".vscode/settings.json",
        ".zed/settings.json",
        "odoorc",
        "odools.toml",
        "pyrightconfig.json",
        "requirements-dev.txt",
        ".vscode/launch.json",
        ".zed/debug.json",
    ]


def test_odoorc_has_mode_0600_others_default_0644():
    files = generate_files(make_odoo_context())
    for f in files:
        if str(f.path) == "odoorc":
            assert f.mode == 0o600
        else:
            assert f.mode == 0o644


def test_generic_workspace_has_no_odoo_only_outputs():
    files = generate_files(make_context())
    odoo_only = {
        "odoorc",
        "odools.toml",
        "pyrightconfig.json",
        "requirements-dev.txt",
        ".vscode/launch.json",
        ".zed/debug.json",
    }
    for f in files:
        if str(f.path) in odoo_only:
            assert f.data is None
        else:
            assert f.data is not None


def test_supported_core_produces_every_odoo_only_output():
    files = generate_files(make_odoo_context())
    for f in files:
        assert f.data is not None


# ---------------------------------------------------------------------------
# Generic has no Odoo/Python keys
# ---------------------------------------------------------------------------


def test_generic_mise_fragment_has_no_odoo_or_python_keys():
    text = by_path(generate_files(make_context()), "mise/conf.d/00-ow.toml").data.decode()
    doc = tomllib.loads(text)
    assert "tools" not in doc
    assert "hooks" not in doc
    assert "shell_alias" not in doc
    assert "tasks" not in doc
    assert doc["env"] == {"OW_WORKSPACE": "{{config_root}}", "_": {"path": ["{{config_root}}"]}}


def test_generic_vscode_settings_has_no_odoo_profile():
    text = by_path(generate_files(make_context()), ".vscode/settings.json").data.decode()
    data = json.loads(text)
    assert "Odoo.selectedProfile" not in data


def test_generic_zed_settings_has_no_lsp_or_odoo_files():
    text = by_path(generate_files(make_context()), ".zed/settings.json").data.decode()
    data = json.loads(text)
    assert "lsp" not in data
    assert "odools.toml" not in data["file_scan_inclusions"]
    assert "pyrightconfig.json" not in data["file_scan_inclusions"]
    assert "mise/conf.d/*.toml" in data["file_scan_inclusions"]


# ---------------------------------------------------------------------------
# Explicit generic Python opt-in
# ---------------------------------------------------------------------------


def test_generic_python_opt_in_has_only_generic_venv_and_ensurepip_hook():
    context = make_context(options=EffectiveOptions(python="3.13", odoo=None, warnings=()))
    text = by_path(generate_files(context), "mise/conf.d/00-ow.toml").data.decode()
    doc = tomllib.loads(text)

    assert doc["tools"] == {"python": "3.13"}
    assert doc["env"]["_"]["python"]["venv"] == {"path": ".venv", "create": True}
    assert doc["hooks"]["postinstall"] == "python -m ensurepip --default-pip"

    assert "ODOO_RC" not in doc["env"]
    assert "COMPOSE_FILE" not in doc["env"]
    assert "shell_alias" not in doc
    assert "tasks" not in doc


# ---------------------------------------------------------------------------
# Both demo eras produce the right debug arguments, faithfully passed through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "with_demo, debug_args, debug_test_args",
    [
        (True, ("--dev=all",), ("--test-tags=myws",)),
        (False, ("--dev=all", "--without-demo=all"), ("--test-tags=myws",)),
    ],
)
def test_debug_arguments_pass_through_for_both_demo_eras(with_demo, debug_args, debug_test_args):
    core = make_core(with_demo=with_demo)
    settings = make_odoo_settings(debug_args=debug_args, debug_test_args=debug_test_args)
    context = make_odoo_context(core=core, settings=settings)

    launch = json.loads(by_path(generate_files(context), ".vscode/launch.json").data.decode())
    debug = json.loads(by_path(generate_files(context), ".zed/debug.json").data.decode())

    assert launch["configurations"][0]["args"] == list(debug_args)
    assert launch["configurations"][1]["args"] == list(debug_test_args)
    assert debug[0]["args"] == list(debug_args)
    assert debug[1]["args"] == list(debug_test_args)
    # Zed uses "label"/"adapter", never vscode's "name"/"type" shape.
    assert debug[0]["label"] == "Run Instance With Debug"
    assert debug[0]["adapter"] == "Debugpy"
    assert debug[0]["console"] == "integratedTerminal"
    assert launch["configurations"][0]["name"] == "Run Instance With Debug"
    assert launch["configurations"][0]["type"] == "debugpy"


# ---------------------------------------------------------------------------
# Workspace path/alias with spaces and quotes
# ---------------------------------------------------------------------------


def test_workspace_root_with_spaces_and_quotes_round_trips():
    root = Path("/workspaces/My Odoo's Workspace")
    common_dir = Path("/home/dev/git-store/co'mmunity worktree")
    core = make_core(sandbox_paths={"bwrap-claude": root / "community" / "bwrap-claude.sh"})
    context = make_odoo_context(root=root, core=core, git_common_dirs=(common_dir,))

    text = by_path(generate_files(context), "mise/conf.d/00-ow.toml").data.decode()
    doc = tomllib.loads(text)

    run_script = doc["tasks"]["bwrap-claude"]["run"]
    assert run_script.startswith("#!/bin/sh\nexec ")
    exec_line = run_script[len("#!/bin/sh\nexec ") : -len(' "$@"\n')]
    argv = shlex.split(exec_line)
    assert argv == [
        str(root / "community" / "bwrap-claude.sh"),
        "--add-dir",
        str(common_dir),
    ]

    settings_text = by_path(generate_files(context), ".vscode/settings.json").data.decode()
    settings = json.loads(settings_text)
    assert settings["Odoo.selectedProfile"] == f"[Odoo Workspace] {root.name}"

    odoorc_text = by_path(generate_files(context), "odoorc").data.decode()
    parser = parse_ini(odoorc_text)
    assert parser["options"]["db_name"] == root.name


# ---------------------------------------------------------------------------
# Numeric-looking Python stays a string
# ---------------------------------------------------------------------------


def test_numeric_looking_python_stays_a_string_everywhere():
    context = make_odoo_context(python="12")

    mise_doc = tomllib.loads(by_path(generate_files(context), "mise/conf.d/00-ow.toml").data.decode())
    assert mise_doc["tools"]["python"] == "12"
    assert isinstance(mise_doc["tools"]["python"], str)

    pyright = json.loads(by_path(generate_files(context), "pyrightconfig.json").data.decode())
    assert pyright["pythonVersion"] == "12"
    assert isinstance(pyright["pythonVersion"], str)


# ---------------------------------------------------------------------------
# No literal `None` anywhere in generated text
# ---------------------------------------------------------------------------


def test_no_literal_none_in_any_generated_output():
    files = generate_files(make_odoo_context())
    for f in files:
        if f.data is None:
            continue
        text = f.data.decode()
        assert "None" not in text, f"{f.path} contains literal None: {text!r}"


def test_no_literal_none_in_generic_outputs():
    files = generate_files(make_context())
    for f in files:
        if f.data is None:
            continue
        text = f.data.decode()
        assert "None" not in text, f"{f.path} contains literal None: {text!r}"


# ---------------------------------------------------------------------------
# dbfilter dot-escape
# ---------------------------------------------------------------------------


def test_dbfilter_escapes_a_dot_in_the_workspace_name():
    root = Path("/workspaces/my.ws")
    context = make_odoo_context(root=root)
    text = by_path(generate_files(context), "odoorc").data.decode()
    parser = parse_ini(text)
    assert parser["options"]["dbfilter"] == f"^{re.escape('my.ws')}$"
    # A literal dot must not survive as an unescaped regex wildcard.
    assert parser["options"]["dbfilter"] == "^my\\.ws$"
    assert re.match(parser["options"]["dbfilter"], "myXws") is None
    assert re.match(parser["options"]["dbfilter"], "my.ws")


# ---------------------------------------------------------------------------
# use_dev_requirements gating
# ---------------------------------------------------------------------------


def test_use_dev_requirements_false_omits_the_dev_operand():
    context = make_odoo_context(use_dev_requirements=False)
    doc = tomllib.loads(by_path(generate_files(context), "mise/conf.d/00-ow.toml").data.decode())
    postinstall = doc["hooks"]["postinstall"]
    assert "requirements-dev.txt" not in postinstall
    assert '"$OW_WORKSPACE"/community/requirements.txt' in postinstall


def test_use_dev_requirements_true_includes_the_dev_operand():
    context = make_odoo_context(use_dev_requirements=True)
    doc = tomllib.loads(by_path(generate_files(context), "mise/conf.d/00-ow.toml").data.decode())
    postinstall = doc["hooks"]["postinstall"]
    assert postinstall == (
        "python -m ensurepip --default-pip && pip install "
        '-r "$OW_WORKSPACE"/community/requirements.txt '
        '-r "$OW_WORKSPACE/requirements-dev.txt"'
    )


# ---------------------------------------------------------------------------
# Sandbox argv/whitelists correct per presence
# ---------------------------------------------------------------------------


def test_no_sandbox_paths_means_no_tasks_table():
    context = make_odoo_context(core=make_core(sandbox_paths={}))
    doc = tomllib.loads(by_path(generate_files(context), "mise/conf.d/00-ow.toml").data.decode())
    assert "tasks" not in doc


def test_bwrap_tasks_get_add_dir_argv_and_odoo_base_env():
    common_dirs = (Path("/git-store/community"), Path("/git-store/enterprise"))
    core = make_core(
        sandbox_paths={
            "bwrap-claude": Path("/ws/community/setup/sandboxing/bwrap/bwrap-claude.sh"),
            "bwrap-opencode": Path("/ws/community/setup/sandboxing/bwrap/bwrap-opencode.sh"),
        }
    )
    context = make_odoo_context(core=core, git_common_dirs=common_dirs)
    doc = tomllib.loads(by_path(generate_files(context), "mise/conf.d/00-ow.toml").data.decode())

    for task_name, script in core.sandbox_paths.items():
        task = doc["tasks"][task_name]
        run_script = task["run"]
        exec_line = run_script[len("#!/bin/sh\nexec ") : -len(' "$@"\n')]
        argv = shlex.split(exec_line)
        assert argv == [
            str(script),
            "--add-dir",
            str(common_dirs[0]),
            "--add-dir",
            str(common_dirs[1]),
        ]
        assert task["env"] == {"ODOO_BASE": "{{config_root}}"}


def test_firejail_task_gets_whitelist_argv_and_no_task_env():
    common_dirs = (Path("/git-store/community"),)
    core = make_core(
        sandbox_paths={"firejail-claude": Path("/ws/community/setup/sandboxing/firejail/claude.profile")}
    )
    root = Path("/workspaces/myws")
    context = make_odoo_context(core=core, root=root, git_common_dirs=common_dirs)
    doc = tomllib.loads(by_path(generate_files(context), "mise/conf.d/00-ow.toml").data.decode())

    task = doc["tasks"]["firejail-claude"]
    run_script = task["run"]
    exec_line = run_script[len("#!/bin/sh\nexec ") : -len(' "$@"\n')]
    argv = shlex.split(exec_line)
    assert argv == [
        "firejail",
        "--profile=/ws/community/setup/sandboxing/firejail/claude.profile",
        f"--whitelist={root}",
        f"--whitelist={common_dirs[0]}",
        "claude",
    ]
    assert "env" not in task


def test_all_four_sandbox_tasks_present_when_all_probed():
    core = make_core(
        sandbox_paths={
            "bwrap-claude": Path("/ws/community/a.sh"),
            "bwrap-opencode": Path("/ws/community/b.sh"),
            "bwrap-pi": Path("/ws/community/c.sh"),
            "firejail-claude": Path("/ws/community/d.profile"),
        }
    )
    context = make_odoo_context(core=core)
    doc = tomllib.loads(by_path(generate_files(context), "mise/conf.d/00-ow.toml").data.decode())
    assert set(doc["tasks"]) == {"bwrap-claude", "bwrap-opencode", "bwrap-pi", "firejail-claude"}


# ---------------------------------------------------------------------------
# Odoorc/odools relative addon paths, comma/CR/LF validation
# ---------------------------------------------------------------------------


def test_odoorc_addons_path_is_relative_to_root_comma_joined():
    root = Path("/workspaces/myws")
    addon_paths = (root / "community" / "addons", root / "community" / "odoo" / "addons")
    context = make_odoo_context(root=root, addon_paths=addon_paths)
    text = by_path(generate_files(context), "odoorc").data.decode()
    parser = parse_ini(text)
    assert parser["options"]["addons_path"] == "community/addons,community/odoo/addons"


def test_odools_toml_addons_paths_are_relative_and_dot_prefixed():
    root = Path("/workspaces/myws")
    addon_paths = (root / "community" / "addons",)
    context = make_odoo_context(root=root, addon_paths=addon_paths)
    text = by_path(generate_files(context), "odools.toml").data.decode()
    doc = tomllib.loads(text)
    assert doc["config"][0]["addons_paths"] == ["./community/addons"]
    assert doc["config"][0]["odoo_path"] == "./community"


def test_addon_path_with_comma_is_rejected_before_serialization():
    root = Path("/workspaces/myws")
    addon_paths = (root / "extra, evil" / "addons",)
    context = make_odoo_context(root=root, addon_paths=addon_paths)
    with pytest.raises(ValueError, match="extra, evil"):
        generate_files(context)


def test_addon_path_with_newline_is_rejected_before_serialization():
    root = Path("/workspaces/myws")
    addon_paths = (root / "extra\nline" / "addons",)
    context = make_odoo_context(root=root, addon_paths=addon_paths)
    with pytest.raises(ValueError):
        generate_files(context)


# ---------------------------------------------------------------------------
# Ownership of odoorc fields (typed connection options, not raw strings)
# ---------------------------------------------------------------------------


def test_odoorc_serializes_typed_connection_options():
    settings = make_odoo_settings(
        http_port=8070,
        db_host="db.internal",
        db_port=5433,
        db_user="myuser",
        db_password="s3cr3t",
        admin_passwd="topsecret",
        smtp_server="smtp.internal",
        smtp_port=2525,
    )
    context = make_odoo_context(settings=settings)
    text = by_path(generate_files(context), "odoorc").data.decode()
    parser = parse_ini(text)
    options = parser["options"]
    assert options["http_port"] == "8070"
    assert options["db_host"] == "db.internal"
    assert options["db_port"] == "5433"
    assert options["db_user"] == "myuser"
    assert options["db_password"] == "s3cr3t"
    assert options["admin_passwd"] == "topsecret"
    assert options["smtp_server"] == "smtp.internal"
    assert options["smtp_port"] == "2525"


def test_odoorc_uses_space_around_delimiters_formatting():
    context = make_odoo_context()
    text = by_path(generate_files(context), "odoorc").data.decode()
    assert "http_port = 8069" in text
    assert "http_port=8069" not in text


# ---------------------------------------------------------------------------
# requirements-dev.txt content preserved verbatim
# ---------------------------------------------------------------------------


def test_requirements_dev_txt_preserves_bundled_content():
    context = make_odoo_context()
    data = by_path(generate_files(context), "requirements-dev.txt").data
    assert data == b"inotify\n"


# ---------------------------------------------------------------------------
# Pyright effective Python string, execution environment alias
# ---------------------------------------------------------------------------


def test_pyrightconfig_uses_effective_python_and_core_alias():
    core = make_core(alias="enterprise")
    context = make_odoo_context(core=core)
    text = by_path(generate_files(context), "pyrightconfig.json").data.decode()
    data = json.loads(text)
    assert data["pythonVersion"] == "3.12"
    assert data["extraPaths"] == ["./enterprise"]


# ---------------------------------------------------------------------------
# json_text / exec_task helpers
# ---------------------------------------------------------------------------


def test_json_text_ends_with_trailing_newline():
    from ow.utils.generate import json_text

    text = json_text({"a": 1})
    assert text.endswith("\n")
    assert json.loads(text) == {"a": 1}


def test_exec_task_forwards_extra_args_exactly_once():
    from ow.utils.generate import exec_task

    script = exec_task(["/bin/sh", "hello world"])
    assert script == '#!/bin/sh\nexec /bin/sh \'hello world\' "$@"\n'
    assert script.count('"$@"') == 1
