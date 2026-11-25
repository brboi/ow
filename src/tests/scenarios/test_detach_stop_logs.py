"""Integration tests for `ow run -d/--detach`, `ow stop`, `ow logs`, `ow status`, and `ow rm` guards."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from ow.core.paths import venvs_dir
from tests.conftest import add_repo, init_workspace
from tests.lab import make_executable, make_fake_engine


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
    make_executable(py)


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

    calls, running_containers = make_fake_engine(monkeypatch,
    running={
        "ow-postgres": {5432: 5432},
        "ow-pgweb": {8081: 8081},
        "ow-mailsink": {1025: 1025, 8025: 8025},
    },)
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
def ow_runner(ws, multi, args):
    if ws is not None:
        return None
    sf = _state_file(multi.root)
    if args:
        return RunCommand(argv=(sys.executable, "-c", f"print('PASS', {list(args)!r})"), cwd=multi.root)
    code = f"from pathlib import Path; Path({str(sf)!r}).write_text('up'); print('SELF_DETACHED_UP')"
    return RunCommand(argv=(sys.executable, "-c", code), cwd=multi.root, self_detaching=True)

@hookimpl
def ow_running(ws):
    root = ws.root.parent if ws.is_stack else ws.root
    return ["demo-svc"] if _state_file(root).is_file() else None

@hookimpl
def ow_stop(ws, multi):
    if ws is not None:
        return None
    sf = _state_file(multi.root)
    code = f"from pathlib import Path; Path({str(sf)!r}).unlink(missing_ok=True); print('PLUGIN_STOPPED')"
    return RunCommand(argv=(sys.executable, "-c", code), cwd=multi.root)

@hookimpl
def ow_logs(ws, multi):
    if ws is not None:
        return None
    return [("demo", multi.root / ".demo.log")]

@hookimpl
def ow_status(ws, spec):
    if spec is not None or ws is None:
        return []
    root = ws.root.parent if ws.is_stack else ws.root
    if _state_file(root).is_file():
        return [StatusInfo(plugin=PLUGIN_NAME, scope="workspace", subject=ws.name, summary="running (demo-svc)")]
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
    assert lab.ow("trust", "-y", cwd=multi).exit_code == 0

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

    # `ow logs` shows the files `ow_logs` returns for the multi-stack root.
    (multi / ".demo.log").write_text("PLUGIN_LOGS\n")
    logs = lab.ow("logs", cwd=multi)
    assert logs.exit_code == 0
    assert "PLUGIN_LOGS" in logs.stdout

    # `ow stop` delegates to the root's `ow_stop`.
    stopped = lab.ow("stop", cwd=multi)
    assert stopped.exit_code == 0
    assert "PLUGIN_STOPPED" in capfd.readouterr().out
    assert not (multi / ".demo_running").exists()


_SERVICE_PLUGIN = '''
import sys
from ow.plugin_api import RunCommand, hookimpl

OW_API_VERSION = 1
PLUGIN_NAME = "svc"
DIES_ALONE = __DIES_ALONE__

START = """
import subprocess, sys
from pathlib import Path
state, log, dies = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] == "1"
state.write_text("up")
log.write_text("SVC_STARTED\\\\n")
if dies:
    waiter = "import os, sys, time; time.sleep(1); os.unlink(sys.argv[1])"
    subprocess.Popen([sys.executable, "-c", waiter, str(state)], start_new_session=True)
"""

STOP = """
import sys
from pathlib import Path
Path(sys.argv[1]).unlink(missing_ok=True)
Path(sys.argv[2]).write_text("stopped")
"""


def _state(ws):
    return ws.root / ".svc_state"


@hookimpl
def ow_runner(ws, multi, args):
    if ws is None:
        return None
    argv = (sys.executable, "-c", START, str(_state(ws)), str(ws.root / ".svc.log"), "1" if DIES_ALONE else "0")
    return RunCommand(argv=argv, cwd=ws.root, self_detaching=True)


@hookimpl
def ow_running(ws):
    return ["svc"] if _state(ws).is_file() else None


@hookimpl
def ow_logs(ws, multi):
    return [("svc", ws.root / ".svc.log")]


@hookimpl
def ow_stop(ws, multi):
    argv = (sys.executable, "-c", STOP, str(_state(ws)), str(ws.root / ".svc_stopped"))
    return RunCommand(argv=argv, cwd=ws.root)
'''


def _service_workspace(lab, *, dies_alone: bool) -> Path:
    add_repo(
        lab,
        "demo",
        {
            "main": {
                "pyproject.toml": '[tool.ow]\nplugin = "ow_plugin.py"\n',
                "ow_plugin.py": _SERVICE_PLUGIN.replace("__DIES_ALONE__", repr(dies_alone)),
            }
        },
        base="main",
    )
    ws = lab.ws("svc")
    ws.mkdir(parents=True)
    assert lab.ow("init", "demo@main", cwd=ws).exit_code == 0
    assert lab.ow("trust", "-y", cwd=ws).exit_code == 0
    return ws


def test_self_detaching_runner_in_the_foreground_follows_the_logs_then_stops_the_services(lab):
    ws = _service_workspace(lab, dies_alone=True)

    res = lab.ow("run", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    # ow started the command, followed the files `ow_logs` returned until `ow_running` went quiet ...
    assert "SVC_STARTED" in res.stdout
    # ... and called `ow_stop` on the way out.
    assert (ws / ".svc_stopped").read_text() == "stopped"
    assert not (ws / ".svc_state").exists()


def test_self_detaching_runner_with_detach_starts_and_returns_without_stopping(lab):
    ws = _service_workspace(lab, dies_alone=False)

    res = lab.ow("run", "-d", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    assert (ws / ".svc_state").is_file()
    assert not (ws / ".svc_stopped").exists()
    assert lab.ow("stop", cwd=ws).exit_code == 0
    assert (ws / ".svc_stopped").is_file()


def test_self_detaching_runner_in_the_foreground_is_stopped_on_ctrl_c(lab):
    import os
    import select
    import signal
    import subprocess

    ws = _service_workspace(lab, dies_alone=False)
    proc = subprocess.Popen(
        [sys.executable, "-m", "ow", "run"],
        cwd=ws,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # Its own session: `killpg(SIGINT)` reaches ow like a terminal's Ctrl+C.
        start_new_session=True,
    )
    try:
        seen = ""
        deadline = time.monotonic() + 20
        fd = proc.stdout.fileno()
        while "SVC_STARTED" not in seen:
            assert time.monotonic() < deadline, f"services never followed:\n{seen}"
            # Raw reads: a buffered readline() can pull several lines at once and leave the
            # rest where select() no longer sees them.
            if select.select([fd], [], [], 0.2)[0]:
                seen += os.read(fd, 65536).decode()
        assert (ws / ".svc_state").is_file()
        assert not (ws / ".svc_stopped").exists()

        os.killpg(proc.pid, signal.SIGINT)

        assert proc.wait(timeout=20) == 130
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    assert (ws / ".svc_stopped").read_text() == "stopped"
    assert not (ws / ".svc_state").exists()


def test_stop_and_logs_hooks_get_the_stack_and_its_multi_stack_root_when_the_root_does_not_answer(lab):
    plugin_code = """
from ow.plugin_api import hookimpl

OW_API_VERSION = 1
PLUGIN_NAME = "perstack"

@hookimpl
def ow_stop(ws, multi):
    if ws is None or not any(r.name == "demo" for r in ws.repos):
        return None
    return [f"perstack stopped {ws.name} of {multi.name}"]

@hookimpl
def ow_logs(ws, multi):
    if ws is None or not any(r.name == "demo" for r in ws.repos):
        return None
    return [(f"{ws.name}@{multi.name}", ws.root / ".perstack.log"), ("extra", ws.root / ".perstack2.log")]
"""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(
        lab,
        "demo",
        {"main": {"pyproject.toml": '[tool.ow]\nplugin = "ow_plugin.py"\n', "ow_plugin.py": plugin_code}},
        base="main",
    )
    multi = lab.home / "per-stack"
    multi.mkdir(parents=True)
    assert lab.ow("init", "odoo=community@18.0", "app=demo@main", cwd=multi).exit_code == 0
    assert lab.ow("trust", "-y", cwd=multi).exit_code == 0
    (multi / "app" / ".perstack.log").write_text("PERSTACK_LOG\n")
    (multi / "app" / ".perstack2.log").write_text("PERSTACK_EXTRA\n")

    stopped = lab.ow("stop", cwd=multi)
    assert stopped.exit_code == 0, stopped.stderr
    assert "# perstack stopped app of per-stack" in stopped.stdout
    assert "odoo of" not in stopped.stdout

    logs = lab.ow("logs", cwd=multi)
    assert logs.exit_code == 0, logs.stderr
    assert "[app@per-stack] PERSTACK_LOG" in logs.stdout
    assert "[extra] PERSTACK_EXTRA" in logs.stdout


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


def test_logs_with_nothing_to_show_exits_one(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="never-ran")

    res = lab.ow("logs", cwd=ws)

    assert res.exit_code == 1
    assert "no logs found" in res.stderr


def test_run_detach_starts_a_fresh_log_each_time(lab):
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="fresh-log")
    _install_fake_server_python(venvs_dir() / "py3.12-18.0", marker="BOOT")
    log = ws / ".ow" / "logs" / "fresh-log.log"

    assert lab.ow("run", "-d", cwd=ws).exit_code == 0
    try:
        _wait_for(log, "BOOT")
    finally:
        assert lab.ow("stop", cwd=ws).exit_code == 0
    first = log.read_text(encoding="utf-8")

    assert lab.ow("run", "-d", cwd=ws).exit_code == 0
    try:
        _wait_for(log, "BOOT")
        assert log.read_text(encoding="utf-8").count("BOOT") == first.count("BOOT")
    finally:
        assert lab.ow("stop", cwd=ws).exit_code == 0


def test_logs_follow_returns_when_the_followed_process_dies(lab):
    import threading

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="follow-ws")
    _install_fake_server_python(venvs_dir() / "py3.12-18.0", marker="FOLLOWED")
    log = ws / ".ow" / "logs" / "follow-ws.log"
    assert lab.ow("run", "-d", cwd=ws).exit_code == 0
    try:
        _wait_for(log, "FOLLOWED")
        results = []
        follower = threading.Thread(
            target=lambda: results.append(lab.ow("logs", "-f", cwd=ws)), daemon=True
        )
        follower.start()
        follower.join(timeout=1.5)
        assert follower.is_alive()  # still running: it follows

        assert lab.ow("stop", cwd=ws).exit_code == 0
        follower.join(timeout=10)
        assert not follower.is_alive()
    finally:
        lab.ow("stop", cwd=ws)

    assert results[0].exit_code == 0
    assert "FOLLOWED" in results[0].stdout


def test_tail_lines_reads_only_the_end_of_a_long_file(tmp_path):
    from ow.core.runtime import tail_lines

    log = tmp_path / "big.log"
    log.write_text("".join(f"line {i}\n" for i in range(5000)) + "no newline", encoding="utf-8")

    assert tail_lines(log, 3) == ["line 4998", "line 4999", "no newline"]
    assert tail_lines(log, 6000)[0] == "line 0"
    assert len(tail_lines(log, 6000)) == 5001
    assert tail_lines(tmp_path / "missing.log", 3) == []


def test_launch_lock_serializes_detached_launches_of_one_root(tmp_path):
    import threading

    from ow.core import runtime

    order: list[str] = []
    holding = threading.Event()

    def first() -> None:
        with runtime.launch_lock([tmp_path]):
            holding.set()
            time.sleep(0.3)
            order.append("first done")

    def second() -> None:
        holding.wait()
        with runtime.launch_lock([tmp_path]):
            order.append("second ran")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert order == ["first done", "second ran"]


def test_a_process_whose_start_time_is_unknown_is_not_considered_ours(tmp_path):
    import os

    from ow.core import runtime

    def record(starttime):
        return runtime.RunningProcess(
            label="x", pid=os.getpid(), starttime=starttime, log_path=tmp_path / "x.log", started_at="", argv=()
        )

    # The PID is alive and ours, but a record without a start time cannot prove it: a recycled PID would look the same.
    assert runtime.is_alive(record(runtime.process_starttime(os.getpid())))
    assert not runtime.is_alive(record(None))
    assert not runtime.is_alive(record("1"))


def _wait_for(path: Path, text: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and text in path.read_text(encoding="utf-8"):
            return
        time.sleep(0.05)
    raise AssertionError(f"{text!r} never appeared in {path}")


def test_an_empty_plugin_log_list_falls_back_to_the_ow_logs_files(lab):
    plugin_code = """
from ow.plugin_api import hookimpl

OW_API_VERSION = 1
PLUGIN_NAME = "nologs"

@hookimpl
def ow_logs(ws):
    return []
"""
    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    add_repo(
        lab,
        "demo",
        {"main": {"pyproject.toml": '[tool.ow]\nplugin = "ow_plugin.py"\n', "ow_plugin.py": plugin_code}},
        base="main",
    )
    ws = lab.ws("quiet")
    ws.mkdir(parents=True)
    assert lab.ow("init", "community@18.0,demo@main", cwd=ws).exit_code == 0
    assert lab.ow("trust", "-y", cwd=ws).exit_code == 0
    (ws / ".ow" / "logs").mkdir(parents=True, exist_ok=True)
    (ws / ".ow" / "logs" / "quiet.log").write_text("FROM_OW_LOGS\n", encoding="utf-8")

    res = lab.ow("logs", cwd=ws)

    assert res.exit_code == 0, res.stderr + res.stdout
    assert "FROM_OW_LOGS" in res.stdout
