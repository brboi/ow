"""`ow status` — workspace, multi-stack workspace, or external spec status (plain table).

One tab-separated line per repo when piped; when stdout is a TTY, aligned columns with header.
When enabled plugins (`github`, `runbot`) contribute external status, those are shown inline,
with `--offline` to skip network calls and `--fetch` to run a parallel surgical fetch first.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ow import term
from ow.commands._runtime import open_target
from ow.commands._shared import known_repo_catalog
from ow.commands._workspace import stack_info
from ow.core import git, runtime
from ow.core.facts import RepoFacts, gather_all
from ow.core.plugins import Plugins, StatusInfo
from ow.core.session import Session, global_settings
from ow.errors import UsageError
from ow.recipes.fetch import fetch_then_gather


def run(
    session: Session,
    *,
    only: Sequence[str] = (),
    fetch: bool = False,
    offline: bool = False,
) -> int:
    """Print the status of the session's mono-stack workspace or stack."""
    if session.workspace is None:
        raise UsageError("not inside an ow workspace; pass -w <name|path> or run `ow init`")

    exit_code = 0
    if fetch:
        _root, facts, failures = fetch_then_gather(session, only, no_fetch=False, hint=None)
        if any(err is not None for err in failures.values()):
            exit_code = 1
    else:
        locations = tuple(loc for loc in session.locations if not only or loc.name in only)
        facts = gather_all(locations)

    infos = _collect_plugin_status(
        session.plugins, ws=session.workspace, facts=facts, spec=None, offline=offline
    )
    print_table(session, facts, infos)
    return exit_code


def run_spec(session: Session, *, spec: str, offline: bool) -> int:
    """Print what the plugins say about an external SPEC (`enterprise#12345`, a Runbot URL)."""
    infos = _collect_plugin_status(session.plugins, ws=None, facts=(), spec=spec, offline=offline)
    if not infos:
        if offline:
            term.error(f"offline mode prevents inspecting {spec!r}; drop --offline to look it up")
        else:
            term.error(f"no plugin could inspect status for {spec!r}")
        return 1
    for info in infos:
        term.out(f"{info.plugin}\t{info.subject}\t{info.summary}")
    return 0


def _dedupe_services(infos: Sequence[StatusInfo]) -> list[StatusInfo]:
    """Keep one StatusInfo per `(plugin, subject)` among `scope == 'service'` entries."""
    seen: set[tuple[str, str]] = set()
    result: list[StatusInfo] = []
    for info in infos:
        if info.scope != "service":
            continue
        key = (info.plugin, info.subject)
        if key not in seen:
            seen.add(key)
            result.append(info)
    return result


def _stack_note_with_runtime(ws, plugins: Plugins) -> str | None:
    """Combine plugin `StackInfo.note` with live detached process PID when running."""
    note = stack_info(ws, plugins).note
    proc = runtime.running_for_workspace(ws)
    if proc is None:
        return note
    running_tag = f"running (pid {proc.pid})"
    return f"{note} · {running_tag}" if note else running_tag


def run_multi(
    plugins: Plugins,
    *,
    multi_root: Path,
    fetch: bool = False,
    offline: bool = False,
) -> int:
    """Print every stack of the multi-stack workspace `multi_root`, then its services and plugins."""
    target = open_target(multi_root, "multi", plugins)
    multi = target.multi
    assert multi is not None
    eff_plugins = target.plugins

    exit_code = 0
    gathered: list[tuple[Session, tuple[RepoFacts, ...], list[StatusInfo], str | None]] = []
    all_services: list[StatusInfo] = []

    for sess in target.sessions:
        assert sess.workspace is not None
        ws = sess.workspace
        if fetch:
            _root, facts, failures = fetch_then_gather(sess, (), no_fetch=False, hint=None)
            if any(err is not None for err in failures.values()):
                exit_code = 1
        else:
            facts = gather_all(sess.locations)
        infos = _collect_plugin_status(eff_plugins, ws=ws, facts=facts, spec=None, offline=offline)
        all_services.extend(i for i in infos if i.scope == "service")
        non_service = [i for i in infos if i.scope != "service"]
        gathered.append((sess, facts, non_service, _stack_note_with_runtime(ws, eff_plugins)))

    services = _dedupe_services(all_services)
    external_plugins = eff_plugins.external_plugins

    if sys.stdout.isatty():
        sections = [
            _build_tty_section(sess.workspace.name, note, sess, facts, non_service)
            for sess, facts, non_service, note in gathered
            if sess.workspace is not None
        ]
        _print_tty_sections(
            sections,
            services,
            external_plugins=external_plugins,
            header=(multi.name, _display_path(multi.root)),
        )
    else:
        for sess, facts, non_service, note in gathered:
            assert sess.workspace is not None
            ws = sess.workspace
            port_str = f" [{note}]" if note else ""
            term.out(f"== {ws.name} ({ws.root}){port_str} ==")
            print_table(sess, facts, non_service, show_external_plugins=False)
        for svc in services:
            term.out(f"# {svc.plugin}: {svc.summary}")
        for name, path in external_plugins:
            term.out(f"# plugin: {name} ({_display_path(path)})")
    return exit_code


def _collect_plugin_status(
    plugins: Plugins,
    *,
    ws,
    facts: Sequence[RepoFacts],
    spec: str | None,
    offline: bool,
) -> list[StatusInfo]:
    catalog = known_repo_catalog(plugins) if spec is not None else {}
    settings = global_settings(plugins) if spec is not None else (ws.settings if ws is not None else {})
    infos: list[StatusInfo] = []
    for plugin_name, answer in plugins.call(
        "ow_status",
        ws=ws,
        facts=facts,
        spec=spec,
        offline=offline,
        known_repos=catalog,
        settings=settings,
        errors="collect",
    ):
        if isinstance(answer, Exception):
            term.warn(f"{plugin_name}: {answer}")
            continue
        if isinstance(answer, list):
            infos.extend(item for item in answer if isinstance(item, StatusInfo))
    return infos


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_AHEAD_BEHIND_RE = re.compile(r"^ahead (\d+), behind (\d+)$")


def _use_color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _ansi(text: str, code: str) -> str:
    if not text or not _use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def _visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def _pad(text: str, width: int) -> str:
    extra = width - _visible_len(text)
    return text + (" " * extra if extra > 0 else "")


def _display_path(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


@dataclass(frozen=True)
class _TtySection:
    title: str
    note: str | None
    rows: tuple[dict[str, str], ...]
    notes: tuple[StatusInfo, ...]
    diagnostics: tuple[tuple[str, str], ...]


def _arrows(ahead: int, behind: int) -> str:
    """`↑2 ↓1` in green and yellow, omitting a zero side."""
    parts: list[str] = []
    if ahead:
        parts.append(_ansi(f"↑{ahead}", "32"))
    if behind:
        parts.append(_ansi(f"↓{behind}", "33"))
    return " ".join(parts)


def _external_by_repo(infos: Sequence[StatusInfo]) -> dict[str, list[str]]:
    """The `scope == 'repo'` summaries, grouped by repo name."""
    result: dict[str, list[str]] = {}
    for info in infos:
        if info.scope == "repo":
            result.setdefault(info.subject, []).append(info.summary)
    return result


def _format_tty_delta(raw: str) -> str:
    if raw == "(even)":
        return _ansi("=", "2")
    match = _AHEAD_BEHIND_RE.match(raw)
    if match:
        return _arrows(int(match.group(1)), int(match.group(2))) or _ansi("=", "2")
    if raw == "(?)":
        return _ansi("?", "2")
    return _ansi(raw, "2")


def _format_tty_upstream(facts: RepoFacts) -> str:
    upstream = facts.upstream
    if upstream is None or upstream == facts.base:
        return ""
    if facts.upstream_ab is None:
        return _ansi(upstream, "36")
    suffix = _arrows(*facts.upstream_ab)
    return f"{_ansi(upstream, '36')} {suffix}" if suffix else _ansi(upstream, "36")


def _format_tty_external(entries: Sequence[str]) -> str:
    if not entries:
        return ""
    styled: list[str] = []
    for text in entries:
        lower = text.lower()
        if "merged" in lower:
            styled.append(_ansi(text, "36"))
        elif "closed" in lower or "changes_requested" in lower:
            styled.append(_ansi(text, "33"))
        elif "approved" in lower or "open" in lower:
            styled.append(_ansi(text, "32"))
        else:
            styled.append(text)
    return ", ".join(styled)


def _format_tty_note_summary(summary: str) -> str:
    lower = summary.lower()
    if "done ko" in lower or "failed" in lower or "error" in lower:
        return _ansi(summary, "31")
    if "done ok" in lower or "running" in lower:
        return _ansi(summary, "32")
    if "pending" in lower or "testing" in lower:
        return _ansi(summary, "33")
    return summary


def _format_tty_port_note(note: str | None) -> str | None:
    if not note:
        return None
    if note.startswith("port ") and note[5:].isdigit():
        return f":{note[5:]}"
    return note


def _build_tty_section(
    title: str,
    note: str | None,
    session: Session,
    facts: Sequence[RepoFacts],
    infos: Sequence[StatusInfo],
) -> _TtySection:
    counts = _compute_base_counts_parallel(facts)
    external = _external_by_repo(infos)
    ws_notes = [info for info in infos if info.scope not in ("repo", "service")]

    rows: list[dict[str, str]] = []
    for f, delta in zip(facts, counts, strict=True):
        if f.problem:
            state_cell = _ansi(f"({f.problem} worktree)", "31")
        elif f.branch:
            state_cell = f.branch
        elif f.head:
            state_cell = _ansi(f"detached {f.head[:10]}", "2")
        else:
            state_cell = _ansi("detached", "2")

        if f.base is None:
            base_cell = _ansi("no base", "2")
        elif f.explicit_base is None and f.default_base is not None:
            base_cell = _ansi(f.base, "2")
        else:
            base_cell = f.base

        dirty_count = len(f.modified) + len(f.untracked)
        rows.append(
            {
                "name": _ansi(f.name, "1"),
                "state": state_cell,
                "base": base_cell,
                "upstream": _format_tty_upstream(f),
                "delta": _format_tty_delta(delta),
                "dirty": _ansi(f"{dirty_count} dirty", "33") if dirty_count else "",
                "operation": _ansi(f.operation, "31") if f.operation else "",
                "external": _format_tty_external(external.get(f.name, ())),
            }
        )

    info_diags = tuple(
        (plugin, diag.text)
        for plugin, diag in session.diagnostics
        if diag.level == "info"
    )
    return _TtySection(
        title=title,
        note=_format_tty_port_note(note),
        rows=tuple(rows),
        notes=tuple(ws_notes),
        diagnostics=info_diags,
    )


def _print_tty_sections(
    sections: Sequence[_TtySection],
    services: Sequence[StatusInfo],
    *,
    external_plugins: Sequence[tuple[str, Path]] = (),
    header: tuple[str, str] | None = None,
) -> None:
    """Render aligned, airy sections for TTY output with global column widths and one services footer."""
    all_rows = [row for sec in sections for row in sec.rows]
    columns = ["name", "state", "base", "delta"]
    for opt in ("upstream", "dirty", "operation", "external"):
        if any(row.get(opt) for row in all_rows):
            columns.append(opt)

    widths: dict[str, int] = {col: 0 for col in columns}
    for row in all_rows:
        for col in columns:
            widths[col] = max(widths[col], _visible_len(row.get(col, "")))

    first_block = True
    if header is not None:
        name, path_display = header
        term.out(f"{_ansi(name, '1')}  {_ansi(path_display, '2')}")
        first_block = False

    for sec in sections:
        if not first_block:
            term.out("")
        first_block = False
        if sec.title:
            title_line = _ansi(sec.title, "1")
            if sec.note:
                title_line += f"  {_ansi(sec.note, '2')}"
            term.out(title_line)
        indent = "  " if sec.title else ""
        for row in sec.rows:
            last_non_empty = len(columns) - 1
            while last_non_empty > 0 and not row.get(columns[last_non_empty]):
                last_non_empty -= 1
            parts: list[str] = []
            for idx in range(last_non_empty + 1):
                val = row.get(columns[idx], "")
                parts.append(_pad(val, widths[columns[idx]]) if idx < last_non_empty else val)
            term.out(f"{indent}{'  '.join(parts)}")
        for info in sec.notes:
            term.out(f"{indent}{_ansi(info.plugin + ':', '2')} {_format_tty_note_summary(info.summary)}")
        for plugin, text in sec.diagnostics:
            term.out(f"{indent}{_ansi(plugin + ':', '2')} {text}")

    if services:
        if not first_block:
            term.out("")
        first_block = False
        term.out(_ansi("services", "1"))
        svc_width = max(len(svc.plugin) for svc in services)
        for svc in services:
            label = _ansi(svc.plugin.ljust(svc_width), "36")
            term.out(f"  {label}  {svc.summary}")

    if external_plugins:
        if not first_block:
            term.out("")
        term.out(_ansi("plugins", "1"))
        plug_width = max(len(name) for name, _p in external_plugins)
        for name, path in external_plugins:
            label = _ansi(name.ljust(plug_width), "36")
            term.out(f"  {label}  {_ansi(_display_path(path), '2')}")


def print_table(
    session: Session,
    facts: Sequence[RepoFacts],
    infos: Sequence[StatusInfo] = (),
    *,
    show_external_plugins: bool = True,
) -> None:
    """Format and print table: clean TTY sections/columns or tab-separated (piped)."""
    external_plugins = session.plugins.external_plugins if show_external_plugins else ()
    if sys.stdout.isatty():
        services = _dedupe_services(infos)
        non_service = [i for i in infos if i.scope != "service"]
        ws = session.workspace
        note = _stack_note_with_runtime(ws, session.plugins) if ws is not None else None
        title = ws.name if ws is not None else ""
        section = _build_tty_section(title, note, session, facts, non_service)
        _print_tty_sections([section], services, external_plugins=external_plugins)
        return

    counts = _compute_base_counts_parallel(facts)
    external = _external_by_repo(infos)
    for f, delta in zip(facts, counts, strict=True):
        term.out(_row(f, delta, external.get(f.name, ())))

    if session.workspace is not None:
        proc = runtime.running_for_workspace(session.workspace)
        if proc is not None:
            term.out(f"# running: pid {proc.pid} (logs: {_display_path(proc.log_path)})")

    for info in infos:
        if info.scope != "repo":
            term.out(f"# {info.plugin}: {info.summary}")

    for plugin, diagnostic in session.diagnostics:
        if diagnostic.level == "info":
            term.out(f"# {plugin}: {diagnostic.text}")

    for name, path in external_plugins:
        term.out(f"# plugin: {name} ({_display_path(path)})")


def _compute_base_counts_parallel(facts: Sequence[RepoFacts]) -> list[str]:
    """The base counts of every repo, computed in parallel, in order."""
    with ThreadPoolExecutor() as executor:
        return list(executor.map(_compute_base_counts_for_fact, facts))


def _compute_base_counts_for_fact(facts: RepoFacts) -> str:
    """`ahead X, behind Y` of HEAD vs base; `(even)` at 0/0; `no base` when none; `(?)` on error."""
    if facts.base is None:
        return "no base"
    if facts.problem:
        return "(?)"

    result = git.run(
        [
            "git",
            "-C",
            str(facts.path),
            "rev-list",
            "--left-right",
            "--count",
            f"{facts.base}...HEAD",
        ]
    )
    if result.returncode != 0:
        return "(?)"

    parts = result.stdout.split()
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return "(?)"
    if ahead == 0 and behind == 0:
        return "(even)"
    return f"ahead {ahead}, behind {behind}"


def _row(facts: RepoFacts, delta: str, external: Sequence[str] = ()) -> str:
    """One repo's fields, tab-separated."""
    fields = [facts.name, _state(facts), _base(facts), _upstream(facts), delta]
    dirty = len(facts.modified) + len(facts.untracked)
    has_ext = bool(external)
    if dirty:
        fields.append(f"dirty {dirty}")
    elif facts.operation or has_ext:
        fields.append("")
    if facts.operation:
        fields.append(f"{facts.operation} in progress")
    elif has_ext:
        fields.append("")
    if has_ext:
        fields.append(", ".join(external))
    return "\t".join(fields)


def _state(facts: RepoFacts) -> str:
    if facts.problem:
        return f"({facts.problem} worktree)"
    if facts.branch:
        return facts.branch
    if facts.head:
        return f"detached at {facts.head[:10]}"
    return "detached"


def _base(facts: RepoFacts) -> str:
    if facts.base is None:
        return "no base"
    if facts.explicit_base is None and facts.default_base is not None:
        return f"{facts.base} (default)"
    return facts.base


def _upstream(facts: RepoFacts) -> str:
    upstream = facts.upstream
    if upstream is None or upstream == facts.base:
        return "(none)"
    if facts.upstream_ab is None:
        return upstream
    ahead, behind = facts.upstream_ab
    return f"{upstream} (ahead {ahead}, behind {behind})"
