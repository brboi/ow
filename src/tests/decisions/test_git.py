"""Tests for the git runner."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ow.core import git
from ow.core.git import GitFailed, auth_hint, remote_has_branch, remotes, run
from ow.errors import UsageError


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


@pytest.fixture
def bare_with_upstream(tmp_path: Path) -> Path:
    """A bare repo whose `origin` and `gitlab-group/test` remotes point at upstream."""
    upstream = tmp_path / "upstream.git"
    _git("init", "--bare", "-b", "main", str(upstream))

    work = tmp_path / "work"
    _git("init", "-b", "main", str(work))
    _git("-C", str(work), "config", "user.email", "t@example.com")
    _git("-C", str(work), "config", "user.name", "Test")
    (work / "README.md").write_text("x")
    _git("-C", str(work), "add", ".")
    _git("-C", str(work), "commit", "-m", "init")
    _git("-C", str(work), "branch", "x/feat")
    _git("-C", str(work), "branch", "dev/feat")
    _git("-C", str(work), "branch", "develop")
    _git("-C", str(work), "remote", "add", "origin", str(upstream))
    _git("-C", str(work), "push", "origin", "main", "x/feat", "dev/feat", "develop")

    url = upstream.as_uri()
    bare = tmp_path / "repo.git"
    _git("init", "--bare", str(bare))
    _git("-C", str(bare), "remote", "add", "origin", url)
    _git("-C", str(bare), "remote", "add", "gitlab-group/test", url)
    return bare


class TestRemoteHasBranch:
    def test_exact_match(self, bare_with_upstream: Path) -> None:
        assert remote_has_branch(bare_with_upstream, "origin", "x/feat") is True

    def test_no_partial_match(self, bare_with_upstream: Path) -> None:
        # `x/feat` and `dev/feat` exist; the bare name `feat` must not match.
        assert remote_has_branch(bare_with_upstream, "origin", "feat") is False

    def test_slash_named_remote(self, bare_with_upstream: Path) -> None:
        assert remote_has_branch(bare_with_upstream, "gitlab-group/test", "x/feat") is True

    def test_missing_remote_raises(self, bare_with_upstream: Path) -> None:
        with pytest.raises(GitFailed):
            remote_has_branch(bare_with_upstream, "nonexistent", "main")


class TestRemotes:
    def test_lists_remotes(self, bare_with_upstream: Path) -> None:
        assert remotes(bare_with_upstream) == ("gitlab-group/test", "origin")

    def test_missing_bare_returns_empty(self, tmp_path: Path) -> None:
        assert remotes(tmp_path / "missing.git") == ()


class TestRunEnv:
    def _record_env(self, monkeypatch) -> list[dict[str, str]]:
        seen: list[dict[str, str]] = []

        def fake_run(argv, **kwargs):
            seen.append(kwargs["env"])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        monkeypatch.setattr(git.subprocess, "run", fake_run)
        return seen

    def test_batch_mode_set_without_user_ssh(self, monkeypatch) -> None:
        monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
        monkeypatch.delenv("GIT_SSH", raising=False)
        monkeypatch.setattr(git, "_user_ssh_configured", lambda: False)
        seen = self._record_env(monkeypatch)

        run(["git", "version"])

        assert seen[0]["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
        assert seen[0]["GIT_TERMINAL_PROMPT"] == "0"

    def test_user_ssh_command_is_kept(self, monkeypatch) -> None:
        monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /tmp/key")
        seen = self._record_env(monkeypatch)

        run(["git", "version"])

        assert seen[0]["GIT_SSH_COMMAND"] == "ssh -i /tmp/key"

    def test_config_ssh_command_suppresses_batch_mode(self, monkeypatch) -> None:
        monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
        monkeypatch.delenv("GIT_SSH", raising=False)
        monkeypatch.setattr(git, "_user_ssh_configured", lambda: True)
        seen = self._record_env(monkeypatch)

        run(["git", "version"])

        assert "GIT_SSH_COMMAND" not in seen[0]


class TestMissingGit:
    def test_missing_git_raises_usage_error(self, monkeypatch) -> None:
        monkeypatch.setenv("PATH", "/nonexistent/ow-test-path")

        with pytest.raises(UsageError) as exc_info:
            run(["git", "version"])

        assert "git not found" in str(exc_info.value).lower()


class TestGitFailed:
    def test_includes_stdout_and_stderr(self) -> None:
        exc = GitFailed(
            ["git", "rebase"],
            returncode=1,
            stderr="could not apply 123",
            stdout="CONFLICT (content): Merge conflict in f",
        )

        message = str(exc)
        assert "CONFLICT" in message
        assert "could not apply 123" in message
        assert exc.stdout == "CONFLICT (content): Merge conflict in f"
        assert exc.stderr == "could not apply 123"
        assert not message.endswith("\n")

    def test_uses_show_for_command(self) -> None:
        exc = GitFailed(["git", "status"], 1, "")
        assert "git status" in str(exc)


class TestAuthHint:
    def test_publickey(self) -> None:
        hint = auth_hint("Permission denied (publickey).")
        assert hint is not None
        assert "ssh-add" in hint

    def test_host_key(self) -> None:
        hint = auth_hint("Host key verification failed.")
        assert hint is not None
        assert "known_hosts" in hint

    def test_username_prompt(self) -> None:
        hint = auth_hint("fatal: could not read Username for 'https://example.com'")
        assert hint is not None

    def test_unrelated(self) -> None:
        assert auth_hint("fatal: not a git repository") is None
