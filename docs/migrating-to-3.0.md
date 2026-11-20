# Migrating to ow 3.0

ow 3.0 is a rewrite: a generic worktree core, built-in plugins (`mise`, `odoo`, `github`, `vscode`, `zed`, `local`, and opt-in `retarget`), multi-workspace tasks, shared Python virtualenvs, fixed generated files instead of templates, and no TUI.

## Creating workspaces in 3.0

`ow add` replaces `ow init` (there is no in-place legacy adoption command). Existing 2.x/3.0a1 workspaces with valid `.ow/config.toml` and worktrees remain readable by `ow status`, `ow refresh`, `ow files`, `ow config`, `ow ls`, `ow cd`, `ow rm`, and `ow prune`, or you can recreate them cleanly with `ow add`:

```sh
ow add enterprise#12345                                     # PR target + sister PRs + community
ow add -t voip-task iap-apps#42+enterprise enterprise#15402 voip#89  # multi-workspace task
ow add my-fix --base 18.0                                   # fresh workspace (odoo.default_repos)
```

When `ow repo add` touches a bare repo left behind by ow 2.x, it repairs it in place (`remote.origin.tagOpt --no-tags`, `core.logAllRefUpdates true`, and `origin/HEAD`) without adding a wide fetch refspec.

## Global config

These 2.x keys are unknown to ow 3, so commands warn about them until you remove them with `ow config unset --global <key>`:

- `version`, `theme`, `editor`, `owignore`
- `[remotes]`: ow 2 already wrote the remotes into the bare repos.

## Safe to delete

- `.ow/templates.lock.toml`, `.ow/templates/`
- a schema-1 root `mise.toml`
- `$XDG_CONFIG_HOME/ow/templates/`
- `$XDG_STATE_HOME/ow/backups/`
- `$XDG_STATE_HOME/ow/template-base/`

### Never delete

- `$XDG_CONFIG_HOME/ow/services/compose.yml` — yours to manage.
- `$XDG_DATA_HOME/ow/volumes/` — holds your databases. See [Services](services.md).
- `$XDG_CONFIG_HOME/ow/local/` — copy-once seed source for `.local/`.

## Commands that changed

| Old | New | Notes |
|---|---|---|
| `ow init` | `ow add` | Create workspaces from PR targets, composite specs, tasks, or a base ref |
| `ow render` | `ow refresh` | Recompute and write owned generated files (and task `.code-workspace`) |
| `ow switch` / `ow pull` / `ow reset` | plain `git` | Daily git operations stay with git |
| `ow rebase` | `ow retarget` (opt-in) | Enable globally via `ow config set --global retarget.enabled true` |
| `ow fetch` | `ow status --fetch` | Surgical parallel fetch of tracked refs |
| `ow open` | `ow cd` + editor | `ow cd` prints the workspace path; tasks generate `<task>.code-workspace` |
| `ow mv` / `ow archive` / `ow remote` / TUI | (removed) | — |

## New in 3.0

- **Multi-workspace tasks (`-t/--task`)**: flat sibling workspaces linked via `[workspace].task`, automatic non-colliding `odoo.http_port` and `odoo.gevent_port`, ownership-locked `<task>.code-workspace`, and sibling context in `ow status --json`.
- **Local runner (`ow run`)**: `ow run [TARGET] [--tmp] [-- ARGS…]` launches `odoo-bin` using the workspace's resolved virtualenv.
- **Built-in `github` plugin (`gh`)**: PR target resolution, sister-PR discovery, cached PR status (use `--offline` to skip network), and merged/closed PR cleanup in `ow prune`.
- **Shared virtualenvs**: `mise.shared_venv = true` (default) shares venvs under `$XDG_DATA_HOME/ow/venvs/py<ver>-<series>`.
- **Universal `--json`**: `schema_version = 1` JSON on `stdout`, progress/plans on `stderr`.

## Environment variables & templates

- `OW_WORKSPACE` is gone; ow walks up to `.ow/config.toml` or takes `-w/--workspace`.
- Templates (`[templates]`, `[vars]`) are gone; see [Generated files](files.md).
- `COMPOSE_FILE` is no longer set by ow; configure it in `~/.config/mise/config.toml` if needed (see [Services](services.md)).
- Sandbox recipes live in [Sandboxing AI assistants](sandboxing.md) (mount `$XDG_DATA_HOME/ow/venvs` when `mise.shared_venv = true`).

## Requirements

- **git ≥ 2.45** (`git cherry-pick --empty=drop` for opt-in `ow retarget`).
- **Python ≥ 3.11**.
- **`gh` (GitHub CLI)** for GitHub PR targets/status/pruning (or disable via `ow config set --global github.enabled false`).
- Fetches are non-interactive (`GIT_TERMINAL_PROMPT=0`, `ssh -o BatchMode=yes`); load SSH keys with `ssh-add` or configure a git credential helper.

## See also

- [Commands](commands.md) — full 3.0 command reference.
- [Configuration](configuration.md) — settings, layers and `ow config`.
- [Generated files](files.md) — generated files and ownership locks.
