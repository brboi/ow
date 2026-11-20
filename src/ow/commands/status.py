"""`ow status` — workspace, multi-stack workspace, or external target status (plain table or `--json`).

One tab-separated line per repo when piped; when stdout is a TTY, aligned columns with header.
When enabled plugins (`github`, `runbot`) contribute external status, those are shown inline
(or in structured form under `--json`), with `--offline` to skip network calls and `--fetch`
to run a parallel surgical fetch first.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
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
    active_plugins = plugins or (session.plugins if session is not None else None)
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

    exit_code = 0
    stack_payloads: list[dict[str, Any]] = []
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
        infos = _collect_plugin_status(plugins, ws=ws, facts=facts, target=None, offline=offline)
        if json_output:
            stack_payloads.append(_workspace_entry_json(sess, facts, infos, fetch_failures=fetch_failures))
        else:
            note = stack_info(ws, plugins).note
            port_str = f" [{note}]" if note else ""
            term.out(f"== {ws.name} ({ws.root}){port_str} ==")
            print_table(sess, facts, infos)

    if json_output:
        term.emit_json(
            envelope(
                {
                    "workspace": {
                        "name": multi.name,
                        "root": str(multi.root),
                        "multi_stack": True,
                        "code_workspace": _code_workspace_path(multi),
                        "stacks": stack_payloads,
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
    payload: dict[str, Any] = {
        "workspace": _workspace_entry_json(session, facts, infos, fetch_failures=fetch_failures)
    }
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
