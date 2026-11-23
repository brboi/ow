"""Integration tests for `ow run -d/--detach`, `ow stop`, `ow logs`, `ow status`, and `ow rm` guards."""

from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path

from ow.core.paths import venvs_dir
from tests.conftest import add_repo, init_workspace


def _install_fake_server_python(venv_dir: Path, *, marker: str = "SERVER_READY") -> None:
    """Create a fake venv Python that logs `marker` and sleeps until SIGTERM."""
    py = venv_dir / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text(
        f"#!{sys.executable}\n"
        "import signal, sys, time\n"
        "stop = False\n"
        "def _handle(_sig, _frame):\n"
        "    global stop\n"
        "    stop = True\n"
        "signal.signal(signal.SIGTERM, _handle)\n"
        f"print({marker!r}, ' '.join(sys.argv[1:]), flush=True)\n"
        "while not stop:\n"
        "    time.sleep(0.05)\n"
        "print('SERVER_STOPPED', flush=True)\n",
        encoding="utf-8",
    )
    py.chmod(py.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_run_detach_logs_status_refuse_duplicate_refuse_rm_and_stop(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="bg-ws")
    _install_fake_server_python(venvs_dir() / "py3.12-18.0", marker="HELLO_BG")

    started = lab.ow("run", "-d", cwd=ws)
    assert started.exit_code == 0, started.stderr + started.stdout
    assert "# started bg-ws (pid " in started.stdout
    assert (ws / ".ow" / "run" / "bg-ws.json").is_file()
    assert (ws / ".ow" / "logs" / "bg-ws.log").is_file()

    try:
        # Wait briefly for the fake server to flush its first line.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if "HELLO_BG" in (ws / ".ow" / "logs" / "bg-ws.log").read_text(encoding="utf-8"):
                break
            time.sleep(0.05)

        logs = lab.ow("logs", cwd=ws)
        assert logs.exit_code == 0, logs.stderr + logs.stdout
        assert "HELLO_BG" in logs.stdout

        st_plain = lab.ow("status", cwd=ws)
        assert st_plain.exit_code == 0
        assert "# running: pid " in st_plain.stdout

        st_json = lab.ow("status", "--json", cwd=ws)
        assert st_json.exit_code == 0
        payload = json.loads(st_json.stdout)
        assert "running" in payload["workspace"]
        assert isinstance(payload["workspace"]["running"]["pid"], int)

        # Duplicate `ow run` and `ow run -d` must refuse while running.
        dup_fg = lab.ow("run", cwd=ws)
        assert dup_fg.exit_code == 2
        assert "already running" in dup_fg.stderr
        assert "ow stop" in dup_fg.stderr

        dup_bg = lab.ow("run", "-d", cwd=ws)
        assert dup_bg.exit_code == 2
        assert "already running" in dup_bg.stderr

        # `ow rm` must refuse while detached process is alive.
        rm_refused = lab.ow("rm", "bg-ws", "-y", cwd=lab.home)
        assert rm_refused.exit_code == 1
        assert "running (pid " in rm_refused.stdout
        assert "ow stop" in rm_refused.stdout
        assert ws.exists()
    finally:
        stopped = lab.ow("stop", cwd=ws)

    assert stopped.exit_code == 0, stopped.stderr + stopped.stdout
    assert "# stopped bg-ws (pid " in stopped.stdout
    assert not (ws / ".ow" / "run" / "bg-ws.json").exists()

    # Stopping again is idempotent.
    stopped_again = lab.ow("stop", cwd=ws)
    assert stopped_again.exit_code == 0
    assert "# bg-ws: not running" in stopped_again.stdout

    # Now `ow rm` succeeds cleanly.
    rm_ok = lab.ow("rm", "bg-ws", "-y", cwd=lab.home)
    assert rm_ok.exit_code == 0, rm_ok.stderr + rm_ok.stdout
    assert not ws.exists()


def test_run_detach_rejects_tmp(lab):
    res = lab.ow("run", "-d", "--tmp", "community@18.0", cwd=lab.home)
    assert res.exit_code == 2
    assert "cannot combine -d/--detach with --tmp" in res.stderr


def test_multi_stack_detach_logs_stop_services_and_stop_all(lab, monkeypatch):
    from ow.plugins import _containers as containers
    from tests.plugins.test_services import _make_fake_engine

    calls, running_containers = _make_fake_engine(
        monkeypatch,
        running={
            "ow-postgres": {5432: 5432},
            "ow-pgweb": {8081: 8081},
            "ow-mailsink": {1025: 1025, 8025: 8025},
        },
    )
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": True)

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    multi = lab.home / "multi-bg"
    multi.mkdir(parents=True)
    assert lab.ow("init", "odoo=community@18.0", "iap=community@18.0", cwd=multi).exit_code == 0
    _install_fake_server_python(venvs_dir() / "py3.12-18.0", marker="STACK_UP")

    started = lab.ow("run", "-d", cwd=multi)
    assert started.exit_code == 0, started.stderr + started.stdout
    assert "# started odoo (pid " in started.stdout
    assert "# started iap (pid " in started.stdout
    # Invariant: single `.ow/` at root, never inside stacks.
    assert (multi / ".ow" / "run" / "odoo.json").is_file()
    assert (multi / ".ow" / "run" / "iap.json").is_file()
    assert not (multi / "odoo" / ".ow").exists()
    assert not (multi / "iap" / ".ow").exists()

    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if (
                "STACK_UP" in (multi / ".ow" / "logs" / "odoo.log").read_text(encoding="utf-8")
                and "STACK_UP" in (multi / ".ow" / "logs" / "iap.log").read_text(encoding="utf-8")
            ):
                break
            time.sleep(0.05)

        logs = lab.ow("logs", cwd=multi)
        assert logs.exit_code == 0
        assert "[odoo] STACK_UP" in logs.stdout
        assert "[iap] STACK_UP" in logs.stdout

        # `ow stop --services` stops global containers without touching running stacks.
        stop_svc = lab.ow("stop", "--services", cwd=lab.home)
        assert stop_svc.exit_code == 0
        assert "# postgres: stopped ow-postgres" in stop_svc.stdout
        assert "# postgres: stopped ow-pgweb" in stop_svc.stdout
        assert "# mailsink: stopped ow-mailsink" in stop_svc.stdout
        assert "ow-postgres" not in running_containers
        assert (multi / ".ow" / "run" / "odoo.json").is_file()

        # Re-seed running container so `--all` stops both workspaces and services.
        running_containers["ow-postgres"] = {5432: 5432}
    finally:
        stop_all = lab.ow("stop", "--all", cwd=lab.home)

    assert stop_all.exit_code == 0, stop_all.stderr + stop_all.stdout
    assert "# stopped multi-bg/odoo" in stop_all.stdout
    assert "# stopped multi-bg/iap" in stop_all.stdout
    assert "# postgres: stopped ow-postgres" in stop_all.stdout
    assert not (multi / ".ow" / "run" / "odoo.json").exists()
    assert not (multi / ".ow" / "run" / "iap.json").exists()
    assert any(c[:2] == ["docker", "stop"] for c in calls)


def test_external_plugin_self_detaching_stop_logs_and_running_hooks(lab, capfd):
    plugin_code = """
import sys
from pathlib import Path
from ow.plugin_api import RunCommand, StatusInfo, hookimpl

OW_API_VERSION = 1
PLUGIN_NAME = "demo_stack"

def _state_file(root: Path) -> Path:
    return root / ".demo_running"

@hookimpl
def ow_multi_runner(multi, args, detach=False):
    sf = _state_file(multi.root)
    if args:
        return RunCommand(argv=(sys.executable, "-c", f"print('PASS', {list(args)!r})"), cwd=multi.root)
    if detach:
        code = f"from pathlib import Path; Path({str(sf)!r}).write_text('up'); print('SELF_DETACHED_UP')"
        return RunCommand(argv=(sys.executable, "-c", code), cwd=multi.root, self_detaching=True)
    return RunCommand(argv=(sys.executable, "-c", "print('FG_RUN')"), cwd=multi.root)

@hookimpl
def ow_running(ws):
    root = ws.root.parent if ws.is_stack else ws.root
    return ["demo-svc"] if _state_file(root).is_file() else None

@hookimpl
def ow_multi_stop(multi):
    sf = _state_file(multi.root)
    code = f"from pathlib import Path; Path({str(sf)!r}).unlink(missing_ok=True); print('PLUGIN_STOPPED')"
    return RunCommand(argv=(sys.executable, "-c", code), cwd=multi.root)

@hookimpl
def ow_multi_logs(multi, follow=False, lines=50):
    return RunCommand(
        argv=(sys.executable, "-c", f"print('PLUGIN_LOGS', {follow!r}, {lines!r})"),
        cwd=multi.root,
    )

@hookimpl
def ow_status(ws, target):
    if target is not None or ws is None:
        return []
    root = ws.root.parent if ws.is_stack else ws.root
    if _state_file(root).is_file():
        return [StatusInfo(plugin=PLUGIN_NAME, scope="workspace", target=ws.name, summary="running (demo-svc)")]
    return []
"""
    add_repo(
        lab,
        "demo",
        {
            "main": {
                "pyproject.toml": '[tool.ow]\nplugin = "tools/ow_plugin.py"\n',
                "tools/ow_plugin.py": plugin_code,
            }
        },
        base="main",
    )
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    multi = lab.home / "demo-ws"
    multi.mkdir(parents=True)
    assert lab.ow("init", "odoo=community@18.0", "app=demo@main", cwd=multi).exit_code == 0
    assert lab.ow("trust", cwd=multi).exit_code == 0

    up = lab.ow("run", "-d", cwd=multi)
    assert up.exit_code == 0, up.stderr + up.stdout
    assert "SELF_DETACHED_UP" in capfd.readouterr().out
    assert (multi / ".demo_running").is_file()

    # Passthrough subcommand still works while running.
    passthrough = lab.ow("run", "--", "doctor", cwd=multi)
    assert passthrough.exit_code == 0
    assert "PASS ['doctor']" in capfd.readouterr().out

    # Starting again without passthrough args refuses.
    dup = lab.ow("run", "-d", cwd=multi)
    assert dup.exit_code == 2
    assert "already running (demo-svc)" in dup.stderr

    # `ow rm` refuses while plugin reports running services.
    rm_refused = lab.ow("rm", "demo-ws", "-y", cwd=lab.home)
    assert rm_refused.exit_code == 1
    assert "running (demo-svc)" in rm_refused.stdout

    # `ow logs` delegates to `ow_multi_logs`.
    logs = lab.ow("logs", "-n", "12", cwd=multi)
    assert logs.exit_code == 0
    assert "PLUGIN_LOGS False 12" in capfd.readouterr().out

    # `ow stop` delegates to `ow_multi_stop`.
    stopped = lab.ow("stop", cwd=multi)
    assert stopped.exit_code == 0
    assert "PLUGIN_STOPPED" in capfd.readouterr().out
    assert not (multi / ".demo_running").exists()


def test_follow_files_interleaves_chronologically_keeping_multiline_tracebacks(tmp_path, capsys):
    from ow.core.runtime import follow_files

    a = tmp_path / "client.log"
    b = tmp_path / "iap.log"
    a.write_text(
        "2026-11-23 12:00:01,000 INFO first\n"
        "2026-11-23 12:00:03,000 ERROR boom\n"
        "  Traceback line 1\n"
        "  ValueError: bad\n",
        encoding="utf-8",
    )
    b.write_text(
        "2026-11-23 12:00:02,000 INFO middle\n"
        "2026-11-23 12:00:04,000 INFO last\n",
        encoding="utf-8",
    )

    rc = follow_files([("client", a), ("iap", b)], lines=20, follow=False)
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "[client] 2026-11-23 12:00:01,000 INFO first",
        "[iap] 2026-11-23 12:00:02,000 INFO middle",
        "[client] 2026-11-23 12:00:03,000 ERROR boom",
        "[client]   Traceback line 1",
        "[client]   ValueError: bad",
        "[iap] 2026-11-23 12:00:04,000 INFO last",
    ]
