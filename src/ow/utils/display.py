import sys
import threading

# ---------------------------------------------------------------------------
# Terminal display helpers
# ---------------------------------------------------------------------------


def c(text: str, *codes: int) -> str:
    """Apply ANSI color codes to text."""
    prefix = "".join(f"\x1b[{code}m" for code in codes)
    return f"{prefix}{text}\x1b[0m"


class Spinner:
    _chars = ['|', '/', '-', '\\']

    def __init__(self, prefix: str):
        self._prefix = prefix
        self._idx = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _animate(self) -> None:
        while not self._stop_event.is_set():
            line = f"{self._prefix}  {self._chars[self._idx]}  "
            sys.stdout.write(f"\r{line}")
            sys.stdout.flush()
            self._idx = (self._idx + 1) % len(self._chars)
            self._stop_event.wait(0.1)

    def __enter__(self) -> "Spinner":
        self._thread = threading.Thread(target=self._animate, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join()
        line_len = len(self._prefix) + 4
        sys.stdout.write(f"\r{' ' * line_len}\r")
        sys.stdout.flush()


def _format_git_cmd(alias: str, cmd: str, args: list[str]) -> str:
    """Format a git command for display."""
    return f"  [{alias}] git {cmd} {' '.join(args)}"


def _print_git_result(alias: str, cmd: str, args: list[str], ok: bool, error: str | None = None) -> None:
    """Print git command result."""
    line = _format_git_cmd(alias, cmd, args)
    if ok:
        print(f"{line}  ✓")
    else:
        print(f"{line}  ✗", file=sys.stderr)
        if error:
            print(f"  Error: {error}", file=sys.stderr)


def counts(behind: int, ahead: int) -> str:
    """Format behind/ahead counts with colors."""
    b = c(f"↓{behind}", 33) if behind > 0 else c(f"↓{behind}", 2)
    a = c(f"↑{ahead}", 32) if ahead > 0 else c(f"↑{ahead}", 2)
    return f"{b} {a}"


def osc8(url: str, text: str) -> str:
    """Create an OSC8 hyperlink."""
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"
