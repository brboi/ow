"""Answer ssh's passphrase prompt on behalf of git children.

Every git child is spawned with start_new_session=True (see git._run): the
child lands in its own session so a Ctrl-C typed at the terminal never
reaches it and ow decides when it dies — an abandoned `git fetch` holds a
lock inside a bare repo every workspace shares.

The price is that the child has no controlling terminal, so ssh cannot
open /dev/tty to ask for a key passphrase. It falls back to SSH_ASKPASS,
which on a machine with no graphical askpass installed fails as
`Permission denied (publickey)` — issue #51.

ow still owns the terminal, so it does the asking. A broker listens on a
Unix socket; git children get SSH_ASKPASS pointing at a tiny shim that
forwards the prompt to that socket and prints the answer back. No extra
network round trip, no probe, no loss of interrupt safety.
"""

import base64
import getpass
import os
import shutil
import socket
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_ACTIVE: dict[str, str] = {}

# One prompt at a time: parallel fetches would otherwise paint the terminal
# over each other, and the user could not tell which key is being asked for.
_prompt_lock = threading.Lock()


def child_env() -> dict[str, str]:
    """Env additions every git child must carry. Empty when no broker is active."""
    return dict(_ACTIVE)


def _ask(prompt: str) -> str:
    """Read one secret from the real terminal. Never echoes, never logs."""
    return getpass.getpass(prompt.rstrip() + " ", stream=sys.stderr)


def _serve(listener: socket.socket, answers: dict[str, str]) -> None:
    """Accept loop. Ends when the listening socket is closed."""
    while True:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            try:
                _handle(conn, answers)
            except Exception:
                # A broken client must not take the broker down with it:
                # the next fetch may still need an answer.
                pass


def _handle(conn: socket.socket, answers: dict[str, str]) -> None:
    chunks: list[bytes] = []
    while not chunks or not chunks[-1].endswith(b"\n"):
        chunk = conn.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
    payload = b"".join(chunks).strip()
    prompt = base64.b64decode(payload).decode("utf-8", "replace") if payload else ""

    with _prompt_lock:
        answer = answers.get(prompt)
        if answer is None:
            try:
                answer = _ask(prompt)
            except (EOFError, KeyboardInterrupt, OSError):
                answer = ""
            else:
                # Four repos behind the same key ask the same question. Cache
                # it for the life of this ow run only — nothing reaches disk.
                answers[prompt] = answer

    conn.sendall(base64.b64encode(answer.encode("utf-8")) + b"\n")


# PYTHONPATH is pinned to the package this module was imported from, so the
# shim always loads the same ow as the process that wrote it — a source
# checkout, a venv, or an editable install pointing somewhere else entirely.
_SHIM = (
    "#!/bin/sh\n"
    'PYTHONPATH="{pkg_parent}${{PYTHONPATH:+:$PYTHONPATH}}"\n'
    "export PYTHONPATH\n"
    'exec "{python}" -m ow.askpass "$@"\n'
)


@contextmanager
def broker() -> Iterator[None]:
    """Serve askpass requests from git children for the duration of the block."""
    global _ACTIVE
    previous = _ACTIVE

    if not sys.stdin.isatty():
        # Nobody to ask. Say so up front rather than letting ssh hang on a
        # graphical askpass or git block on a credential prompt.
        _ACTIVE = {"SSH_ASKPASS_REQUIRE": "never", "GIT_TERMINAL_PROMPT": "0"}
        try:
            yield
        finally:
            _ACTIVE = previous
        return

    directory = Path(tempfile.mkdtemp(prefix="ow-askpass-"))
    sock_path = directory / "sock"
    shim_path = directory / "ask"

    # A generated shim rather than a console script: it works the same from
    # a source checkout and from an installed wheel, with no dependency on
    # PATH or on where the wheel put its entry points.
    shim_path.write_text(
        _SHIM.format(
            python=sys.executable,
            pkg_parent=Path(__file__).resolve().parents[2],
        ),
        encoding="utf-8",
    )
    shim_path.chmod(0o700)

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(sock_path))
        listener.listen(16)
    except OSError:
        listener.close()
        shutil.rmtree(directory, ignore_errors=True)
        _ACTIVE = previous
        yield
        return

    answers: dict[str, str] = {}
    thread = threading.Thread(target=_serve, args=(listener, answers), daemon=True)
    thread.start()

    _ACTIVE = {
        "SSH_ASKPASS": str(shim_path),
        # force needs OpenSSH >= 8.4; DISPLAY covers older ones, which only
        # allow an askpass helper when it is set to something.
        "SSH_ASKPASS_REQUIRE": "force",
        "DISPLAY": os.environ.get("DISPLAY") or ":0",
        "OW_ASKPASS_SOCK": str(sock_path),
    }
    try:
        yield
    finally:
        _ACTIVE = previous
        listener.close()
        shutil.rmtree(directory, ignore_errors=True)
