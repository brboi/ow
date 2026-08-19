from pathlib import Path

from ow.utils.config import BranchSpec, Config, WorkspaceConfig
from ow.utils.refs import fetch_workspace_refs


def _workspace(tmp_path, alias="community"):
    """A project whose worktree exists but whose bare repo does not."""
    root = tmp_path / "project"
    ws_dir = root / "workspaces" / "ws"
    (ws_dir / alias).mkdir(parents=True)
    config = Config(vars={}, remotes={}, root_dir=root)
    ws = WorkspaceConfig(
        templates=[], vars={}, repos={alias: BranchSpec("origin/master", "feature")}
    )
    return config, ws, ws_dir


class TestMissingBareRepo:
    """A missing bare repo is a broken project, not a missing branch."""

    def test_names_the_missing_bare_repo(self, tmp_path, capsys):
        config, ws, ws_dir = _workspace(tmp_path)

        fetch_workspace_refs(ws, ws_dir, config)

        err = capsys.readouterr().err
        assert str(config.root_dir / ".bare-git-repos" / "community.git") in err
        assert "ow update" in err

    def test_does_not_blame_the_branch(self, tmp_path, capsys):
        """The old path reported 'Branch <x> not found in local refs' instead."""
        config, ws, ws_dir = _workspace(tmp_path)

        fetch_workspace_refs(ws, ws_dir, config)

        assert "not found in local refs" not in capsys.readouterr().err

    def test_labels_the_failure_as_resolve_not_fetch(self, tmp_path, capsys):
        """No fetch is attempted when resolution fails; 'git fetch ?' claimed otherwise."""
        config, ws, ws_dir = _workspace(tmp_path)

        fetch_workspace_refs(ws, ws_dir, config)

        err = capsys.readouterr().err
        assert "resolve" in err
        assert "fetch ?" not in err

    def test_falls_back_to_the_declared_base_ref(self, tmp_path, capsys):
        config, ws, ws_dir = _workspace(tmp_path)

        outcome = fetch_workspace_refs(ws, ws_dir, config)

        assert outcome.tracks["community"] == "origin/master"


class TestUpstreamBefore:
    """The SHA read before the fetch is what makes force-push detection
    possible without consulting a reflog."""

    def test_records_the_upstream_sha_before_fetching(self, tmp_path, monkeypatch):
        from ow.utils.config import BranchSpec, Config, WorkspaceConfig
        from ow.utils import refs as refs_mod

        ws_dir = tmp_path / "ws"
        (ws_dir / "community").mkdir(parents=True)
        bare = tmp_path / ".bare-git-repos" / "community.git"
        bare.mkdir(parents=True)

        config = Config(vars={}, remotes={"community": {}}, root_dir=tmp_path)
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


def test_fetch_jobs_stay_routed_through_tracked_run(tmp_path, monkeypatch):
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
    bare = tmp_path / ".bare-git-repos" / f"{alias}.git"
    bare.mkdir(parents=True)

    config = Config(vars={}, remotes={alias: {}}, root_dir=tmp_path)
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
