import subprocess

from ow.utils.config import BranchSpec, Config, WorkspaceConfig
from ow.utils.refs import fetch_workspace_refs
from ow.utils import paths


def _workspace(tmp_path, alias="community"):
    """A workspace whose worktree exists but whose bare repo does not."""
    ws_dir = tmp_path / "workspaces" / "ws"
    (ws_dir / alias).mkdir(parents=True)
    config = Config(vars={}, remotes={})
    ws = WorkspaceConfig(
        templates=[], vars={}, repos={alias: BranchSpec("origin/master", "feature")}
    )
    return config, ws, ws_dir


class TestMissingBareRepo:
    """A missing bare repo is a broken project, not a missing branch."""

    def test_names_the_missing_bare_repo(self, tmp_path, capsys, xdg):
        config, ws, ws_dir = _workspace(tmp_path)

        fetch_workspace_refs(ws, ws_dir, config)

        err = capsys.readouterr().err
        assert str(paths.repos_dir() / "community.git") in err
        assert "ow apply" in err

    def test_does_not_blame_the_branch(self, tmp_path, capsys, xdg):
        """The old path reported 'Branch <x> not found in local refs' instead."""
        config, ws, ws_dir = _workspace(tmp_path)

        fetch_workspace_refs(ws, ws_dir, config)

        assert "not found in local refs" not in capsys.readouterr().err

    def test_labels_the_failure_as_resolve_not_fetch(self, tmp_path, capsys, xdg):
        """No fetch is attempted when resolution fails; 'git fetch ?' claimed otherwise."""
        config, ws, ws_dir = _workspace(tmp_path)

        fetch_workspace_refs(ws, ws_dir, config)

        err = capsys.readouterr().err
        assert "resolve" in err
        assert "fetch ?" not in err

    def test_falls_back_to_the_declared_base_ref(self, tmp_path, capsys, xdg):
        config, ws, ws_dir = _workspace(tmp_path)

        outcome = fetch_workspace_refs(ws, ws_dir, config)

        assert outcome.tracks["community"] == "origin/master"


class TestUpstreamBefore:
    """The SHA read before the fetch is what makes force-push detection
    possible without consulting a reflog."""

    def test_records_the_upstream_sha_before_fetching(self, tmp_path, monkeypatch, xdg):
        from ow.utils.config import BranchSpec, Config, WorkspaceConfig
        from ow.utils import refs as refs_mod

        ws_dir = tmp_path / "ws"
        (ws_dir / "community").mkdir(parents=True)
        bare = paths.repos_dir() / "community.git"
        bare.mkdir(parents=True)

        config = Config(vars={}, remotes={"community": {}})
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master", "work")},
            templates=[],
        )

        def fake_resolve(bare_repo, spec, alias_remotes):
            if spec.local_branch is not None:
                return BranchSpec("dev/work", "work")
            return BranchSpec("origin/master")

        monkeypatch.setattr(refs_mod, "rev_parse", lambda repo, ref: "cafebabe" * 5)
        monkeypatch.setattr(refs_mod, "get_upstream", lambda p: None)
        monkeypatch.setattr(
            refs_mod, "parallel_per_repo",
            lambda tasks, on_done=None: {k: fn() for k, fn in tasks.items()},
        )
        monkeypatch.setattr(
            refs_mod, "_run",
            lambda *a, **k: __import__("subprocess").CompletedProcess(a, 0, b"", b""),
        )

        outcome = refs_mod.fetch_workspace_refs(
            ws, ws_dir, config, fetch_upstreams=True, resolve_fn=fake_resolve,
        )

        assert outcome.upstream_before["community"] == "cafebabe" * 5
        assert outcome.tracks["community"] == "origin/master"
        assert outcome.upstreams["community"] == "dev/work"


def test_fetch_jobs_stay_routed_through_tracked_run(tmp_path, monkeypatch, xdg):
    """Guards against `_do_fetch` reverting to a raw subprocess.run.

    Those are the parallel `git fetch` calls issue #26 is about: if they ever
    bypass `_run`, they spawn untracked children that `terminate_children`
    cannot kill, and the tests would keep passing since nothing else exercises
    a real fetch.

    Patches refs_mod._run with a Mock and drives a real fetch job through it,
    so a trivial `import subprocess as sp; sp.run(...)` refactor still fails
    the test — a source-text search would miss that rewrite.
    """
    import subprocess
    from unittest.mock import Mock

    from ow.utils import refs as refs_mod
    from ow.utils import git as git_mod

    alias = "community"
    ws_dir = tmp_path / "ws"
    (ws_dir / alias).mkdir(parents=True)
    bare = paths.repos_dir() / f"{alias}.git"
    bare.mkdir(parents=True)

    config = Config(vars={}, remotes={alias: {}})
    ws = WorkspaceConfig(
        repos={alias: BranchSpec("origin/master")}, templates=[],
    )

    def fake_resolve(bare_repo, spec, alias_remotes):
        return BranchSpec("origin/master")

    # A different failure mode than a raw subprocess.run: local shadowing of
    # the tracked _run. Check identity before patching it away below.
    assert refs_mod._run is git_mod._run

    mock_run = Mock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
    monkeypatch.setattr(refs_mod, "_run", mock_run)

    refs_mod.fetch_workspace_refs(ws, ws_dir, config, resolve_fn=fake_resolve)

    assert mock_run.called


class TestFetchFailureIsReported:
    """D2 — printing ✗ is not a signal a caller can act on.

    `ow rebase` planned and executed against stale cached refs after a fetch
    failed, and exited 0. Worse than a plain stale rebase: the upstream ref
    never moved, so force_pushed stayed False and even the `rewritten` marker
    was suppressed.
    """

    def _drive(self, tmp_path, monkeypatch, run_result):
        """Run one real fetch job whose `_run` yields `run_result`.

        An Exception instance stands for a `_run` that raises — what
        parallel_per_repo hands back as the job's result.
        """
        from ow.utils import refs as refs_mod

        ws_dir = tmp_path / "ws"
        (ws_dir / "community").mkdir(parents=True)
        (paths.repos_dir() / "community.git").mkdir(parents=True)

        config = Config(vars={}, remotes={"community": {}})
        ws = WorkspaceConfig(
            repos={"community": BranchSpec("origin/master")}, templates=[],
        )

        def fake_run(*a, **k):
            if isinstance(run_result, Exception):
                raise run_result
            return run_result

        monkeypatch.setattr(refs_mod, "_run", fake_run)
        monkeypatch.setattr(
            refs_mod, "parallel_per_repo",
            lambda tasks, on_done=None: {k: _collect(fn) for k, fn in tasks.items()},
        )

        return refs_mod.fetch_workspace_refs(
            ws, ws_dir, config,
            resolve_fn=lambda bare, spec, remotes: BranchSpec("origin/master"),
        )

    def test_a_nonzero_fetch_marks_the_alias_failed(self, tmp_path, monkeypatch, capsys, xdg):
        outcome = self._drive(
            tmp_path, monkeypatch,
            subprocess.CompletedProcess([], 1, b"", b"fatal: unreachable"),
        )

        assert "community" in outcome.failed

    def test_a_raising_fetch_marks_the_alias_failed(self, tmp_path, monkeypatch, capsys, xdg):
        outcome = self._drive(tmp_path, monkeypatch, OSError("no such host"))

        assert "community" in outcome.failed

    def test_a_successful_fetch_marks_nothing(self, tmp_path, monkeypatch, capsys, xdg):
        outcome = self._drive(
            tmp_path, monkeypatch, subprocess.CompletedProcess([], 0, b"", b""),
        )

        assert outcome.failed == frozenset()

    def test_a_missing_bare_repo_counts_as_a_failure(self, tmp_path, capsys, xdg):
        """Resolution never got far enough to fetch anything."""
        config, ws, ws_dir = _workspace(tmp_path)

        outcome = fetch_workspace_refs(ws, ws_dir, config)

        assert "community" in outcome.failed


def _collect(fn):
    """parallel_per_repo's contract: a raising task becomes its exception."""
    try:
        return fn()
    except Exception as exc:
        return exc
