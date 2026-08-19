import os
import subprocess
import sys
import time

from ow.utils.git import _run, live_children, terminate_children


class TestProcessGroup:
    def test_child_gets_its_own_process_group(self):
        """A child in our own group would receive the terminal's Ctrl-C directly,
        racing us; its own group means we decide when it dies."""
        result = _run(
            [sys.executable, "-c", "import os; print(os.getpgid(0))"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert int(result.stdout.strip()) != os.getpgid(0)

    def test_output_and_returncode_survive_the_indirection(self):
        result = _run(
            [sys.executable, "-c", "import sys; sys.stdout.write('out'); sys.exit(3)"],
            capture_output=True, text=True,
        )
        assert result.stdout == "out"
        assert result.returncode == 3

    def test_registry_is_empty_once_a_child_has_finished(self):
        _run([sys.executable, "-c", "pass"], capture_output=True)
        assert live_children() == 0


class TestTerminateChildren:
    def test_kills_a_running_child(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        from ow.utils import git as git_mod
        with git_mod._children_lock:
            git_mod._children.add(proc)

        killed = terminate_children(grace=2.0)

        assert killed == 1
        assert proc.poll() is not None
        assert live_children() == 0

    def test_is_a_no_op_with_nothing_running(self):
        assert terminate_children() == 0

    def test_survives_a_child_that_already_exited(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        proc.wait()
        from ow.utils import git as git_mod
        with git_mod._children_lock:
            git_mod._children.add(proc)

        assert terminate_children() == 1
        assert live_children() == 0
