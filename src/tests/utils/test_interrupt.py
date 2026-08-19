import os
import subprocess
import sys
import threading
import time

import pytest

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
