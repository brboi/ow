# Migrating to ow 3.0

ow 3.0 is a rewrite: a generic worktree core with a Workspace + Stack model, built-in plugins (`mise`, `odoo`, `github`, `runbot`, `vscode`, `zed`, `local`), shared Python virtualenvs, parallel multi-stack `ow run`, fixed generated files instead of templates, and no TUI.

## Creating and evolving workspaces in 3.0

`ow init` initializes the current directory (`.`): empty, mono-stack, or multi-stack. Re-running `ow init` inside a moved or existing workspace repairs git worktrees and refreshes files. `ow add` evolves an initialized workspace (adding repos, attaching PRs, or promoting a mono-stack workspace to multi-stack):

```sh
# Mono-stack workspace on a PR (pins community to Runbot's exact base_match commit)
mkdir my-review && cd my-review
ow init enterprise#12345

# Multi-stack workspace in one command (coordinated ports + <workspace>.code-workspace)
mkdir voip-review && cd voip-review
ow init odoo=community#56789,enterprise#12345,upgrade#2345 iap=iap#4321 voip=voip

# Fresh workspace on 18.0 (expands odoo.default_repos on branch 18.0-my-fix)
mkdir my-fix && cd my-fix
ow init @18.0 --branch 18.0-my-fix
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

- `$XDG_CONFIG_HOME/ow/services/compose.yml` — yours to manage; ow 3 no longer ships or reads a compose stack.
- `$XDG_DATA_HOME/ow/volumes/` — holds your databases (`postgres-data/`, `mailpit-data/`, database `postgres`). To keep using them, bind-mount those directories in your own compose file and run `ow config set odoo.db_name postgres`.
- `$XDG_CONFIG_HOME/ow/local/` — copy-once seed source for `.local/`.

## Commands that changed

| Old | New | Notes |
|---|---|---|
| `ow init` | `ow init [SPEC…]` + `ow add SPEC…` | `ow init` initializes/composes/repairs `.`; `ow add` evolves an existing workspace |
| `ow render` | `ow refresh` | Recompute and write owned generated files (and `<workspace>.code-workspace`) |
| `ow switch` / `ow pull` / `ow reset` | plain `git` | Daily git operations stay with git |
| `ow rebase` | `ow retarget` | Replays branches, switches detached repos, updates `[repos]` |
| `ow fetch` | `ow status --fetch` | Surgical parallel fetch of tracked refs |
| `ow open` | `ow cd` + editor | `ow cd` prints workspace/stack paths; multi-stack generates `<workspace>.code-workspace` |
| `ow mv` / `ow archive` / `ow remote` / TUI | (removed) | Move with `mv`, then run `ow init` to repair worktrees and update the index |

## Environment variables & templates

- `OW_WORKSPACE` is gone; ow walks up to `.ow/config.toml` or takes `-w/--workspace`.
- Templates (`[templates]`, `[vars]`) are gone; see [Generated files](files.md).
- `COMPOSE_FILE` is no longer set by ow; set it yourself (e.g. in `~/.config/mise/config.toml`) if needed.
- Sandboxes must mount the bare repos and shared venvs; see [Generated files](files.md#outside-the-workspace).

## Requirements

- **git ≥ 2.45** (`git cherry-pick --empty=drop` for `ow retarget`).
- **Python ≥ 3.11**.
- **`gh` (GitHub CLI)** for GitHub PR targets/status/pruning (or disable via `ow config set --global github.enabled false`).
- Fetches are non-interactive (`GIT_TERMINAL_PROMPT=0`, `ssh -o BatchMode=yes`); load SSH keys with `ssh-add` or configure a git credential helper.

## See also

- [Commands](commands.md) — full 3.0 command reference.
- [Configuration](configuration.md) — settings, layers and `ow config`.
- [Generated files](files.md) — generated files and ownership locks.
