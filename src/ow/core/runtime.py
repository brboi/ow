"""Detached process tracking, stopping, and log reading for `ow run -d`, `ow stop`, and `ow logs`.

Runtime files live exclusively under the workspace root's `.ow/` directory
(`<root>/.ow/run/<label>.json` and `<root>/.ow/logs/<label>.log`), preserving the
single-`.ow/`-per-root invariant for multi-stack workspaces.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ow.core import fs
from ow.errors import UsageError

if TYPE_CHECKING:
    from ow.core.workspace import Workspace

_ANSI_COLORS = ("\033[36m", "\033[33m", "\033[35m", "\033[32m", "\033[34m", "\033[96m")
_ANSI_RESET = "\033[0m"
_LINE_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:[.,](\d{1,6}))?")


@dataclass(frozen=True)
class RunningProcess:
    """One live detached process tracked under `<root>/.ow/run/<label>.json`."""

    label: str
    pid: int
    starttime: str | None
    log_path: Path
    started_at: str
    argv: tuple[str, ...]


def runtime_root(ws: Workspace) -> Path:
    """Return the `.ow/` owner root for `ws` (the parent multi-stack root when `ws.is_stack`)."""
    return ws.root.parent if ws.is_stack else ws.root


def run_dir(root: Path) -> Path:
    """Directory holding detached PID metadata JSON files for `root`."""
    return root / ".ow" / "run"


def logs_dir(root: Path) -> Path:
    """Directory holding detached stdout/stderr log files for `root`."""
    return root / ".ow" / "logs"


def pid_file(root: Path, label: str) -> Path:
    """Path to the metadata JSON file for `label` under `root`."""
    return run_dir(root) / f"{label}.json"


def log_file(root: Path, label: str) -> Path:
    """Path to the log file for `label` under `root`."""
    return logs_dir(root) / f"{label}.log"


def _proc_state_and_starttime(pid: int) -> tuple[str | None, str | None]:
    """Return `(state, starttime)` from `/proc/<pid>/stat`, or `(None, None)` when unavailable."""
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None, None
    parts = stat_text.rsplit(")", 1)
    if len(parts) != 2:
        return None, None
    fields = parts[1].split()
    if len(fields) <= 19:
        return None, None
    return fields[0], fields[19]


def process_starttime(pid: int) -> str | None:
    """Field 22 of `/proc/<pid>/stat` (clock ticks since boot), or None when unavailable."""
    return _proc_state_and_starttime(pid)[1]


def group_alive(pid: int) -> bool:
    """Whether `pid` or any member of its process group is still alive."""
    for probe in (os.killpg, os.kill):
        try:
            probe(pid, 0)
            return True
        except ProcessLookupError:
            continue
        except PermissionError:
            return True
        except OSError:
            continue
    return False


def is_alive(proc: RunningProcess) -> bool:
    """Whether `proc` is still running and has not exited, zombied, or had its PID recycled."""
    if proc.pid <= 0:
        return False
    # Reap if this process is a direct child of the current interpreter (e.g. in tests).
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(proc.pid, os.WNOHANG)
    state, current = _proc_state_and_starttime(proc.pid)
    if state in ("Z", "X", "x"):
        return False
    if not group_alive(proc.pid):
        return False
    if proc.starttime is not None and current is not None and current != proc.starttime:
        return False
    return True


def _load_record(path: Path) -> RunningProcess | None:
    """Parse one `.ow/run/<label>.json` file, returning None if malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    label = data.get("label") or path.stem
    log_raw = data.get("log")
    if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(label, str) or not isinstance(log_raw, str):
        return None
    starttime = data.get("starttime")
    if starttime is not None and not isinstance(starttime, str):
        starttime = None
    started_at = str(data.get("started_at") or "")
    raw_argv = data.get("argv")
    argv = tuple(str(x) for x in raw_argv) if isinstance(raw_argv, list) else ()
    return RunningProcess(
        label=label,
        pid=pid,
        starttime=starttime,
        log_path=Path(log_raw),
        started_at=started_at,
        argv=argv,
    )


def read_running(root: Path, label: str) -> RunningProcess | None:
    """Return the live `RunningProcess` for `label` under `root`, pruning stale metadata."""
    path = pid_file(root, label)
    if not path.is_file():
        return None
    proc = _load_record(path)
    if proc is None or not is_alive(proc):
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    return proc


def list_running(root: Path) -> list[RunningProcess]:
    """Return every live `RunningProcess` under `root`, pruning stale entries."""
    directory = run_dir(root)
    if not directory.is_dir():
        return []
    live: list[RunningProcess] = []
    for entry in sorted(directory.glob("*.json")):
        proc = _load_record(entry)
        if proc is None or not is_alive(proc):
            with contextlib.suppress(OSError):
                entry.unlink()
            continue
        live.append(proc)
    return live


def running_for_workspace(ws: Workspace) -> RunningProcess | None:
    """Return the live detached process for `ws`, or None."""
    return read_running(runtime_root(ws), ws.name)


def tail_lines(path: Path, n: int) -> list[str]:
    """Return the last `n` lines of `path` (without trailing newlines), or `[]` if unreadable."""
    if n <= 0 or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-n:]


def start_detached(
    root: Path,
    label: str,
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    startup_grace: float = 0.2,
) -> RunningProcess:
    """Spawn `argv` detached in its own session, record metadata, and fail fast on immediate crash."""
    lpath = log_file(root, label)
    lpath.parent.mkdir(parents=True, exist_ok=True)
    run_dir(root).mkdir(parents=True, exist_ok=True)

    full_env = dict(os.environ)
    full_env.update(env)

    try:
        with open(lpath, "wb") as log_handle:
            child = subprocess.Popen(
                list(argv),
                cwd=cwd,
                env=full_env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError as exc:
        raise UsageError(f"failed to execute {argv[0]!r}: {exc}") from exc

    deadline = time.monotonic() + startup_grace
    while time.monotonic() < deadline:
        rc = child.poll()
        if rc is not None:
            if rc != 0:
                excerpt = "\n".join(f"  {line}" for line in tail_lines(lpath, 15))
                detail = f"\n{excerpt}" if excerpt else ""
                raise UsageError(f"{label} exited immediately with code {rc} (see {lpath}){detail}")
            break
        time.sleep(0.05)

    starttime = process_starttime(child.pid)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record = RunningProcess(
        label=label,
        pid=child.pid,
        starttime=starttime,
        log_path=lpath,
        started_at=started_at,
        argv=tuple(str(tok) for tok in argv),
    )
    payload = json.dumps(
        {
            "label": record.label,
            "pid": record.pid,
            "starttime": record.starttime,
            "log": str(record.log_path),
            "started_at": record.started_at,
            "argv": list(record.argv),
        },
        indent=2,
    )
    fs.atomic_write(pid_file(root, label), (payload + "\n").encode("utf-8"), mode=0o644)
    return record


def stop_process(root: Path, proc: RunningProcess, *, timeout: float = 10.0) -> bool:
    """Gracefully terminate `proc`'s process group (SIGTERM -> SIGKILL) and remove its PID file."""
    path = pid_file(root, proc.label)
    if not is_alive(proc):
        with contextlib.suppress(OSError):
            path.unlink()
        return False

    def _send(sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(proc.pid, sig)

    _send(signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(proc):
            break
        time.sleep(0.1)

    if is_alive(proc):
        _send(signal.SIGKILL)
        kill_deadline = time.monotonic() + 2.0
        while time.monotonic() < kill_deadline:
            if not is_alive(proc):
                break
            time.sleep(0.05)

    with contextlib.suppress(OSError):
        path.unlink()
    return True


def cleanup_runtime_dirs(root: Path) -> None:
    """Remove `.ow/run` and `.ow/logs` under `root` during workspace removal."""
    for directory in (run_dir(root), logs_dir(root)):
        if directory.is_dir():
            with contextlib.suppress(OSError):
                shutil.rmtree(directory)


def _extract_timestamp_key(line: str) -> str | None:
    """Extract a sortable `YYYY-MM-DD HH:MM:SS.ffffff` timestamp from a log line, or None."""
    match = _LINE_TS_RE.search(line)
    if match is None:
        return None
    date_part, time_part, frac_part = match.groups()
    micros = (frac_part or "").ljust(6, "0")[:6]
    return f"{date_part} {time_part}.{micros}"


def _file_mtime_key(path: Path) -> str:
    """Fallback sortable UTC timestamp from a file's mtime when its lines carry no timestamps."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return "0000-00-00 00:00:00.000000"
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _build_prefixes(entries: Sequence[tuple[str, Path]]) -> dict[str, str]:
    """Build per-label line prefixes: aligned & colored on TTY, compact `[label] ` when piped."""
    if len(entries) <= 1:
        return {label: "" for label, _path in entries}
    use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    max_width = max(len(label) for label, _path in entries)
    prefixes: dict[str, str] = {}
    for idx, (label, _path) in enumerate(entries):
        if use_color:
            color = _ANSI_COLORS[idx % len(_ANSI_COLORS)]
            pad = " " * (max_width - len(label) + 1)
            prefixes[label] = f"{color}[{label}]{_ANSI_RESET}{pad}"
        else:
            prefixes[label] = f"[{label}] "
    return prefixes


def follow_files(
    entries: Sequence[tuple[str, Path]],
    *,
    lines: int = 50,
    follow: bool = False,
) -> int:
    """Interleave the last `lines` of each log file chronologically with a prefix per line, then optionally follow."""
    prefixes = _build_prefixes(entries)
    offsets: dict[str, int] = {}
    partials: dict[str, str] = {}

    initial_rows: list[tuple[str, int, int, str, str]] = []
    for file_idx, (label, path) in enumerate(entries):
        fallback_ts = _file_mtime_key(path)
        last_ts = fallback_ts
        for line_idx, line in enumerate(tail_lines(path, lines)):
            ts = _extract_timestamp_key(line)
            if ts is not None:
                last_ts = ts
            initial_rows.append((last_ts, file_idx, line_idx, label, line))
        try:
            offsets[label] = path.stat().st_size
        except OSError:
            offsets[label] = 0

    if len(entries) > 1:
        initial_rows.sort(key=lambda row: (row[0], row[1], row[2]))

    for _ts, _fidx, _lidx, label, line in initial_rows:
        sys.stdout.write(f"{prefixes[label]}{line}\n")
    sys.stdout.flush()

    if not follow:
        return 0

    try:
        while True:
            wrote = False
            for label, path in entries:
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                pos = offsets.get(label, 0)
                if size < pos:
                    pos = 0
                    partials[label] = ""
                if size == pos:
                    continue
                try:
                    with open(path, "rb") as handle:
                        handle.seek(pos)
                        chunk = handle.read()
                        offsets[label] = handle.tell()
                except OSError:
                    continue
                if chunk:
                    buf = partials.get(label, "") + chunk.decode("utf-8", errors="replace")
                    prefix = prefixes[label]
                    lines_out = buf.splitlines(keepends=True)
                    if lines_out and not lines_out[-1].endswith(("\n", "\r")):
                        partials[label] = lines_out.pop()
                    else:
                        partials[label] = ""
                    for out_line in lines_out:
                        sys.stdout.write(f"{prefix}{out_line}")
                        wrote = True
            if wrote:
                sys.stdout.flush()
            time.sleep(0.1)
    except KeyboardInterrupt:
        return 0
