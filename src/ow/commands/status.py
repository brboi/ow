"""`ow status` — workspace, task, or external target status (plain table or `--json`).

One tab-separated line per repo when piped; when stdout is a TTY, aligned columns with header.
When enabled plugins (e.g. `github`, future `runbot`) contribute external status, those are shown
inline (or in structured form under `--json`), with `--offline` to skip network calls and `--fetch`
to run a parallel surgical fetch first.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ow import term
from ow.commands._sync import fetch_then_gather
from ow.core import git, tasks
from ow.core.facts import RepoFacts, gather_all
from ow.core.json_schema import envelope
from ow.core.plugins import Plugins, StatusInfo
from ow.core.session import Session, open_session
from ow.errors import UsageError


def run(
    session: Session | None,
    *,
    plugins: Plugins | None = None,
    target: str | None = None,
    task_name: str | None = None,
    only: Sequence[str] = (),
    fetch: bool = False,
    offline: bool = False,
    json_output: bool = False,
) -> int:
    """Inspect workspace, task, or external target; returns 0 on success or 1 on fetch failure."""
    active_plugins = plugins or (session.plugins if session is not None else None)
    assert active_plugins is not None

    with term.json_mode(json_output):
        if target is not None:
            return _run_external_target(
                active_plugins, target=target, offline=offline, json_output=json_output
            )

        if task_name is not None:
            return _run_task(
                active_plugins,
                task_name=task_name,
                fetch=fetch,
                offline=offline,
                json_output=json_output,
            )

        if session is None or session.workspace is None:
            raise UsageError("not inside an ow workspace; pass -w <name|path>, -t <task>, or run `ow add`")

        exit_code = 0
        if fetch:
            _root, facts, failures = fetch_then_gather(session, only, no_fetch=False)
            if any(err is not None for err in failures.values()):
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
            payload = _workspace_json(session, facts, infos, active_plugins)
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


def _run_task(
    plugins: Plugins,
    *,
    task_name: str,
    fetch: bool,
    offline: bool,
    json_output: bool,
) -> int:
    task = tasks.find_task(task_name, plugins)
    if task is None:
        raise UsageError(f"unknown task {task_name!r}")

    exit_code = 0
    ws_payloads: list[dict[str, Any]] = []
    for ws in task.workspaces:
        sess = open_session(str(ws.root), need_workspace=True, plugins=plugins)
        if fetch:
            _root, facts, failures = fetch_then_gather(sess, (), no_fetch=False)
            if any(err is not None for err in failures.values()):
                exit_code = 1
        else:
            facts = gather_all(sess.locations)
        infos = _collect_plugin_status(plugins, ws=sess.workspace, facts=facts, target=None, offline=offline)
        if json_output:
            ws_payloads.append(_workspace_entry_json(sess, facts, infos))
        else:
            port = ws.settings.get("odoo.http_port")
            header = f"== {ws.name} ({ws.root})" + (f" [port {port}]" if isinstance(port, int) else "") + " =="
            term.out(header)
            print_table(sess, facts, infos)

    if json_output:
        code_ws = (task.files_dir / f"{task.name}.code-workspace") if task.files_dir else None
        term.emit_json(
            envelope(
                {
                    "task": {
                        "name": task.name,
                        "common_parent": str(task.common_parent) if task.common_parent else None,
                        "code_workspace": str(code_ws) if code_ws and code_ws.exists() else None,
                        "workspaces": ws_payloads,
                    }
                }
            )
        )
    return exit_code


def _collect_plugin_status(
    plugins: Plugins,
    *,
    ws,
    facts: Sequence[RepoFacts],
    target: str | None,
    offline: bool,
) -> list[StatusInfo]:
    infos: list[StatusInfo] = []
    for plugin_name, answer in plugins.call(
        "ow_status",
        ws=ws,
        facts=facts,
        target=target,
        offline=offline,
        errors="collect",
    ):
        if isinstance(answer, Exception):
            term.warn(f"{plugin_name}: {answer}")
            continue
        if isinstance(answer, list):
            infos.extend(item for item in answer if isinstance(item, StatusInfo))
    return infos


def _workspace_entry_json(
    session: Session, facts: Sequence[RepoFacts], infos: Sequence[StatusInfo]
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
        "task": ws.task,
        "http_port": ws.settings.get("odoo.http_port") if isinstance(ws.settings.get("odoo.http_port"), int) else None,
        "repos": repos_data,
        "diagnostics": [
            {"plugin": p, "level": d.level, "text": d.text}
            for p, d in session.diagnostics
        ],
    }
    if ws_plugins:
        out["plugins"] = ws_plugins
    return out


def _workspace_json(
    session: Session,
    facts: Sequence[RepoFacts],
    infos: Sequence[StatusInfo],
    plugins: Plugins,
) -> dict[str, Any]:
    assert session.workspace is not None
    ws = session.workspace
    payload: dict[str, Any] = {"workspace": _workspace_entry_json(session, facts, infos)}
    if ws.task:
        task = tasks.find_task(ws.task, plugins)
        if task is not None:
            code_ws = (task.files_dir / f"{task.name}.code-workspace") if task.files_dir else None
            payload["task"] = {
                "name": task.name,
                "common_parent": str(task.common_parent) if task.common_parent else None,
                "code_workspace": str(code_ws) if code_ws and code_ws.exists() else None,
                "siblings": [
                    {
                        "name": sibling.name,
                        "root": str(sibling.root),
                        "http_port": sibling.settings.get("odoo.http_port")
                        if isinstance(sibling.settings.get("odoo.http_port"), int)
                        else None,
                        "repos": [
                            {"name": r.name, "branch": r.branch, "base": r.base}
                            for r in sibling.repos
                        ],
                    }
                    for sibling in task.workspaces
                    if sibling.root != ws.root
                ],
            }
    return payload


def print_table(
    session: Session,
    facts: Sequence[RepoFacts],
    infos: Sequence[StatusInfo] = (),
) -> None:
    """Format and print table: aligned columns (TTY) or tab-separated (piped)."""
    base_counts_cache = _compute_base_counts_parallel(facts)
    extra_by_repo: dict[str, list[str]] = {}
    for info in infos:
        if info.scope == "repo":
            extra_by_repo.setdefault(info.target, []).append(info.summary)

    rows = [_row(f, base_counts_cache, i, extra_by_repo.get(f.name, ())) for i, f in enumerate(facts)]

    if sys.stdout.isatty():
        headers = ["name", "state", "base", "upstream", "base counts", "dirty", "operation"]
        if extra_by_repo:
            headers.append("external")
        _print_aligned(headers, rows)
    else:
        for row in rows:
            _line(row)

    for info in infos:
        if info.scope != "repo":
            term.out(f"# {info.plugin}: {info.summary}")

    for plugin, diagnostic in session.diagnostics:
        if diagnostic.level == "info":
            term.out(f"# {plugin}: {diagnostic.text}")


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
