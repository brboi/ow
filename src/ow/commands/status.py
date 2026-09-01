from pathlib import Path

from ow.utils.display import console
from rich.text import Text
from rich.markup import escape
from ow.utils.drift import warn_if_drifted
from ow.utils.resolver import resolve_workspace
from ow.utils.config import Config
from ow.utils.status import (
    RepoStatus,
    WorkspaceStatus,
    gather_workspace_status,
    _display_detached_status,
    _display_attached_status,
)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _format_status_line(rs: RepoStatus, max_alias_len: int) -> str:
    if rs.kind == "detached":
        return _display_detached_status(rs, max_alias_len)
    return _display_attached_status(rs, max_alias_len)


def _render_status(status: WorkspaceStatus, max_alias_len: int) -> None:
    """Produce byte-identical CLI output from a WorkspaceStatus."""
    header = Text(f"[{status.ws_dir.name}]", style="bold cyan")
    console.print(header)
    console.print("    [dim]branches[/]")

    first_attached_branch: str | None = None
    github_links: list[tuple[str, str]] = []

    for rs in status.repos:
        alias = rs.alias
        padding = " " * (max_alias_len - len(alias) + 1)

        if rs.state == "not_applied":
            console.print(f"        {escape(alias)}:{padding}[dim](not applied)[/]")
            continue

        if rs.state == "unresolved":
            console.print(f"        {escape(alias)}:{padding}[red](error: could not resolve)[/]")
            continue

        if rs.state == "error":
            console.print(f"        {escape(alias)}:{padding}[red](error)[/]")
            if rs.fetch_failed:
                console.print(f"        {escape(alias)}:{padding}[red](fetch failed; showing stale)[/]")
            continue

        # state == "ok"
        console.print(_format_status_line(rs, max_alias_len))
        if rs.fetch_failed:
            console.print(f"        {escape(alias)}:{padding}[red](fetch failed; showing stale)[/]")
        if first_attached_branch is None and rs.runbot_branch:
            first_attached_branch = rs.runbot_branch
        if rs.github_url:
            github_links.append((alias, rs.github_url))

    if first_attached_branch or github_links:
        console.print("    [dim]links[/]")
        if first_attached_branch:
            runbot_url = f"https://runbot.odoo.com/runbot/bundle/{first_attached_branch}"
            console.print(f"        runbot: [link={runbot_url}]{first_attached_branch}[/]")
        for link_alias, link_url in github_links:
            link_padding = " " * (max_alias_len - len(link_alias) + 1)
            console.print(f"        {escape(link_alias)}:{link_padding}[link={link_url}]{link_url}[/]")

    console.print()


# ---------------------------------------------------------------------------
# Command: status
# ---------------------------------------------------------------------------


def cmd_status(config: Config, workspace: str | None = None, *, fetch: bool = False) -> None:
    """Show branch status for the current workspace."""
    ws_dir, ws = resolve_workspace(name=workspace)
    warn_if_drifted(ws, ws_dir)
    status = gather_workspace_status(ws, ws_dir, config, fetch=fetch)
    max_alias_len = max((len(a) for a in ws.repos), default=0)
    _render_status(status, max_alias_len=max_alias_len)
