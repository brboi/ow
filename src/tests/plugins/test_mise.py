"""Tests for the mise plugin."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import tomllib

from ow.plugins import mise
from ow.specs.mise import MiseEnvironment
from tests.conftest import diagnostics_of, files_of, resolved_workspace

_FRAGMENT = "mise/conf.d/00-ow.toml"


def _answers(*given: MiseEnvironment):
    def answers(hook: str):
        if hook == "mise_environment":
            return list(given)
        return []

    return answers


def _fragment(result: list) -> dict:
    files = files_of(result)
    assert [f.path.as_posix() for f in files] == [_FRAGMENT]
    return tomllib.loads(files[0].data.decode("utf-8"))


def test_mise_fragment_parses_with_a_stub_answer(lab):
    """A stub answer produces a parseable fragment carrying every field."""
    root = lab.ws("mise")
    root.mkdir(parents=True)
    ws = resolved_workspace(root, (), {"mise.python": "3.11"})

    result = mise.ow_generated_files(
        ws,
        _answers(
            MiseEnvironment(
                env={"ODOO_RC": "{{config_root}}/odoorc"},
                path=("{{config_root}}/community",),
                python=((3, 10), (3, 12)),
                postinstall=("pip install -r {{config_root}}/odoorc.txt",),
                aliases={"osh": "odoo-bin shell --no-http -c $ODOO_RC"},
            )
        ),
    )

    assert diagnostics_of(result) == []
    fragment = _fragment(result)
    assert fragment["tools"]["python"] == "3.11"
    assert fragment["env"]["ODOO_RC"] == "{{config_root}}/odoorc"
    assert fragment["env"]["_"]["path"] == ["{{config_root}}/community"]
    assert fragment["env"]["_"]["python"]["venv"] == {"path": ".venv", "create": True}
    assert fragment["hooks"]["postinstall"] == "pip install -r {{config_root}}/odoorc.txt"
    assert fragment["shell_alias"] == {
        "osh": "odoo-bin shell --no-http -c $ODOO_RC"
    }
    assert "alias" not in fragment
    assert "tasks" not in fragment


def test_mise_clamps_explicit_python_into_range(lab):
    """`3.9` with range 3.10–3.12 clamps to `3.10` with an `info` diagnostic."""
    root = lab.ws("clamp")
    root.mkdir(parents=True)
    ws = resolved_workspace(root, (), {"mise.python": "3.9"})

    result = mise.ow_generated_files(ws, _answers(MiseEnvironment(python=((3, 10), (3, 12)))))

    diagnostics = diagnostics_of(result)
    assert len(diagnostics) == 1
    assert diagnostics[0].level == "info"
    assert "3.9" in diagnostics[0].text and "3.10" in diagnostics[0].text
    assert _fragment(result)["tools"]["python"] == "3.10"


def test_mise_unset_python_defaults_to_range_max(lab):
    """An unset `mise.python` takes the range's max."""
    root = lab.ws("max")
    root.mkdir(parents=True)
    ws = resolved_workspace(root)

    result = mise.ow_generated_files(ws, _answers(MiseEnvironment(python=((3, 10), (3, 12)))))

    assert diagnostics_of(result) == []
    assert _fragment(result)["tools"]["python"] == "3.12"


def test_mise_no_answer_and_no_python_writes_nothing(lab):
    """Without an answer and without `mise.python` there is no file, no diagnostic."""
    root = lab.ws("nothing")
    root.mkdir(parents=True)
    ws = resolved_workspace(root)

    assert mise.ow_generated_files(ws, _answers()) == []


def test_mise_python_setting_alone_sets_the_tool_and_venv(lab):
    """`mise.python` alone yields a fragment with the tool and the venv, nothing else."""
    root = lab.ws("pythononly")
    root.mkdir(parents=True)
    ws = resolved_workspace(root, (), {"mise.python": "3.11"})

    result = mise.ow_generated_files(ws, _answers())

    assert diagnostics_of(result) == []
    fragment = _fragment(result)
    assert fragment["tools"] == {"python": "3.11"}
    assert fragment["env"]["_"]["python"]["venv"] == {"path": ".venv", "create": True}
    assert "path" not in fragment["env"]["_"]
    assert "hooks" not in fragment
    assert "shell_alias" not in fragment


def test_mise_without_range_uses_the_value_as_is(lab):
    """Without a range the explicit value is used as is."""
    root = lab.ws("norange")
    root.mkdir(parents=True)
    ws = resolved_workspace(root, (), {"mise.python": "3.13"})

    result = mise.ow_generated_files(ws, _answers())

    assert diagnostics_of(result) == []
    assert _fragment(result)["tools"]["python"] == "3.13"


def _fake_mise(tmp_path: Path, version: str) -> tuple[Path, Path]:
    """A `mise` on PATH that logs its argv and reports `version`."""
    log = tmp_path / "mise.log"
    script = tmp_path / "bin" / "mise"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{log}"\n'
        f'if [ "$1" = "--version" ]; then echo "{version}"; fi\n'
    )
    script.chmod(0o755)
    return script, log


def test_mise_trust_logs_the_fragment_with_recent_mise(tmp_path, lab, capsys):
    """mise ≥ 2026.8.13 is asked to trust the absolute fragment path."""
    _, log = _fake_mise(tmp_path, "2026.8.13 linux-x64 (2026-08-13)")
    root = lab.ws("trust")
    root.mkdir(parents=True)
    ws = resolved_workspace(root)

    mise.ow_refreshed(ws, (PurePosixPath(_FRAGMENT),))

    assert log.read_text().splitlines() == [
        "--version",
        f"trust {root / 'mise' / 'conf.d' / '00-ow.toml'}",
    ]
    assert capsys.readouterr().err == ""


def test_mise_trust_old_mise_warns_and_does_not_trust(tmp_path, lab, capsys):
    """mise < 2026.8.13 is never asked to trust, and one warning is printed."""
    _, log = _fake_mise(tmp_path, "2026.8.12")
    root = lab.ws("old")
    root.mkdir(parents=True)
    ws = resolved_workspace(root)

    mise.ow_refreshed(ws, (PurePosixPath(_FRAGMENT),))

    assert log.read_text().splitlines() == ["--version"]
    captured = capsys.readouterr()
    assert captured.err.count("warning:") == 1
    assert "2026.8.13" in captured.err


def test_mise_trust_skipped_when_the_fragment_is_not_owned(tmp_path, lab):
    """A fragment ow does not own is never trusted."""
    _, log = _fake_mise(tmp_path, "2026.8.13")
    root = lab.ws("unowned")
    root.mkdir(parents=True)
    ws = resolved_workspace(root)

    mise.ow_refreshed(ws, ())

    assert not log.exists()


def test_mise_trust_with_empty_path_warns(lab, capsys, monkeypatch):
    """mise missing from PATH prints the one warning instead of raising."""
    root = lab.ws("nopath")
    root.mkdir(parents=True)
    ws = resolved_workspace(root)

    monkeypatch.setenv("PATH", "")
    mise.ow_refreshed(ws, (PurePosixPath(_FRAGMENT),))

    captured = capsys.readouterr()
    assert captured.err.count("warning:") == 1
    assert "2026.8.13" in captured.err


def test_mise_trust_failure_warns(lab, capsys, monkeypatch):
    """A non-zero `mise trust` warns with its stderr first line."""
    import subprocess

    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        if list(argv)[:2] == ["mise", "trust"]:
            return subprocess.CompletedProcess(
                argv, 1, "", "Error: unwritable state dir\nmore detail"
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(mise, "_mise_version", lambda: (2026, 8, 13))

    root = lab.ws("trustfail")
    root.mkdir(parents=True)
    ws = resolved_workspace(root)

    mise.ow_refreshed(ws, (PurePosixPath(_FRAGMENT),))

    captured = capsys.readouterr()
    assert "mise trust failed: Error: unwritable state dir" in captured.err


def test_mise_warning_includes_disable_hint(lab, capsys, monkeypatch):
    """The missing/old mise warning points at the plugin toggle."""
    root = lab.ws("nohint")
    root.mkdir(parents=True)
    ws = resolved_workspace(root)

    monkeypatch.setenv("PATH", "")
    mise.ow_refreshed(ws, (PurePosixPath(_FRAGMENT),))

    captured = capsys.readouterr()
    assert "disable with `ow config set --global mise.enabled false`" in captured.err
