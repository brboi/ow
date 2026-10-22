"""Git command runner and utilities."""

from __future__ import annotations

import functools
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from ow.errors import OwError, UsageError


class GitFailed(OwError):
    """A git command failed with a non-zero exit code."""

    exit_code = 1

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int,
        stderr: str,
        stdout: str = "",
    ) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        lines = [f"git command failed ({returncode}): {show(argv)}"]
        if stdout.strip():
            lines.append(stdout.rstrip("\n"))
        if stderr.strip():
            lines.append(stderr.rstrip("\n"))
        super().__init__("\n".join(lines))


@functools.cache
def _user_ssh_configured() -> bool:
    """Whether git config core.sshCommand is set."""
    try:
        result = subprocess.run(
            ["git", "config", "core.sshCommand"],
            capture_output=True,
            text=True,
            errors="replace",
        )
    except FileNotFoundError:
        raise UsageError("git not found on PATH")
    return result.returncode == 0 and bool(result.stdout.strip())


def run(argv: Sequence[str], *, live: bool = False) -> subprocess.CompletedProcess[str]:
    """Run git, capturing stdout; stderr is inherited when live on a tty.

    Never prompts: BatchMode ssh and GIT_TERMINAL_PROMPT=0 unless the user set one;
    raises UsageError when git is missing.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if (
        not env.get("GIT_SSH_COMMAND")
        and not env.get("GIT_SSH")
        and not _user_ssh_configured()
    ):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"

    stderr: int | None = None if live and sys.stderr.isatty() else subprocess.PIPE
    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            env=env,
            errors="replace",
        )
    except FileNotFoundError:
        raise UsageError("git not found on PATH")

    if result.stderr is None:
        result.stderr = ""
    return result


def read(argv: Sequence[str], *, check: bool = True) -> str:
    """Run a git read command and return stdout without the trailing newline."""
    result = run(argv)
    if check and result.returncode != 0:
        raise GitFailed(argv, result.returncode, result.stderr, result.stdout)
    return result.stdout.rstrip("\n")


@functools.cache
def check_version() -> None:
    """Check that git version is >= 2.45, raising UsageError if not."""
    output = read(["git", "version"])
    # Examples: "git version 2.45.4", "2.39.5 (Apple Git-154)", "2.45.0.windows.1"
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", output)
    if not match:
        raise UsageError(f"Could not parse git version: {output}")

    major = int(match.group(1))
    minor = int(match.group(2))

    if (major, minor) < (2, 45):
        raise UsageError(f"git {major}.{minor} is too old; need 2.45 or later")


def remote_has_branch(bare: Path, remote: str, branch: str) -> bool:
    """Whether the remote has exactly this branch, per ls-remote.

    Raises GitFailed when ls-remote fails (offline, auth).
    """
    argv = ["git", "-C", str(bare), "ls-remote", "--heads", remote, f"refs/heads/{branch}"]
    result = run(argv)
    if result.returncode != 0:
        raise GitFailed(argv, result.returncode, result.stderr, result.stdout)
    return bool(result.stdout.strip())


def remotes(bare: Path) -> tuple[str, ...]:
    """Remote names of a bare repository; empty when the bare is missing."""
    if not bare.exists():
        return ()
    result = run(["git", "-C", str(bare), "remote"])
    if result.returncode != 0:
        return ()
    return tuple(result.stdout.split())


def auth_hint(stderr: str) -> str | None:
    """One-line hint for an ssh/https auth failure, or None."""
    text = stderr.lower()
    if "permission denied" in text and "publickey" in text:
        return "load your key with `ssh-add`; ow never prompts for a passphrase"
    if "host key verification failed" in text:
        return "trust the host first: `ssh-keyscan <host> >> ~/.ssh/known_hosts`"
    if "could not read username" in text or "could not read password" in text:
        return "configure credentials so git never prompts"
    return None


def show(argv: Sequence[str], root: Path | None = None) -> str:
    """Display a git command line: `-C` under `root` workspace-relative, home paths `~/…`."""
    home = Path.home()

    def display(value: str, *, under_root: bool) -> str:
        path = Path(value)
        if under_root and root:
            try:
                rel = path.relative_to(root)
            except ValueError:
                pass
            else:
                return "." if str(rel) == "." else shlex.quote(str(rel))
        try:
            rel = path.relative_to(home)
        except ValueError:
            return shlex.quote(value)
        return "~/" + shlex.quote(str(rel))

    result = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "-C" and i + 1 < len(argv):
            result.append(arg)
            result.append(display(argv[i + 1], under_root=True))
            i += 2
            continue
        if Path(arg).is_absolute():
            result.append(display(arg, under_root=False))
        else:
            result.append(shlex.quote(arg))
        i += 1
    return " ".join(result)
