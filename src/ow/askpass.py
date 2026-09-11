"""SSH_ASKPASS shim: ask the ow process that spawned git, print the answer.

ssh execs this with the prompt as its first argument and reads the secret
from its stdout. It runs in the git child's session, with no terminal of
its own, so it forwards the question to the broker in ow — which still has
one. See ow.utils.askpass.
"""

import base64
import os
import socket
import sys


def main() -> int:
    sock_path = os.environ.get("OW_ASKPASS_SOCK")
    if not sock_path:
        return 1

    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(sock_path)
            conn.sendall(base64.b64encode(prompt.encode("utf-8")) + b"\n")
            chunks: list[bytes] = []
            while not chunks or not chunks[-1].endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError:
        return 1

    payload = b"".join(chunks).strip()
    answer = base64.b64decode(payload).decode("utf-8", "replace") if payload else ""
    sys.stdout.write(answer + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
