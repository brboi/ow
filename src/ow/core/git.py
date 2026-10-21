"""Git command runner and utilities."""

from __future__ import annotations

import functools
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Sequence

from ow.errors import OwError, UsageError


class GitFailed(OwError):
    """A git command failed with a non-zero exit code."""

    exit_code = 1

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        cmd = shlex.join(argv)
        super().__init__(f"git command failed ({returncode}): {cmd}\n{stderr}")


def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a git command, capturing stdout and stderr as text.
    
    Never raises, even on non-zero exit. Returns CompletedProcess with stdout/stderr as strings.
    Sets GIT_TERMINAL_PROMPT=0 in the child environment.
    """
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=env,
    )
    return result


def read(argv: Sequence[str], *, check: bool = True) -> str:
    """Run a git read command and return stdout.
    
    Strips the trailing newline. If check=True and the command fails (non-zero exit),
    raises GitFailed.
    """
    result = run(argv)
    if check and result.returncode != 0:
        raise GitFailed(argv, result.returncode, result.stderr)
    return result.stdout.rstrip("\n")


@functools.cache
def check_version() -> None:
    """Check that git version is >= 2.45, raising UsageError if not.
    
    Result is cached per process.
    """
    output = read(["git", "version"])
    # Examples: "git version 2.45.4", "2.39.5 (Apple Git-154)", "2.45.0.windows.1"
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", output)
    if not match:
        raise UsageError(f"Could not parse git version: {output}")
    
    major = int(match.group(1))
    minor = int(match.group(2))
    
    if (major, minor) < (2, 45):
        raise UsageError(f"git {major}.{minor} is too old; need 2.45 or later")


def show(argv: Sequence[str], root: Path | None = None) -> str:
    """Display git command line for user consumption.
    
    Applies these rules:
    - The value following `-C` is relativized to `root` when given and the value is `root` or under it.
    - Path arguments under $HOME are printed as `~/...` with the path part shell-quoted.
    - Other paths are shell-quoted as-is.
    - Non-path arguments are shell-quoted.
    """
    home = Path.home()
    result = []
    i = 0
    
    while i < len(argv):
        arg = argv[i]
        
        # Handle -C option: next arg is the directory
        if arg == "-C" and i + 1 < len(argv):
            result.append(arg)
            i += 1
            path_val = argv[i]
            path = Path(path_val)
            
            # Relativize to root: the root itself prints as ".", anything under it
            # as its relative remainder, so the line stays runnable from the root.
            if root:
                try:
                    rel = path.relative_to(root)
                    result.append(shlex.quote(str(rel)) if str(rel) != "." else ".")
                    i += 1
                    continue
                except ValueError:
                    pass  # Not under root
            
            # Display the path
            try:
                rel = path.relative_to(home)
                # It's under home, display as ~/...
                rest = str(rel)
                result.append("~/" + shlex.quote(rest))
            except ValueError:
                # Not under home, use absolute path with quotes
                result.append(shlex.quote(str(path)))
            i += 1
        else:
            # Check if this argument is a path under home
            try:
                path = Path(arg)
                if path.is_absolute():
                    rel = path.relative_to(home)
                    # It's under home, display as ~/...
                    rest = str(rel)
                    result.append("~/" + shlex.quote(rest))
                else:
                    result.append(shlex.quote(arg))
            except (ValueError, OSError):
                # Not a path under home or not a valid path, quote it
                result.append(shlex.quote(arg))
            i += 1
    
    return " ".join(result)
