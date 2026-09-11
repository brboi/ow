"""The askpass broker, exercised over its real socket.

No ssh server is needed: what matters is that the shim ssh would exec
reaches ow's prompt and prints the answer back on stdout.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ow.utils import askpass


def _ask_via_shim(env: dict[str, str], prompt: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [env["SSH_ASKPASS"], prompt],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
    )


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch):
    """Pin whether stdin is a terminal, instead of inheriting pytest's."""

    def _set(present: bool) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: present, raising=False)

    return _set


def test_the_shim_carries_the_prompt_to_ow_and_the_answer_back(tty):
    tty(True)

    with patch("ow.utils.askpass._ask", return_value="s3cret"):
        with askpass.broker():
            env = askpass.child_env()
            shim = Path(env["SSH_ASKPASS"])
            assert shim.is_file() and os.access(shim, os.X_OK)

            result = _ask_via_shim(env, "Enter passphrase for key '/k':")

    assert result.returncode == 0
    assert result.stdout == "s3cret\n"


def test_the_same_question_is_only_asked_once(tty):
    """Four repos behind one key must not mean four prompts."""
    tty(True)

    with patch("ow.utils.askpass._ask", return_value="s3cret") as ask:
        with askpass.broker():
            env = askpass.child_env()
            first = _ask_via_shim(env, "Enter passphrase for key '/k':")
            second = _ask_via_shim(env, "Enter passphrase for key '/k':")

    assert first.stdout == second.stdout == "s3cret\n"
    assert ask.call_count == 1


def test_a_different_question_is_asked_again(tty):
    tty(True)

    with patch("ow.utils.askpass._ask", side_effect=["one", "two"]) as ask:
        with askpass.broker():
            env = askpass.child_env()
            first = _ask_via_shim(env, "passphrase for /a:")
            second = _ask_via_shim(env, "passphrase for /b:")

    assert (first.stdout, second.stdout) == ("one\n", "two\n")
    assert ask.call_count == 2


def test_without_a_terminal_nothing_is_served_and_git_is_told_not_to_ask(tty):
    """A script has nobody to ask: failing at once beats hanging on a GUI askpass."""
    tty(False)

    with askpass.broker():
        env = askpass.child_env()

    assert env == {"SSH_ASKPASS_REQUIRE": "never", "GIT_TERMINAL_PROMPT": "0"}


def test_the_broker_leaves_nothing_behind(tty):
    tty(True)

    with patch("ow.utils.askpass._ask", return_value="x"):
        with askpass.broker():
            socket_path = Path(askpass.child_env()["OW_ASKPASS_SOCK"])
            assert socket_path.exists()

    assert askpass.child_env() == {}
    assert not socket_path.exists()
    assert not socket_path.parent.exists()


def test_the_shim_fails_quietly_when_no_broker_is_listening(tty):
    """ssh reads the helper's stdout: printing junk there would be handed to a server."""
    tty(True)

    with patch("ow.utils.askpass._ask", return_value="x"):
        with askpass.broker():
            env = dict(askpass.child_env())
            env["OW_ASKPASS_SOCK"] = str(Path(env["OW_ASKPASS_SOCK"]).parent / "gone")

            result = _ask_via_shim(env, "passphrase:")

    assert result.returncode == 1
    assert result.stdout == ""
