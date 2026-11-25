"""`ow exec` scenarios: virtualenv + plugin env execution, multi-stack targeting, shell & bootstrap."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ow.core.paths import venvs_dir
from tests.conftest import add_repo, init_workspace
from tests.lab import make_executable, read_jsonl


def _make_recording_script(path: Path, log_path: Path, *, exit_code: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log_path)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps({\n"
        "        'exe': __file__,\n"
        "        'argv': sys.argv[1:],\n"
        "        'cwd': os.getcwd(),\n"
        "        'virtual_env': os.environ.get('VIRTUAL_ENV'),\n"
        "        'virtual_env_prompt': os.environ.get('VIRTUAL_ENV_PROMPT'),\n"
        "        'ow_workspace': os.environ.get('OW_WORKSPACE'),\n"
        "        'odoo_rc': os.environ.get('ODOO_RC'),\n"
        "        'pythonhome': os.environ.get('PYTHONHOME'),\n"
        "        'path': os.environ.get('PATH', '').split(os.pathsep),\n"
        "    }) + '\\n')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    make_executable(path)
    return path


def _install_fake_venv(venv_dir: Path, log_path: Path, *, exit_code: int = 0) -> None:
    _make_recording_script(venv_dir / "bin" / "python", log_path, exit_code=exit_code)
    _make_recording_script(venv_dir / "bin" / "pip", log_path, exit_code=exit_code)


def test_exec_runs_command_in_venv_with_plugin_env_cwd_and_exit_code(lab, monkeypatch):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="my-ws")
    venv_dir = venvs_dir() / "py3.12-18.0"
    log_path = lab.root / "exec.jsonl"
    _install_fake_venv(venv_dir, log_path)

    monkeypatch.setenv("PYTHONHOME", "/bogus/pythonhome")

    # 1. From inside a subdirectory of the workspace: preserves cwd and resolves venv pip
    subdir = ws / "community"
    r1 = lab.ow("exec", "--", "pip", "install", "phonenumbers", cwd=subdir)
    assert r1.exit_code == 0, r1.stderr + r1.stdout
    assert r1.stdout == ""
    assert r1.stderr == ""

    records = read_jsonl(log_path)
    assert len(records) == 1
    rec = records[0]
    assert Path(rec["exe"]).resolve() == (venv_dir / "bin" / "pip").resolve()
    assert rec["argv"] == ["install", "phonenumbers"]
    assert Path(rec["cwd"]).resolve() == subdir.resolve()
    assert rec["virtual_env"] == str(venv_dir)
    assert rec["virtual_env_prompt"] == "my-ws"
    assert rec["ow_workspace"] == "my-ws"
    assert rec["odoo_rc"] == str(ws / "odoorc")
    assert rec["pythonhome"] is None
    assert rec["path"][:2] == [str(venv_dir / "bin"), str(ws / "community")]

    # 2. From outside the workspace with positional WS: switches cwd to ws.root
    r2 = lab.ow("exec", "my-ws", "--", "python", "-c", "pass", cwd=lab.home)
    assert r2.exit_code == 0, r2.stderr
    rec2 = read_jsonl(log_path)[1]
    assert Path(rec2["cwd"]).resolve() == ws.resolve()
    assert rec2["argv"] == ["-c", "pass"]

    # 3. Propagates non-zero child exit code
    _make_recording_script(venv_dir / "bin" / "failtool", log_path, exit_code=7)
    r3 = lab.ow("exec", "-w", "my-ws", "--", "failtool", "--flag", cwd=lab.home)
    assert r3.exit_code == 7


def test_exec_without_command_opens_interactive_shell_and_announces_venv_on_stderr(lab, monkeypatch):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="shell-ws")
    venv_dir = venvs_dir() / "py3.12-18.0"
    log_path = lab.root / "shell.jsonl"
    _install_fake_venv(venv_dir, log_path)

    fake_shell = _make_recording_script(lab.root / "bin" / "fake-shell", log_path)
    monkeypatch.setenv("SHELL", str(fake_shell))

    res = lab.ow("exec", cwd=ws)
    assert res.exit_code == 0, res.stderr
    assert res.stdout == ""
    assert f"# shell-ws: venv {venv_dir} (shared)" in res.stderr

    records = read_jsonl(log_path)
    assert len(records) == 1
    assert Path(records[0]["exe"]).resolve() == fake_shell.resolve()
    assert records[0]["virtual_env"] == str(venv_dir)
    assert records[0]["odoo_rc"] == str(ws / "odoorc")

    # Local venv mode announces (local)
    assert lab.ow("config", "set", "python.shared_venv", "false", cwd=ws).exit_code == 0
    local_venv = ws / ".venv"
    _install_fake_venv(local_venv, log_path)
    res_local = lab.ow("exec", cwd=ws)
    assert res_local.exit_code == 0, res_local.stderr
    assert f"# shell-ws: venv {local_venv} (local)" in res_local.stderr


def test_exec_multi_stack_auto_selects_single_python_stack_and_disambiguates_multiple(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "voip", {"master": {"README.md": "voip\n"}}, base="master")
    venv_dir = venvs_dir() / "py3.12-18.0"
    log_path = lab.root / "multi.jsonl"
    _install_fake_venv(venv_dir, log_path)

    # Multi-stack with 1 Python stack (odoo) + 1 non-Python stack (voip): auto-selects odoo
    single_py = lab.home / "one-py"
    single_py.mkdir(parents=True)
    assert lab.ow("init", "odoo=community@18.0", "voip=voip", cwd=single_py).exit_code == 0

    r_auto = lab.ow("exec", "--", "pip", "list", cwd=single_py)
    assert r_auto.exit_code == 0, r_auto.stderr
    rec = read_jsonl(log_path)[0]
    assert rec["ow_workspace"] == "odoo"
    assert rec["odoo_rc"] == str(single_py / "odoo" / "odoorc")
    assert Path(rec["cwd"]).resolve() == (single_py / "odoo").resolve()

    # Multi-stack with 2 Python stacks (odoo, iap): refuses bare root, accepts explicit stack
    two_py = lab.home / "two-py"
    two_py.mkdir(parents=True)
    assert lab.ow("init", "odoo=community@18.0", "iap=community@18.0", "voip=voip", cwd=two_py).exit_code == 0

    r_ambig = lab.ow("exec", "--", "pip", "list", cwd=two_py)
    assert r_ambig.exit_code == 2
    assert "multiple Python stacks" in r_ambig.stderr
    assert "odoo" in r_ambig.stderr and "iap" in r_ambig.stderr

    # Positional stack target from root
    r_iap = lab.ow("exec", "iap", "--", "pip", "install", "phonenumbers", cwd=two_py)
    assert r_iap.exit_code == 0, r_iap.stderr
    rec_iap = read_jsonl(log_path)[-1]
    assert rec_iap["ow_workspace"] == "iap"
    assert rec_iap["odoo_rc"] == str(two_py / "iap" / "odoorc")

    # Qualified <ws>/<stack> from outside
    r_qual = lab.ow("exec", "two-py/odoo", "--", "pip", "list", cwd=lab.home)
    assert r_qual.exit_code == 0, r_qual.stderr
    assert read_jsonl(log_path)[-1]["ow_workspace"] == "odoo"


def test_exec_refuses_non_python_stack_missing_venv_and_syntax_errors(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(lab, "voip", {"master": {"README.md": "voip\n"}}, base="master")

    multi = lab.home / "guard-ws"
    multi.mkdir(parents=True)
    assert lab.ow("init", "odoo=community@18.0", "voip=voip", cwd=multi).exit_code == 0

    # Non-Python stack executes commands directly in the stack dir with OW_WORKSPACE set
    log_path = lab.root / "nonpy.jsonl"
    tool = _make_recording_script(lab.root / "bin" / "mytool", log_path)
    r_voip = lab.ow("exec", "voip", "--", str(tool), "status", cwd=multi)
    assert r_voip.exit_code == 0, r_voip.stderr
    rec = read_jsonl(log_path)[0]
    assert rec["ow_workspace"] == "voip"
    assert Path(rec["cwd"]).resolve() == (multi / "voip").resolve()
    assert rec["argv"] == ["status"]
    assert rec["virtual_env"] is None

    # Missing virtualenv (mise not installed on hermetic PATH, no venv created) -> refused
    r_missing = lab.ow("exec", "odoo", "--", "pip", "install", "phonenumbers", cwd=multi)
    assert r_missing.exit_code == 2
    assert "virtualenv not found" in r_missing.stderr

    # Both positional WS and -w WS -> refused
    r_both = lab.ow("exec", "odoo", "-w", "odoo", "--", "pip", "list", cwd=multi)
    assert r_both.exit_code == 2
    assert "not both" in r_both.stderr

    # Forgot `--` with multiple args -> helpful hint
    r_forgot_multi = lab.ow("exec", "pip", "install", "phonenumbers", cwd=multi / "odoo")
    assert r_forgot_multi.exit_code == 2
    assert "ow exec -- pip install phonenumbers" in r_forgot_multi.stderr

    # Forgot `--` with single unknown token -> helpful hint
    r_forgot_one = lab.ow("exec", "pip", cwd=multi / "odoo")
    assert r_forgot_one.exit_code == 2
    assert "ow exec -- pip" in r_forgot_one.stderr


def test_exec_bootstraps_venv_via_mise_and_stamps_requirements_on_stderr(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="boot-ws")
    venv_dir = venvs_dir() / "py3.12-18.0"

    pip_log = lab.root / "pip.jsonl"
    exec_log = lab.root / "exec.jsonl"

    # Install a fake `mise` binary on the hermetic PATH that materializes a venv with pyvenv.cfg
    fake_venv_python = (
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['-m', 'pip']:\n"
        f"    with open({str(pip_log)!r}, 'a', encoding='utf-8') as f:\n"
        "        f.write(json.dumps(args) + '\\n')\n"
        "    print('Successfully installed requirements')\n"
        "    sys.exit(0)\n"
        f"with open({str(exec_log)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(args) + '\\n')\n"
    )
    bin_dir = Path(os.environ["PATH"].split(os.pathsep)[0])
    fake_mise = bin_dir / "mise"
    fake_mise.write_text(
        f"#!{sys.executable}\n"
        "import stat, sys\n"
        "from pathlib import Path\n"
        f"venv = Path({str(venv_dir)!r})\n"
        f"py_src = {fake_venv_python!r}\n"
        "if sys.argv[1:2] == ['install']:\n"
        "    print('mise installing python 3.12')\n"
        "    sys.exit(0)\n"
        "if sys.argv[1:2] == ['exec']:\n"
        "    (venv / 'bin').mkdir(parents=True, exist_ok=True)\n"
        "    (venv / 'pyvenv.cfg').write_text('home = /usr/bin\\n', encoding='utf-8')\n"
        "    py = venv / 'bin' / 'python'\n"
        "    py.write_text(py_src, encoding='utf-8')\n"
        "    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)\n"
        "    sys.exit(0)\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    make_executable(fake_mise)

    r1 = lab.ow("exec", "--", "python", "-c", "import phonenumbers", cwd=ws)
    assert r1.exit_code == 0, r1.stderr + r1.stdout
    # Bootstrap chatter stays off stdout so pipelines remain clean
    assert r1.stdout == ""
    assert "preparing Python virtualenv" in r1.stderr
    assert "installing Python requirements" in r1.stderr
    assert (venv_dir / ".ow-requirements.sha256").is_file()

    pip_calls_1 = [json.loads(line) for line in pip_log.read_text().splitlines()]
    assert any(c[:3] == ["-m", "pip", "install"] for c in pip_calls_1)
    assert [json.loads(line) for line in exec_log.read_text().splitlines()] == [
        ["-c", "import phonenumbers"]
    ]

    # Second call reuses the stamped venv without re-running pip install
    r2 = lab.ow("exec", "--", "python", "-c", "print(2)", cwd=ws)
    assert r2.exit_code == 0, r2.stderr
    assert r2.stderr == ""
    pip_calls_2 = [json.loads(line) for line in pip_log.read_text().splitlines()]
    assert len(pip_calls_2) == len(pip_calls_1)


def test_exec_works_with_mise_python_without_odoo_and_skips_services(lab, monkeypatch):
    from ow.plugins import _containers as containers

    def fail_if_called():
        raise AssertionError("ow exec must not touch container services")

    monkeypatch.setattr(containers, "detect_engine", fail_if_called)

    add_repo(lab, "voip", {"master": {"README.md": "voip\n"}}, base="master")
    ws = init_workspace(lab, "voip", name="py-only", base="master")
    assert lab.ow("config", "set", "python.version", "3.12", cwd=ws).exit_code == 0

    local_venv = ws / ".venv"
    log_path = lab.root / "pyonly.jsonl"
    _install_fake_venv(local_venv, log_path)

    res = lab.ow("exec", "--", "pip", "install", "httpx", cwd=ws)
    assert res.exit_code == 0, res.stderr
    rec = read_jsonl(log_path)[0]
    assert rec["virtual_env"] == str(local_venv)
    assert rec["odoo_rc"] is None
    assert rec["argv"] == ["install", "httpx"]


def _install_recording_mise(log_path: Path) -> None:
    """A fake `mise` on the hermetic PATH that records its argv and runs what follows `exec --`."""
    bin_dir = Path(os.environ["PATH"].split(os.pathsep)[0])
    fake_mise = bin_dir / "mise"
    fake_mise.write_text(
        f"#!{sys.executable}\n"
        "import json, subprocess, sys\n"
        f"with open({str(log_path)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['exec', '--']:\n"
        "    sys.exit(subprocess.call(args[2:]))\n",
        encoding="utf-8",
    )
    make_executable(fake_mise)


def test_exec_without_python_environment_goes_through_the_plugin_that_wraps_commands(lab):
    add_repo(lab, "voip", {"master": {"README.md": "voip\n"}}, base="master")
    ws = init_workspace(lab, "voip", name="plain", base="master")
    mise_log = lab.root / "mise.jsonl"
    marker = lab.root / "ran"
    _install_recording_mise(mise_log)

    res = lab.ow("exec", "--", "sh", "-c", f"echo wrapped > {marker}", cwd=ws)

    assert res.exit_code == 0, res.stderr
    assert read_jsonl(mise_log) == [["exec", "--", "sh", "-c", f"echo wrapped > {marker}"]]
    assert marker.read_text() == "wrapped\n"

    # With the plugin disabled nothing wraps the command: it runs as it is.
    marker.unlink()
    assert lab.ow("config", "set", "mise.enabled", "false", cwd=ws).exit_code == 0
    res = lab.ow("exec", "--", "sh", "-c", f"echo bare > {marker}", cwd=ws)

    assert res.exit_code == 0, res.stderr
    assert len(read_jsonl(mise_log)) == 1
    assert marker.read_text() == "bare\n"


def test_exec_with_mise_disabled_uses_the_workspace_venv_without_any_other_setting(lab, monkeypatch):
    """Nothing else builds the shared venv, so with mise off the venv is `<workspace>/.venv`."""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="no-mise")
    assert lab.ow("config", "set", "mise.enabled", "false", cwd=ws).exit_code == 0
    log_path = lab.root / "nomise.jsonl"
    _install_fake_venv(ws / ".venv", log_path)
    fake_shell = _make_recording_script(lab.root / "bin" / "fake-shell", log_path)
    monkeypatch.setenv("SHELL", str(fake_shell))

    res = lab.ow("exec", cwd=ws)

    assert res.exit_code == 0, res.stderr
    assert f"# no-mise: venv {ws / '.venv'} (local)" in res.stderr
    assert read_jsonl(log_path)[0]["virtual_env"] == str(ws / ".venv")
