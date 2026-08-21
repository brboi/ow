import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from ow.utils import git as git_mod
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

    def test_check_raises_calledprocesserror_carrying_git_s_output(self):
        """`check=True` is what makes ensure_bare_repo, create_worktree,
        set_branch_upstream and get_rev_list_count fail loudly instead of
        quietly producing a half-built workspace. Popen has no `check`, so
        _run reimplements it — and must also carry the captured output into
        the exception, which is where the caller reads git's own message."""
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            _run(
                [sys.executable, "-c",
                 "import sys; sys.stderr.write('boom'); sys.exit(3)"],
                capture_output=True, text=True, check=True,
            )

        assert excinfo.value.returncode == 3
        assert excinfo.value.stderr == "boom"

    def test_check_is_silent_when_the_command_succeeds(self):
        result = _run([sys.executable, "-c", "pass"], capture_output=True, check=True)
        assert result.returncode == 0


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


class TestConcurrentTracking:
    def test_live_children_sees_a_child_spawned_from_another_thread(self):
        """parallel_per_repo drives every fetch job from a worker thread, so
        _run is called concurrently, not just from the main thread. This
        proves the registry a fetch job relies on actually holds the child
        while a worker thread is inside `_run`'s communicate() call — the
        exact shape a real `git fetch` takes when run in parallel.
        """
        seen_while_running = {}

        def worker():
            _run([sys.executable, "-c", "import time; time.sleep(0.3)"], capture_output=True)

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.1)
        seen_while_running["count"] = live_children()
        thread.join()

        assert seen_while_running["count"] == 1
        assert live_children() == 0

    def test_lock_does_not_serialise_concurrent_runs(self):
        """The lock in `_run` must span Popen() but release before communicate().

        If it held across communicate() too, every parallel git call would run
        one at a time — a severe, easily-missed regression. Run several slow
        children concurrently and check the wall-clock is well under their sum.
        """
        n = 5
        per_call = 0.5

        def worker():
            _run(
                [sys.executable, "-c", f"import time; time.sleep({per_call})"],
                capture_output=True,
            )

        threads = [threading.Thread(target=worker) for _ in range(n)]
        start = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.monotonic() - start

        assert elapsed < per_call * n / 2
        assert live_children() == 0


class TestParallelPerRepo:
    def test_still_returns_results_keyed_by_alias(self):
        from ow.utils.git import parallel_per_repo
        results = parallel_per_repo({"a": lambda: 1, "b": lambda: 2})
        assert results == {"a": 1, "b": 2}

    def test_still_returns_exceptions_as_values(self):
        from ow.utils.git import parallel_per_repo

        def boom():
            raise RuntimeError("nope")

        results = parallel_per_repo({"a": boom, "b": lambda: 2})
        assert isinstance(results["a"], RuntimeError)
        assert results["b"] == 2

    def test_on_done_fires_once_per_task(self):
        from ow.utils.git import parallel_per_repo
        seen = []
        parallel_per_repo(
            {"a": lambda: 1, "b": lambda: 2, "c": lambda: 3},
            on_done=seen.append,
        )
        assert sorted(seen) == ["a", "b", "c"]

    def test_on_done_fires_for_a_failing_task_too(self):
        from ow.utils.git import parallel_per_repo

        def boom():
            raise RuntimeError("nope")

        seen = []
        parallel_per_repo({"a": boom}, on_done=seen.append)
        assert seen == ["a"]

    def test_empty_tasks_is_a_no_op(self):
        from ow.utils.git import parallel_per_repo
        assert parallel_per_repo({}) == {}

    def test_an_interrupt_kills_the_children_and_propagates(self):
        """The behaviour issue #26 is about: Ctrl-C must not leave git running."""
        import sys
        from ow.utils.git import _run, live_children, parallel_per_repo

        def slow():
            return _run([sys.executable, "-c", "import time; time.sleep(30)"],
                        capture_output=True)

        def interrupt():
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            parallel_per_repo({"slow": slow, "boom": interrupt})

        assert live_children() == 0

    def test_an_on_done_callback_that_raises_propagates_and_does_not_hang(self):
        """A callback raising something other than KeyboardInterrupt (e.g. a bug
        in a caller's progress display) must not be swallowed."""
        from ow.utils.git import parallel_per_repo

        def boom(alias):
            raise RuntimeError("on_done blew up")

        with pytest.raises(RuntimeError, match="on_done blew up"):
            parallel_per_repo({"a": lambda: 1}, on_done=boom)

    def test_an_on_done_callback_that_raises_does_not_leak_the_pool(self):
        """parallel_per_repo deliberately avoids `with pool:`, so nothing shuts
        the executor down implicitly. If the `except BaseException:` clause
        stopped calling shutdown(), the worker threads would outlive the raised
        exception and keep the interpreter alive at exit.

        ThreadPoolExecutor has no public "are you shut down" flag, so this spies
        on the instance parallel_per_repo actually built and reads the private
        one — a private attribute is a fair price for covering a leak that is
        otherwise invisible from outside.
        """
        from ow.utils.git import parallel_per_repo

        built: list[ThreadPoolExecutor] = []

        class SpyPool(ThreadPoolExecutor):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                built.append(self)

        def boom(alias):
            raise RuntimeError("on_done blew up")

        with patch.object(git_mod, "ThreadPoolExecutor", SpyPool), \
             pytest.raises(RuntimeError, match="on_done blew up"):
            parallel_per_repo({"a": lambda: 1}, on_done=boom)

        assert len(built) == 1
        assert built[0]._shutdown is True
