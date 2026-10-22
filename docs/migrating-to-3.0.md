# Migrating to ow 3.0

ow 3.0 is a rewrite: a plugin-based core, fixed generated files instead of templates, no TUI. It keeps 3.0.0a1's layouts, so a 2.x or 3.0.0a1 workspace is read in place. This page lists what breaks and how to adopt an existing workspace.

## Adopting an existing workspace

```sh
cd ~/workspace && ow init
```

`ow init` adopts the workspace where it is:

- It infers a default base for every repo whose value is missing or rejected: the agreement rule from the values that passed, else `--base`, else `origin/HEAD`. Each inferred base is printed with its source.
- It repairs the bare repos ow 2 left behind: it sets `remote.origin.tagOpt` and `core.logAllRefUpdates` where missing, and seeds `origin/HEAD` where missing. It adds no fetch refspec and downloads nothing beyond the default branch's new commits; other branches arrive with `ow fetch` or `ow switch`. Fetches and pulls work afterwards.
- It relinks a worktree whose directory moved (`git worktree repair`).
- It rewrites `.ow/config.toml` whole, printing every key it drops with its value: `version`, `templates`, `[vars]`, and invalid `[repos]` bases.
- It indexes the workspace and refreshes the generated files. Files you edited are listed with the hint "delete it and run `ow refresh`".

No prompts, and no backfill: `branch.<b>.ow-base` is not added to existing branches.

## Before you adopt

Every command already works on a 2.x workspace. Each run warns, once per config file:

```
warning: unknown setting version in ~/.config/ow/config.toml; remove it with `ow config unset --global version`
warning: unknown setting version in .ow/config.toml; remove it with `ow config unset version`; ignoring default base x..y for myrepo in .ow/config.toml: invalid base; run `ow init`
```

Global unknown keys warn until you unset them; the workspace warning clears at `ow init`.

## What is read as is

- The lock, `.ow/rendered.lock.toml`, same path and format.
- The `[odoo]` and `[mise]` tables, as settings. Schema 1's `[vars]` is not read.
- `[repos]` default bases, through the same reader as `--base`: `18.0`, `master` and `origin/master` pass; `x..y` and SHAs are rejected.
- The workspace index.

## Global config

These keys are unknown to ow 3, so every command warns about them until you remove them with `ow config unset --global <key>`:

- `version`, `theme`, `editor`, `owignore`
- `[remotes]`: ow 2 already wrote the remotes into the bare repos, so ow 3 does not compare them.

## Safe to delete

Once the adopted workspace runs, these leftovers of older versions are safe to delete:

- `.ow/templates.lock.toml`, `.ow/templates/`
- a schema-1 root `mise.toml`
- generated files no plugin produces any more
- `$XDG_CONFIG_HOME/ow/templates/`
- `$XDG_STATE_HOME/ow/backups/`
- `$XDG_STATE_HOME/ow/template-base/`

### Never delete

- `$XDG_CONFIG_HOME/ow/services/compose.yml` — it is yours now; ow never manages it.
- `$XDG_DATA_HOME/ow/volumes/` — it holds your database. See [Services](services.md) before reusing the stack.
- `$XDG_CONFIG_HOME/ow/local/` — it is the copy-once seed source for `.local/`.

## Commands that changed

| Old | New | Notes |
|---|---|---|
| `ow render` | `ow refresh` | Recompute and write the generated files |
| `ow mv` | `mv` + `ow init` | Move the directory, then `ow init` in the new location |
| `ow open` | `ow cd` + your editor | `ow cd` prints the workspace path |
| `ow archive` / `ow unarchive` | (removed) | — |
| `ow remote` | `git remote` | Use plain git |
| `ow status --fetch` | `ow fetch` | Fetches every remote, then shows the status |
| `ow init --configuration` | (removed) | — |
| `ow switch --all` / `--include-detached-specs` | (removed) | — |
| `ow prune --also-backups` | (removed) | — |
| `ow ls --archived` | (removed) | — |
| TUI | (removed) | ow has no interactive UI |

## Environment variables

`OW_WORKSPACE` is gone. The workspace comes from the current directory (walking up to `.ow/config.toml`), or from `-w/--workspace <name>`.

## Templates

Implicit template handling is gone: `[templates]` and `[vars]` are not read. Plugins generate a fixed set of files from each workspace's state at every refresh. See [Generated files](files.md).

## COMPOSE_FILE

ow 3 never sets `COMPOSE_FILE`. Its mise fragment is `mise/conf.d/00-ow.toml` inside the workspace and carries no services. If you run the compose stack, add the tip to your global mise config at `~/.config/mise/config.toml`:

```toml
[env]
COMPOSE_FILE = "/path/to/compose.yml"
```

See [Services](services.md) for the stack.

## Sandboxing

The sandbox tasks are gone. See [Sandboxing AI assistants](sandboxing.md) for the bwrap and firejail recipes that fit ow's layout.

## Requirements

- **git ≥ 2.45** — `git cherry-pick --empty=drop` in rebase conflict recovery.
- **Python ≥ 3.11**.
- Fetches are non-interactive: ow always runs git with `GIT_TERMINAL_PROMPT=0`, and with `ssh -o BatchMode=yes` unless you set `core.sshCommand`, `GIT_SSH_COMMAND` or `GIT_SSH`. There is no askpass broker, so load your keys first:

```sh
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_rsa
```

For HTTPS clones, configure a git credential helper.

## Smoke test

```sh
ow status          # every repo, its branch and its state
ow refresh         # rewrite the generated files
ow status -w myfeature   # a workspace by name
```

## See also

- [Commands](commands.md) — the full 3.0 surface, with flags and exit codes.
- [Configuration](configuration.md) — settings, layers and `ow config`.
