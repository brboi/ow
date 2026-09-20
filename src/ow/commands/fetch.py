import sys
from dataclasses import replace
from pathlib import Path

from rich.markup import escape
from rich.text import Text

from ow.utils import paths
from ow.utils.config import (
    Config,
    WorkspaceConfig,
    report_pending_migration,
    select_aliases,
)
from ow.utils.display import console, err_console
from ow.utils.git import count_commits, resolve_spec, rev_parse
from ow.utils.refs import FetchOutcome
from ow.utils.refs import fetch_workspace_refs
from ow.utils.resolver import resolve_workspace


def _arrival(bare_repo: Path, ref: str, before: str | None) -> str:
    """What this fetch brought to one ref, as one column."""
    after = rev_parse(bare_repo, f"refs/remotes/{ref}")
    if after is None:
        return "[red]missing[/]"
    if before is None:
        return "new"
    if before == after:
        return "[dim]up to date[/]"

    ahead = count_commits(bare_repo, f"{before}..{after}")
    dropped = count_commits(bare_repo, f"{after}..{before}")
    if dropped:
        # The ref no longer contains what it did: a force-push, which is
        # worth naming here because it is what `ow rebase` will have to
        # replay around and what `ow reset -f` exists to adopt.
        return f"[yellow]force-pushed[/] +{ahead} -{dropped}"
    return f"+{ahead}"


def _rows(ws: WorkspaceConfig, ws_dir: Path, outcome: FetchOutcome) -> list[tuple[str, str, str]]:
    """One row per fetched ref: (alias, ref, what arrived).

    A repo follows its base ref and, when its branch is pushed somewhere,
    its upstream — two refs, two rows, because a fetch moves them
    independently and averaging them into one line hides which moved.
    """
    rows: list[tuple[str, str, str]] = []
    for alias in ws.repos:
        bare_repo = paths.repos_dir() / f"{alias}.git"
        if not (ws_dir / alias).exists():
            rows.append((alias, "", "[yellow]skipped[/] — worktree not found"))
            continue
        if alias in outcome.failed:
            rows.append((alias, "", "[red]fetch failed[/]"))
            continue

        refs = [outcome.tracks[alias]] if alias in outcome.tracks else []
        upstream = outcome.upstreams.get(alias)
        if upstream and upstream not in refs:
            refs.append(upstream)

        before = {
            outcome.tracks.get(alias, ""): outcome.track_before.get(alias),
            upstream or "": outcome.upstream_before.get(alias),
        }
        for ref in refs:
            rows.append((alias, ref, _arrival(bare_repo, ref, before.get(ref))))
    return rows


def _display(ws_name: str, rows: list[tuple[str, str, str]]) -> None:
    console.print(Text(f"[{ws_name}] fetch", style="bold cyan"))
    alias_width = max((len(a) for a, _, _ in rows), default=0)
    ref_width = max((len(r) for _, r, _ in rows), default=0)
    seen: set[str] = set()
    for alias, ref, state in rows:
        # A repo with two refs prints its name once: the second row is the
        # same repo, and repeating the alias reads as a second repo.
        shown = "" if alias in seen else alias
        seen.add(alias)
        console.print(f"  {shown.ljust(alias_width)}  {escape(ref.ljust(ref_width))}  {state}")


def cmd_fetch(config: Config, workspace: str | None = None, *, only: str | None = None) -> None:
    """Refresh the refs a workspace follows, and say what arrived.

    The network half of `ow pull` and `ow rebase`, on its own: it writes
    to the bare repos and to nothing else, so no worktree moves and no
    branch changes. What it is for is the question the other commands
    answer only as a side effect — whether there is anything to get, and
    whether someone force-pushed under you.
    """
    ws_dir, ws = resolve_workspace(name=workspace)
    report_pending_migration(config, ws, ws_dir)
    aliases = select_aliases(list(ws.repos), only)
    if not aliases:
        return

    # --only must narrow the fetching itself, not just the report. `replace`
    # keeps the schema version, the typed overrides and any legacy evidence:
    # a narrowed view of the workspace is still the same workspace.
    selected = replace(ws, repos={alias: ws.repos[alias] for alias in aliases})

    outcome = fetch_workspace_refs(
        selected, ws_dir, config, fetch_upstreams=True,
        resolve_fn=resolve_spec, spinner_prefix="Fetching",
    )

    _display(ws_dir.name, _rows(selected, ws_dir, outcome))

    if outcome.failed:
        err_console.print(
            f"\n[red]{len(outcome.failed)} repo(s) could not be fetched[/]: "
            "the refs they follow are still the ones of the last successful fetch."
        )
        sys.exit(1)
