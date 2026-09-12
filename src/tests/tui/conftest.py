"""Shared fixtures for driving the real `DashboardApp` in tests.

Every TUI test used to hand-roll `asyncio.run(_run())` around its own
`DashboardApp(_make_config())` build, with a `_make_config`/`_make_workspace`
pair copy-pasted (and subtly diverging — some used `MagicMock` remotes that
choke `write_global_config`, some forgot to register the workspace with the
index) across every file. `dashboard_pilot` and `seed_workspace` replace all
of that with one real app at one pinned terminal size, and one workspace
seeding helper.

None of this is optional plumbing: the five crash-level bugs that reached a
user (NoMatches from the wrong query scope, a clipped header, NoActiveWorker
from `push_screen_wait`, a silently-reverted theme, a `get_cell_at` call
that never round-tripped a saved var) all needed a *pushed screen*,
*rendered geometry*, or a *clicked button* to show up. A dashboard that
never actually boots past `DashboardApp(config)` construction cannot catch
any of them.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import pytest

from ow.utils import index
from ow.utils.config import (
    BranchSpec,
    Config,
    RemoteConfig,
    WorkspaceConfig,
    write_workspace_config,
)
from ow.tui.dashboard import DashboardApp, MainScreen


def _default_config() -> Config:
    return Config(
        vars={"http_port": 8069, "db_host": "localhost", "db_port": 5432},
        remotes={
            "community": {"origin": RemoteConfig(url="git@github.com:odoo/odoo.git")},
        },
    )


@pytest.fixture
def seed_workspace() -> Callable[..., Path]:
    """Factory: `seed_workspace(base, name, repos=..., templates=..., vars=...)`
    writes a real `.ow/config.toml` under `base/name` and registers it with
    the workspace index — exactly what `ow init` does — so `MainScreen`'s
    startup `reload_workspaces()` (and any command run against the index)
    picks it up. Call this *before* opening `dashboard_pilot`: the initial
    reload happens once, at `on_mount`.
    """

    def _seed(
        base: Path,
        name: str,
        *,
        repos: dict[str, str] | None = None,
        templates: list[str] | None = None,
        vars: dict[str, Any] | None = None,
    ) -> Path:
        ws_dir = base / name
        ws_dir.mkdir(parents=True, exist_ok=True)
        parsed_repos = {
            alias: BranchSpec(spec)
            for alias, spec in (repos or {"community": "origin/master"}).items()
        }
        ws = WorkspaceConfig(
            repos=parsed_repos,
            templates=templates or ["common"],
            vars=vars or {},
        )
        write_workspace_config(ws_dir / ".ow" / "config.toml", ws)
        index.remember(ws_dir)
        return ws_dir

    return _seed


@pytest.fixture
def dashboard_pilot(xdg: Path):
    """`async with dashboard_pilot(config=None) as (pilot, screen):` boots a
    real `DashboardApp` on the isolated `xdg` config directory, at a pinned
    80x24 terminal size, waits for the initial `on_mount` legacy-layout
    check to clear, and hands back the pilot plus the pushed `MainScreen`.

    `config` overrides the default (a `community` remote and a few vars);
    `size` overrides the pinned 80x24 for tests that specifically need
    more room (e.g. a full-screen modal with a sidebar).
    """

    @asynccontextmanager
    async def _open(
        config: Config | None = None,
        *,
        size: tuple[int, int] = (80, 24),
    ) -> AsyncIterator[tuple[Any, MainScreen]]:
        app = DashboardApp(config if config is not None else _default_config())
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            screen = app.main_screen
            assert screen is not None
            # Let the on_mount legacy-layout check worker clear before the
            # test's own actions start competing with it for `_busy`.
            for _ in range(100):
                if not screen._busy:
                    break
                await pilot.pause()
            yield pilot, screen

    return _open
