"""Foreground child handling: Ctrl+C, exit statuses and the shell `ow exec`/`ow sandbox` start."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from ow.commands import _runtime

_RUNNER = """
import sys
from pathlib import Path
from ow.commands._runtime import run_foreground

sys.exit(run_foreground([sys.executable, sys.argv[1]], cwd=Path.cwd()))
"""


_MULTI_RUNNER = """
import sys
from pathlib import Path
from ow.commands.run import _run_multi
from ow.core.plugins import RunCommand

cmd = RunCommand(argv=(sys.executable, sys.argv[1]), cwd=Path.cwd())
sys.exit(_run_multi([("a", None, cmd)]))
"""


def _child(tmp_path: Path, *, trap: bool) -> Path:
    """A child that announces itself, then either traps SIGINT and tears down slowly or ignores it."""
    ready, done = tmp_path / "ready", tmp_path / "teardown-end"
    body = f"""
    import signal, time
    from pathlib import Path

    def teardown(*_):
        time.sleep(0.5)
        Path({str(done)!r}).write_text("done")
        raise SystemExit(0)

    signal.signal(signal.SIGINT, {"teardown" if trap else "signal.SIG_IGN"})
    Path({str(ready)!r}).write_text("ready")
    time.sleep(60)
    """
    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent(body))
    return script


def _start_runner(tmp_path: Path, child: Path, source: str = _RUNNER) -> subprocess.Popen[bytes]:
    runner = tmp_path / "runner.py"
    runner.write_text(source)
    # Its own session: killpg(SIGINT) then reaches the runner and its child like a terminal's Ctrl+C.
    proc = subprocess.Popen([sys.executable, str(runner), str(child)], cwd=tmp_path, start_new_session=True)
    deadline = time.monotonic() + 10
    while not (tmp_path / "ready").exists():
        assert time.monotonic() < deadline, "child never started"
        time.sleep(0.02)
    return proc


def _ctrl_c(proc: subprocess.Popen[bytes]) -> None:
    os.killpg(proc.pid, signal.SIGINT)
    time.sleep(0.6)  # past Popen.wait's own 0.25 s grace after a first SIGINT


def test_first_ctrl_c_lets_the_child_finish_its_teardown(tmp_path):
    proc = _start_runner(tmp_path, _child(tmp_path, trap=True))

    _ctrl_c(proc)

    assert proc.wait(timeout=10) == 130
    assert (tmp_path / "teardown-end").read_text() == "done"


def test_second_ctrl_c_terminates_the_child(tmp_path):
    proc = _start_runner(tmp_path, _child(tmp_path, trap=False))

    _ctrl_c(proc)
    assert proc.poll() is None, "the first Ctrl+C must only make ow wait"
    _ctrl_c(proc)

    assert proc.wait(timeout=10) == 130
    assert not (tmp_path / "teardown-end").exists()


def test_multi_run_first_ctrl_c_lets_children_finish_their_teardown(tmp_path):
    # `ow run` of several stacks puts each child in its own session: ow forwards the SIGINT itself.
    proc = _start_runner(tmp_path, _child(tmp_path, trap=True), _MULTI_RUNNER)

    _ctrl_c(proc)

    assert proc.wait(timeout=10) == 130
    assert (tmp_path / "teardown-end").read_text() == "done"


def test_multi_run_second_ctrl_c_terminates_children(tmp_path):
    proc = _start_runner(tmp_path, _child(tmp_path, trap=False), _MULTI_RUNNER)

    _ctrl_c(proc)
    assert proc.poll() is None
    _ctrl_c(proc)

    assert proc.wait(timeout=10) == 130


def test_exit_status_maps_signals_like_a_shell():
    assert _runtime.exit_status(0) == 0
    assert _runtime.exit_status(3) == 3
    assert _runtime.exit_status(-signal.SIGTERM) == 143
    assert _runtime.exit_status(-signal.SIGKILL) == 137


def test_run_foreground_passes_the_exit_status_through(tmp_path):
    assert _runtime.run_foreground([sys.executable, "-c", "raise SystemExit(7)"], cwd=tmp_path) == 7
    kill_self = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    assert _runtime.run_foreground([sys.executable, "-c", kill_self], cwd=tmp_path) == 137


def test_default_shell_prefers_an_executable_shell_from_the_environment():
    assert _runtime.default_shell({"SHELL": "/bin/sh"}) == "/bin/sh"
    assert _runtime.default_shell({"SHELL": "/nonexistent/shell"}) in ("/bin/bash", "/usr/bin/bash", "/bin/sh")
    assert _runtime.default_shell({}) in ("/bin/bash", "/usr/bin/bash", "/bin/sh")
