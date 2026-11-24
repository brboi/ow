# Changelog

All notable changes to ow. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [PEP 440](https://peps.python.org/pep-0440/) (`3.0.0b1` is a pre-release).

## Unreleased

## 3.0.0b1

ow 3.0 is a rewrite of ow 2.x around a generic worktree core, a Workspace + Stack model and plugins.
**Read [Migrating to 3.0](docs/migrating-to-3.0.md) first**: it lists what to delete, what to run
and what to script differently. The plugin API (v1) is frozen through every 3.x release.

### Breaking changes

- **ow 2.x is gone.** No TUI, no templates (`[templates]`, `[vars]`, bundles), no `ow mv`, `ow archive`,
  `ow unarchive`, `ow switch`, `ow pull`, `ow reset`. `ow apply` is `ow refresh`, `ow templates` is
  `ow files`, `ow rebase` is `ow retarget`, `ow fetch` is `ow status --fetch`, `ow open` is `ow cd`,
  and `ow init` / `ow add` split the old `ow init`. Generated files are a fixed set, not templates.
- **2.x global config keys are unknown** (`version`, `theme`, `editor`, `owignore`, `[remotes]`);
  commands warn until you `ow config unset --global` them.
- **ow starts its own PostgreSQL, pgweb and Mailpit containers** on `ow run`; the 2.x compose stack and
  its volumes are no longer read. To keep your own server: `postgres.enabled = false`. See
  [Services](docs/migrating-to-3.0.md#services).
- **No `--json`** on any command (it existed in the 3.0 alphas).
- **Exit codes**: `ow run`, `ow exec` and `ow sandbox` return the child's code (`128 + n` on a signal);
  `ow logs` exits 1 when there is nothing to show; Ctrl+C is 130 everywhere.
- **CLI names**: a workspace is `WS` (`WS[/STACK]` where a stack is accepted), a spec is `SPEC`, and
  `--as NAME` is `--as STACK`.
- **Requirements**: Python ≥ 3.11, git ≥ 2.45, typer ≥ 0.29.2 (`click` is no longer a dependency).

### Added

- **Workspaces and stacks.** A mono-stack workspace, or a multi-stack one with named stacks, coordinated
  ports and a generated `<workspace>.code-workspace`; `ow add iap=iap#4321` promotes a mono-stack
  workspace. Shared bare repos, one worktree per repo and workspace.
- **Specs**: GitHub PRs with sister PRs (`enterprise#12345`), Runbot bundle/batch URLs, `<repo>@<ref>`,
  `@18.0`; companion repos are pinned to Runbot's `base_match` commit.
- **`ow run`**: foreground with the real TTY, parallel multi-stack runs, `-d` detached runs with
  `ow stop` and `ow logs [-f]`, `--tmp` throwaway workspaces, `--reset` (shows the database it will
  drop and asks; `-y` to skip).
- **`ow exec`**, **`ow sandbox`** (bubblewrap, with profiles for common AI agent harnesses),
  **`ow retarget`**, **`ow status`** (PR and Runbot state), **`ow files`**, **`ow trust`**.
- **Shared Python virtualenvs** per `(python, Odoo series)` through mise, and generated `odoorc`,
  `odools.toml`, `pyrightconfig.json`, VS Code and Zed settings that never overwrite a hand edit.
- **Shared services**: `ow-postgres` (with a random superuser password kept in
  `$XDG_STATE_HOME/ow/postgres-admin-password`), `ow-pgweb` and `ow-mailsink`; a setting you set
  yourself always wins over what a service provides.
- **Unique database names**: a new workspace whose `odoo.db_name` collides with another one gets
  `<name>-2` in its config.
- **Plugins**: ten built in (`mise`, `odoo`, `postgres`, `mailsink`, `github`, `runbot`, `vscode`,
  `zed`, `local`, `sandbox`), and plugins shipped inside a repo (`[tool.ow] plugin` or `ow_plugin.py`),
  loaded after `ow trust`. Trust is by location, like an editor's workspace trust: trusting a directory
  trusts any code that later arrives in it, including a PR checked out with `ow add`. The guide:
  [Plugins](docs/plugins.md). Settings must be prefixed with the plugin's name.
- Git's SSH and credential prompts pass through on a terminal; unattended runs never prompt.

### Changed since 3.0.0a1

- Plugin API: `ow_resolve_target`/`ResolvedTarget` are `ow_resolve_spec`/`ResolvedSpec`, `ow_status`
  takes `spec`, `StatusInfo.target` is `subject`, `DatabaseService.admin_url` is `web_url`, and
  `ow_logs`/`ow_multi_logs` no longer receive `follow`/`lines`. New: `ow_reset_preview`,
  `Setting.unique`, `DatabaseService.admin_user`/`admin_password`, `Workspace.origins`/`is_explicit`,
  and `UsageError`, `PluginError`, `RepoFacts`, `Answers` exported from `ow.plugin_api`.
- Plugin API, breaking for plugins written against 3.0.0a1:
  - `resolve_stack_python(ws, plugins=None)` is `resolve_stack_python(ws, answers)`, with the `answers`
    ow hands to your hook. It is the only Python helper exported: `resolve_workspace_python` stays
    internal.
  - A module's `NAME` attribute is no longer read, only `PLUGIN_NAME`; a plugin name must match
    `[A-Za-z0-9_-]+`, so a repo named `foo.bar` needs a `PLUGIN_NAME` or is skipped.
  - Every exported record is keyword-only (`Setting(key=…, type=…)`, `GeneratedFile(path=…, data=…)`):
    a field can then be added in any 3.x release.
  - `StatusInfo.data` and `PruneCandidate.data` are removed; nothing read them once `--json` was gone.
  - `ow_logs`/`ow_multi_logs` return `list[tuple[str, Path]] | None` only: a `RunCommand` could not
    honour `-f`/`-n`.
  - `ow_settings` of an in-repo plugin that raises skips that plugin alone, with a warning.
  - `Workspace.setting_int` is removed (use `ws.settings[key]`).
- `ow prune` and `ow rm` refuse a workspace with a running process; loading an in-repo plugin no longer
  writes `__pycache__` into its repo; `ow logs -f` stops when the process dies; each `ow run -d`
  starts a fresh log.
- Ctrl+C on a foreground `ow run`, `ow exec`, `ow sandbox` or `ow stop` no longer kills the child
  0.25 s later: the first one waits for it to tear down, a second one terminates it (SIGTERM, then
  SIGKILL); the exit status is 130. An empty `ow_logs`/`ow_multi_logs` list means "nothing from this
  plugin" (like `None`): `ow logs` falls back to `.ow/logs`. `detect_container_engine()` returns
  `docker`/`podman` even when `OW_CONTAINER_ENGINE` is a path. A `unique` setting of an in-repo
  plugin no longer collides with a workspace that lacks that plugin.

### Security

- The `postgres` superuser no longer has the `odoo` password. `postgres.bind_address` is still
  `0.0.0.0` so containers can reach the server: set `127.0.0.1` on untrusted networks.
- In-repo plugins run only from locations you trusted; an untrusted plugin only produces a warning.

[Unreleased]: https://github.com/brboi/ow/compare/v3.0.0b1...HEAD
[3.0.0b1]: https://github.com/brboi/ow/compare/v3.0.0a1...v3.0.0b1
