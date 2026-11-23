"""`ow status` — workspace, multi-stack workspace, or external target status (plain table or `--json`).

One tab-separated line per repo when piped; when stdout is a TTY, aligned columns with header.
When enabled plugins (`github`, `runbot`) contribute external status, those are shown inline
(or in structured form under `--json`), with `--offline` to skip network calls and `--fetch`
to run a parallel surgical fetch first.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ow import term
from ow.commands._shared import known_repo_catalog
from ow.commands._workspace import stack_info
from ow.core import git, refresh, stacks
from ow.core.facts import RepoFacts, gather_all
from ow.core.json_schema import envelope
from ow.core.plugins import Plugins, StatusInfo
from ow.core.session import Session, global_settings
from ow.errors import UsageError
from ow.recipes.fetch import fetch_then_gather


def run(
    session: Session | None,
    *,
    plugins: Plugins | None = None,
    target: str | None = None,
    multi_root: Path | None = None,
    only: Sequence[str] = (),
    fetch: bool = False,
    offline: bool = False,
    json_output: bool = False,
) -> int:
    """Inspect mono-stack workspace, multi-stack workspace, or external target."""
    active_plugins = (session.plugins if session is not None else None) or plugins
    assert active_plugins is not None

    with term.json_mode(json_output):
        if target is not None:
            return _run_external_target(
                active_plugins, target=target, offline=offline, json_output=json_output
            )

        if multi_root is not None:
            return _run_multi(
                active_plugins,
                multi_root=multi_root,
                fetch=fetch,
                offline=offline,
                json_output=json_output,
            )

        if session is None or session.workspace is None:
            raise UsageError("not inside an ow workspace; pass -w <name|path> or run `ow init`")

        exit_code = 0
        fetch_failures: list[str] = []
        if fetch:
            _root, facts, failures = fetch_then_gather(session, only, no_fetch=False, hint=None)
            fetch_failures = [name for name, err in failures.items() if err is not None]
            if fetch_failures:
                exit_code = 1
        else:
            locations = tuple(
                loc for loc in session.locations if not only or loc.name in only
            )
            facts = gather_all(locations)

        infos = _collect_plugin_status(
            active_plugins, ws=session.workspace, facts=facts, target=None, offline=offline
        )

        if json_output:
            payload = _workspace_json(
                session, facts, infos, active_plugins, fetch_failures=fetch_failures
            )
            term.emit_json(envelope(payload))
        else:
            print_table(session, facts, infos)
        return exit_code


def _run_external_target(
    plugins: Plugins, *, target: str, offline: bool, json_output: bool
) -> int:
    infos = _collect_plugin_status(plugins, ws=None, facts=(), target=target, offline=offline)
    if json_output:
        term.emit_json(
            envelope(
                {
                    "target": target,
                    "offline": offline,
                    "results": [
                        {
                            "plugin": info.plugin,
                            "scope": info.scope,
                            "target": info.target,
                            "summary": info.summary,
                            "data": dict(info.data),
                        }
                        for info in infos
                    ],
                }
            )
        )
        return 0 if infos else 1

    if not infos:
        term.error(f"no plugin could inspect status for target {target!r}")
        return 1
    for info in infos:
        _line(f"{info.plugin}\t{info.target}\t{info.summary}")
    return 0


def _code_workspace_path(multi: stacks.MultiWorkspace) -> str | None:
    """Locate an existing `.code-workspace` file owned by `multi` (or in `multi.root`)."""
    for path in refresh.owned_files(multi.root):
        if path.suffix == ".code-workspace":
            return str(path)
    fallback = multi.root / f"{multi.name}.code-workspace"
    if fallback.exists():
        return str(fallback)
    return None


def _dedupe_services(infos: Sequence[StatusInfo]) -> list[StatusInfo]:
    """Keep one StatusInfo per `(plugin, target)` among `scope == 'service'` entries."""
    seen: set[tuple[str, str]] = set()
    result: list[StatusInfo] = []
    for info in infos:
        if info.scope != "service":
            continue
        key = (info.plugin, info.target)
        if key not in seen:
            seen.add(key)
            result.append(info)
    return result


def _run_multi(
    plugins: Plugins,
    *,
    multi_root: Path,
    fetch: bool,
    offline: bool,
    json_output: bool,
) -> int:
    loaded = stacks.load_multi_workspace(multi_root, plugins)
    if loaded is None:
        raise UsageError(f"no valid stacks found in {multi_root}")
    multi, sessions = loaded
    eff_plugins = sessions[0].plugins if sessions else plugins

    exit_code = 0
    stack_payloads: list[dict[str, Any]] = []
    gathered: list[tuple[Session, tuple[RepoFacts, ...], list[StatusInfo], str | None]] = []
    all_services: list[StatusInfo] = []

    for sess in sessions:
        assert sess.workspace is not None
        ws = sess.workspace
        for warning in sess.warnings:
            term.warn(warning)
        fetch_failures: list[str] = []
        if fetch:
            _root, facts, failures = fetch_then_gather(sess, (), no_fetch=False, hint=None)
            fetch_failures = [name for name, err in failures.items() if err is not None]
            if fetch_failures:
                exit_code = 1
        else:
            facts = gather_all(sess.locations)
        infos = _collect_plugin_status(eff_plugins, ws=ws, facts=facts, target=None, offline=offline)
        all_services.extend(i for i in infos if i.scope == "service")
        non_service = [i for i in infos if i.scope != "service"]
        if json_output:
            stack_payloads.append(
                _workspace_entry_json(sess, facts, non_service, fetch_failures=fetch_failures)
            )
        else:
            note = stack_info(ws, eff_plugins).note
            gathered.append((sess, facts, non_service, note))

    services = _dedupe_services(all_services)
    external_plugins = eff_plugins.external_plugins

    if json_output:
        ws_payload: dict[str, Any] = {
            "name": multi.name,
            "root": str(multi.root),
            "multi_stack": True,
            "code_workspace": _code_workspace_path(multi),
            "stacks": stack_payloads,
        }
        if services:
            ws_payload["services"] = {
                svc.plugin: {"summary": svc.summary, "data": dict(svc.data)}
                for svc in services
            }
        if external_plugins:
            ws_payload["external_plugins"] = [
                {"name": name, "path": str(path)} for name, path in external_plugins
            ]
        term.emit_json(envelope({"workspace": ws_payload}))
        return exit_code

    if sys.stdout.isatty():
        sections = [
            _build_tty_section(
                sess.workspace.name,
                note,
                sess,
                facts,
                non_service,
            )
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
    target: str | None,
    offline: bool,
) -> list[StatusInfo]:
    catalog = known_repo_catalog(plugins) if target is not None else {}
    settings = global_settings(plugins) if target is not None else (ws.settings if ws is not None else {})
    infos: list[StatusInfo] = []
    for plugin_name, answer in plugins.call(
        "ow_status",
        ws=ws,
        facts=facts,
        target=target,
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


def _workspace_entry_json(
    session: Session,
    facts: Sequence[RepoFacts],
    infos: Sequence[StatusInfo],
    *,
    fetch_failures: Sequence[str] = (),
) -> dict[str, Any]:
    assert session.workspace is not None
    ws = session.workspace
    base_counts_cache = _compute_base_counts_parallel(facts)
    repo_plugins: dict[str, dict[str, Any]] = {}
    ws_plugins: dict[str, Any] = {}
    for info in infos:
        if info.scope == "repo":
            repo_plugins.setdefault(info.target, {})[info.plugin] = {
                "summary": info.summary,
                "data": dict(info.data),
            }
        else:
            ws_plugins[info.plugin] = {
                "summary": info.summary,
                "data": dict(info.data),
            }

    repos_data = []
    for i, f in enumerate(facts):
        entry: dict[str, Any] = {
            "name": f.name,
            "path": str(f.path),
            "branch": f.branch,
            "head": f.head,
            "base": f.base,
            "upstream": f.upstream,
            "upstream_ab": list(f.upstream_ab) if f.upstream_ab is not None else None,
            "base_counts": base_counts_cache.get(i, "(?)"),
            "modified": list(f.modified),
            "untracked": list(f.untracked),
            "dirty": bool(f.modified or f.untracked),
            "operation": f.operation,
            "problem": f.problem,
        }
        if f.name in repo_plugins:
            entry["plugins"] = repo_plugins[f.name]
        repos_data.append(entry)

    out: dict[str, Any] = {
        "name": ws.name,
        "root": str(ws.root),
        "is_stack": ws.is_stack,
        "ports": dict(stack_info(ws, session.plugins).ports),
        "repos": repos_data,
        "diagnostics": [
            {"plugin": p, "level": d.level, "text": d.text}
            for p, d in session.diagnostics
        ],
    }
    if fetch_failures:
        out["fetch_failures"] = list(fetch_failures)
    if ws_plugins:
        out["plugins"] = ws_plugins
    return out


def _workspace_json(
    session: Session,
    facts: Sequence[RepoFacts],
    infos: Sequence[StatusInfo],
    plugins: Plugins,
    *,
    fetch_failures: Sequence[str] = (),
) -> dict[str, Any]:
    assert session.workspace is not None
    ws = session.workspace
    ws_entry = _workspace_entry_json(session, facts, infos, fetch_failures=fetch_failures)
    if plugins.external_plugins:
        ws_entry["external_plugins"] = [
            {"name": name, "path": str(path)} for name, path in plugins.external_plugins
        ]
    payload: dict[str, Any] = {"workspace": ws_entry}
    if ws.is_stack:
        parent_root = stacks.parent_multi_workspace(ws.root)
        if parent_root is not None:
            loaded = stacks.load_multi_workspace(parent_root, plugins)
            if loaded is not None:
                multi = loaded[0]
                payload["parent_workspace"] = {
                    "name": multi.name,
                    "root": str(multi.root),
                    "code_workspace": _code_workspace_path(multi),
                    "siblings": [
                        {
                            "name": sibling.name,
                            "root": str(sibling.root),
                            "ports": dict(stack_info(sibling, plugins).ports),
                            "repos": [
                                {"name": r.name, "branch": r.branch, "base": r.base}
                                for r in sibling.repos
                            ],
                        }
                        for sibling in multi.stacks
                        if sibling.root != ws.root
                    ],
                }
    return payload


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


def _format_tty_delta(raw: str) -> str:
    if raw == "(even)":
        return _ansi("=", "2")
    match = _AHEAD_BEHIND_RE.match(raw)
    if match:
        ahead, behind = int(match.group(1)), int(match.group(2))
        parts: list[str] = []
        if ahead:
            parts.append(_ansi(f"↑{ahead}", "32"))
        if behind:
            parts.append(_ansi(f"↓{behind}", "33"))
        return " ".join(parts) if parts else _ansi("=", "2")
    if raw == "(?)":
        return _ansi("?", "2")
    return _ansi(raw, "2")


def _format_tty_upstream(facts: RepoFacts) -> str:
    upstream = facts.upstream
    if upstream is None or upstream == facts.base:
        return ""
    if facts.upstream_ab is None:
        return _ansi(upstream, "36")
    ahead, behind = facts.upstream_ab
    parts: list[str] = []
    if ahead:
        parts.append(_ansi(f"↑{ahead}", "32"))
    if behind:
        parts.append(_ansi(f"↓{behind}", "33"))
    suffix = f" {' '.join(parts)}" if parts else ""
    return f"{_ansi(upstream, '36')}{suffix}"


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
    base_counts_cache = _compute_base_counts_parallel(facts)
    extra_by_repo: dict[str, list[str]] = {}
    ws_notes: list[StatusInfo] = []
    for info in infos:
        if info.scope == "repo":
            extra_by_repo.setdefault(info.target, []).append(info.summary)
        elif info.scope != "service":
            ws_notes.append(info)

    rows: list[dict[str, str]] = []
    for i, f in enumerate(facts):
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
                "delta": _format_tty_delta(base_counts_cache.get(i, "(?)")),
                "dirty": _ansi(f"{dirty_count} dirty", "33") if dirty_count else "",
                "operation": _ansi(f.operation, "31") if f.operation else "",
                "external": _format_tty_external(extra_by_repo.get(f.name, ())),
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
        _line(f"{_ansi(name, '1')}  {_ansi(path_display, '2')}")
        first_block = False

    for sec in sections:
        if not first_block:
            _line("")
        first_block = False
        if sec.title:
            title_line = _ansi(sec.title, "1")
            if sec.note:
                title_line += f"  {_ansi(sec.note, '2')}"
            _line(title_line)
        indent = "  " if sec.title else ""
        for row in sec.rows:
            last_non_empty = len(columns) - 1
            while last_non_empty > 0 and not row.get(columns[last_non_empty]):
                last_non_empty -= 1
            parts: list[str] = []
            for idx in range(last_non_empty + 1):
                val = row.get(columns[idx], "")
                parts.append(_pad(val, widths[columns[idx]]) if idx < last_non_empty else val)
            _line(f"{indent}{'  '.join(parts)}")
        for info in sec.notes:
            _line(
                f"{indent}{_ansi(info.plugin + ':', '2')} {_format_tty_note_summary(info.summary)}"
            )
        for plugin, text in sec.diagnostics:
            _line(f"{indent}{_ansi(plugin + ':', '2')} {text}")

    if services:
        if not first_block:
            _line("")
        first_block = False
        _line(_ansi("services", "1"))
        svc_width = max(len(svc.plugin) for svc in services)
        for svc in services:
            label = _ansi(svc.plugin.ljust(svc_width), "36")
            _line(f"  {label}  {svc.summary}")

    if external_plugins:
        if not first_block:
            _line("")
        _line(_ansi("plugins", "1"))
        plug_width = max(len(name) for name, _p in external_plugins)
        for name, path in external_plugins:
            label = _ansi(name.ljust(plug_width), "36")
            _line(f"  {label}  {_ansi(_display_path(path), '2')}")


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
        note = stack_info(ws, session.plugins).note if ws is not None else None
        title = ws.name if ws is not None else ""
        section = _build_tty_section(title, note, session, facts, non_service)
        _print_tty_sections([section], services, external_plugins=external_plugins)
        return

    base_counts_cache = _compute_base_counts_parallel(facts)
    extra_by_repo: dict[str, list[str]] = {}
    for info in infos:
        if info.scope == "repo":
            extra_by_repo.setdefault(info.target, []).append(info.summary)

    rows = [_row(f, base_counts_cache, i, extra_by_repo.get(f.name, ())) for i, f in enumerate(facts)]
    for row in rows:
        _line(row)

    for info in infos:
        if info.scope != "repo":
            term.out(f"# {info.plugin}: {info.summary}")

    for plugin, diagnostic in session.diagnostics:
        if diagnostic.level == "info":
            term.out(f"# {plugin}: {diagnostic.text}")

    for name, path in external_plugins:
        term.out(f"# plugin: {name} ({_display_path(path)})")


def _compute_base_counts_parallel(facts: Sequence[RepoFacts]) -> dict[int, str]:
    """Compute base counts for all repos in parallel using ThreadPoolExecutor."""
    cache_map: dict[int, str] = {}
    if not facts:
        return cache_map

    with ThreadPoolExecutor() as executor:
        futures = {
            executor.submit(_compute_base_counts_for_fact, fact): i
            for i, fact in enumerate(facts)
        }
        for future in futures:
            i = futures[future]
            cache_map[i] = future.result()

    return cache_map


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


def _print_aligned(headers: Sequence[str], rows: Sequence[str]) -> None:
    """Print aligned columns for TTY output."""
    if not rows:
        return

    parsed_rows = [row.split("\t") for row in rows]
    col_widths = []
    for i in range(len(headers)):
        max_width = len(headers[i])
        for parsed_row in parsed_rows:
            if i < len(parsed_row):
                max_width = max(max_width, len(parsed_row[i]))
        col_widths.append(max_width)

    header_line = "  ".join(headers[i].ljust(col_widths[i]) for i in range(len(headers)))
    _line(header_line)

    for parsed_row in parsed_rows:
        padded_fields = []
        for i, field_val in enumerate(parsed_row):
            if i < len(col_widths) - 1:
                padded_fields.append(field_val.ljust(col_widths[i]))
            else:
                padded_fields.append(field_val)
        _line("  ".join(padded_fields))


def _row(
    facts: RepoFacts,
    base_counts_cache: dict[int, str],
    index: int,
    external: Sequence[str] = (),
) -> str:
    """One repo's fields, tab-separated."""
    fields = [
        facts.name,
        _state(facts),
        _base(facts),
        _upstream(facts),
        base_counts_cache.get(index, "(??)"),
    ]
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


def _line(text: str) -> None:
    sys.stdout.write(f"{text}\n")
