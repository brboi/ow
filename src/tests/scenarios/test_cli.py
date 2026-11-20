"""`ow` top-level CLI behaviour: help, --version, exit codes, flags, completion."""

from __future__ import annotations

import re

import pytest

from ow import term
from ow.core.paths import global_config
from tests.conftest import workspace

REPO = "community"
BASE = "18.0"

COMMANDS = (
    "init", "status", "fetch", "switch", "pull", "rebase", "reset",
    "refresh", "files", "ls", "cd", "shell-init", "rm", "prune",
    "repo", "config",
)


def test_bare_ow_prints_help_and_exits_zero(lab):
    result = lab.ow()

    assert result.exit_code == 0
    assert "Ready-to-code Odoo workspaces on git worktrees" in result.stdout
    for command in COMMANDS:
        assert re.search(rf"^  {re.escape(command)}\s", result.stdout, re.M), command


def test_root_help_never_truncates_a_summary(lab):
    result = lab.ow("--help")

    assert result.exit_code == 0
    assert "Commands:" in result.stdout
    summaries = result.stdout.partition("Commands:")[2].splitlines()
    assert summaries
    # A truncated summary ends in an ellipsis (`...` from click, `…` from rich).
    # Typer's `[ARGS]...` lives in the usage line, above the listing, so it is not one.
    for line in summaries:
        assert not line.rstrip().endswith(("...", "…")), line


def test_workspace_and_only_metavars(lab):
    assert "-w, --workspace WS" in lab.ow("status", "--help").stdout
    assert "--only REPO" in lab.ow("fetch", "--help").stdout


def test_repo_with_no_arguments_prints_help_and_exits_zero(lab):
    result = lab.ow("repo")

    assert result.exit_code == 0
    assert "Register, list and remove repos" in result.stdout


def test_version_exits_zero(lab):
    result = lab.ow("--version")

    assert result.exit_code == 0
    assert result.stdout.startswith("ow ")
    assert result.stderr == ""


def test_unknown_command_exits_two(lab):
    result = lab.ow("nope")

    assert result.exit_code == 2
    assert "No such command 'nope'" in result.stderr


def test_only_with_an_unknown_repo_names_the_valid_ones(lab):
    root = workspace(lab)

    result = lab.ow("fetch", "--only", "bogus", cwd=root)

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "bogus" in result.stderr
    assert REPO in result.stderr


def test_status_has_no_only_option(lab):
    root = workspace(lab)

    result = lab.ow("status", "--only", REPO, cwd=root)

    assert result.exit_code == 2
    assert "--only" in result.stderr


def test_switch_base_without_create_is_a_usage_error(lab):
    root = workspace(lab)

    result = lab.ow("switch", "some-branch", "--base", "origin/18.0", cwd=root)

    assert result.exit_code == 2
    assert "--base needs -c" in result.stderr


def test_cd_outside_a_workspace_exits_two(lab):
    result = lab.ow("cd", cwd=lab.home)

    assert result.exit_code == 2
    assert "not inside an ow workspace" in result.stderr


def test_config_get_global_reads_the_global_layer_only(lab):
    root = workspace(lab)
    global_config().parent.mkdir(parents=True, exist_ok=True)
    global_config().write_text("[odoo]\nhttp_port = 8070\n")
    (root / ".ow" / "config.toml").write_text(
        f'[repos]\n{REPO} = "origin/{BASE}"\n\n[odoo]\nhttp_port = 9000\n'
    )

    global_read = lab.ow("config", "get", "--global", "odoo.http_port", cwd=root)
    assert global_read.exit_code == 0
    assert global_read.stdout == "8070\n"

    effective = lab.ow("config", "get", "odoo.http_port", cwd=root)
    assert effective.stdout == "9000\n"


def test_repo_names_complete_for_repo_add(lab):
    from typer.testing import CliRunner

    from ow.cli import build_app
    from ow.core.plugins import load_plugins

    workspace(lab)
    runner = CliRunner()
    app = build_app(load_plugins())
    env = {"_OW_COMPLETE": "complete_bash", "COMP_WORDS": "ow repo add ", "COMP_CWORD": "3"}

    result = runner.invoke(app, [], env=env, prog_name="ow")

    assert result.exit_code == 0
    assert REPO in result.stdout.split()


def test_out_prints_text_verbatim(capsys):
    term.out("a:fire:b\tc")

    assert capsys.readouterr().out == "a:fire:b\tc\n"


def test_keyboard_interrupt_exits_130(monkeypatch, capsys):
    import ow.__main__ as entry

    def interrupted() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(entry, "load_plugins", interrupted)
    with pytest.raises(SystemExit) as exit_info:
        entry.main()

    assert exit_info.value.code == 130
    assert "interrupted" in capsys.readouterr().err
