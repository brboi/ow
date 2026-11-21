"""Container runtime helper shared by service plugins (`postgres`, `mailsink`).

Support module, not a plugin. Detects `docker` or `podman`, inspects and ensures
global shared containers, and resolves non-colliding host ports.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ContainerState:
    """Observed state and published `container_port -> host_port` map of one container."""

    status: Literal["running", "stopped"]
    ports: Mapping[int, int]


def detect_engine() -> str | None:
    """Return the container CLI binary (`docker` or `podman`), or None when absent."""
    override = os.environ.get("OW_CONTAINER_ENGINE", "").strip()
    if override:
        return override if shutil.which(override) else None
    for candidate in ("docker", "podman"):
        if shutil.which(candidate):
            return candidate
    return None


@functools.cache
def engine_ready(engine: str) -> bool:
    """Whether `engine` can reach its daemon (`<engine> info` exits 0)."""
    try:
        res = subprocess.run(
            [engine, "info"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _parse_ports(raw_ports: object) -> dict[int, int]:
    """Parse Docker/Podman `NetworkSettings.Ports` into `{container_port: host_port}`."""
    if not isinstance(raw_ports, dict):
        return {}
    result: dict[int, int] = {}
    for spec, bindings in raw_ports.items():
        if not isinstance(spec, str) or not isinstance(bindings, list) or not bindings:
            continue
        port_part = spec.split("/", 1)[0]
        if not port_part.isdigit():
            continue
        first = bindings[0]
        if isinstance(first, dict):
            host_port = str(first.get("HostPort") or "")
            if host_port.isdigit():
                result[int(port_part)] = int(host_port)
    return result


def inspect_container(engine: str, name: str) -> ContainerState | None:
    """Return the container's state and published ports, or None when it does not exist."""
    try:
        res = subprocess.run(
            [engine, "inspect", name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    try:
        payload = json.loads(res.stdout)
    except ValueError:
        return None
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return None
    entry = payload[0]
    state = entry.get("State")
    running = isinstance(state, dict) and bool(state.get("Running"))
    net = entry.get("NetworkSettings")
    ports = _parse_ports(net.get("Ports") if isinstance(net, dict) else None)
    return ContainerState(status="running" if running else "stopped", ports=ports)


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Whether `host:port` is currently unbound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def resolve_host_port(
    engine: str | None,
    container_name: str,
    container_port: int,
    preferred: int,
    fallback: int,
) -> int:
    """Pick the host port for `container_name`: existing mapping, else `preferred` if free, else `fallback`."""
    if engine is not None:
        state = inspect_container(engine, container_name)
        if state is not None and container_port in state.ports:
            return state.ports[container_port]
    return preferred if is_port_free(preferred) else fallback


def ensure_network(engine: str, name: str = "ow-net") -> None:
    """Create bridge network `name` if it does not already exist."""
    check = subprocess.run(
        [engine, "network", "inspect", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode == 0:
        return
    created = subprocess.run(
        [engine, "network", "create", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0:
        detail = (created.stderr or created.stdout).strip()
        raise RuntimeError(f"{engine} network create {name} failed: {detail}")


def ensure_container(
    engine: str,
    name: str,
    *,
    image: str,
    ports: Sequence[tuple[int, int]],
    env: Mapping[str, str] = (),
    volumes: Sequence[tuple[str, str]] = (),
    network: str | None = None,
) -> Literal["running", "started", "created"]:
    """Ensure container `name` is running, starting or creating it if needed."""
    state = inspect_container(engine, name)
    if state is not None:
        if state.status == "running":
            return "running"
        started = subprocess.run(
            [engine, "start", name],
            capture_output=True,
            text=True,
            check=False,
        )
        if started.returncode != 0:
            detail = (started.stderr or started.stdout).strip()
            raise RuntimeError(f"{engine} start {name} failed: {detail}")
        return "started"

    argv = [engine, "run", "-d", "--name", name]
    if network:
        argv.extend(["--network", network])
    for host_port, container_port in ports:
        argv.extend(["-p", f"127.0.0.1:{host_port}:{container_port}"])
    for source, target in volumes:
        argv.extend(["-v", f"{source}:{target}"])
    if isinstance(env, Mapping):
        for key, value in env.items():
            argv.extend(["-e", f"{key}={value}"])
    argv.append(image)

    created = subprocess.run(argv, capture_output=True, text=True, check=False)
    if created.returncode != 0:
        detail = (created.stderr or created.stdout).strip()
        raise RuntimeError(f"{engine} run {name} failed: {detail}")
    return "created"


def wait_for_tcp(host: str, port: int, *, timeout: float = 15.0) -> bool:
    """Poll `host:port` until a TCP connection succeeds or `timeout` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def exec_container(
    engine: str,
    name: str,
    cmd: Sequence[str],
    *,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    """Run `<engine> exec <name> <cmd...>` and return the completed process."""
    return subprocess.run(
        [engine, "exec", name, *cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def wait_for_exec(
    engine: str,
    name: str,
    cmd: Sequence[str],
    *,
    timeout: float = 30.0,
) -> bool:
    """Poll `<engine> exec <name> <cmd...>` until it exits 0 or `timeout` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            res = subprocess.run(
                [engine, "exec", name, *cmd],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if res.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(0.3)
    return False
