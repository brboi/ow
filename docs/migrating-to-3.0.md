# Migrating to ow 3.0

ow 3.0 is a rewrite around a generic worktree core, a Workspace + Stack model and plugins. This page says what to run and what to delete. What changed at a glance is in the [changelog](../CHANGELOG.md). Requirements are in the [README](../README.md#requirements).

## From 3.0.0a1 (and 2.x)

Run `ow init` once in each workspace, at its root:

```sh
cd ~/Odoo/my-fix
ow init -n     # preview the rewrites
ow init
```

`ow init` migrates `.ow/config.toml` in place:

- It removes the root keys `version` and `templates`.
- It rewrites each `[repos]` entry of the form `<base>..<feature>` to `origin/<base>` and records the base of `<feature>` in the bare repo as `branch.<feature>.ow-base`.
- It moves each `[vars]` key that matches a declared `odoo.<key>` setting of the right type into `[odoo]`. When `[odoo]` already has the key, that value wins, the `[vars]` key is dropped and ow warns.
- Other `[vars]` keys stay as unknown keys. Remove each with `ow config unset vars.<key>`.

A multi-stack workspace is migrated per stack. `-n` prints the rewrites without touching the file. Other commands warn once (`… is in an older ow format … run `ow init` to migrate it`) and ignore the old `[repos]` entries until you do.

Then change what the new version names differently:

- **`mise.python` and `mise.shared_venv` are `python.version` and `python.shared_venv`.** A config that still sets the old keys is refused (exit 2) with `mise.python was renamed to python.version`. Edit the workspace config and your global config by hand.
- **`ow rebase` keeps its daily use.** Without argument it fetches and puts each branch back on its base. `ow rebase 19.0` changes the series. See [Commands](commands.md#ow-rebase).
- **`--branch` is gone from `ow init` and `ow add`.** Spell the base and the branch in the spec: `ow init @18.0..18.0-my-fix`. See [Specs](commands.md#specs).
- **`ow pull`, `ow switch`, `ow reset` and `ow fetch` are gone.** Use git. `ow status --fetch` replaces `ow fetch`.
- **`ow render` is `ow refresh`.**

## Commands that changed

| Old | New | Notes |
|---|---|---|
| `ow init` | `ow init [SPEC…]` and `ow add SPEC…` | `ow init` initializes, composes or repairs `.`. `ow add` evolves an existing workspace |
| `ow apply`, `ow render` | `ow refresh` | Recompute and write the owned generated files |
| `ow templates` | `ow files` | Generated files and their state. `--diff` shows the differences |
| `ow switch`, `ow pull`, `ow reset` | plain `git` | Daily git operations stay with git |
| `ow rebase` | `ow rebase` | Same daily use. `ow rebase BASE` changes the series |
| `ow fetch` | `ow status --fetch` | Parallel fetch of tracked refs |
| `ow open` | `ow cd` and an editor | `ow cd` prints a workspace or stack path |
| `ow mv`, `ow archive`, `ow unarchive`, the TUI | removed | Move with `mv`, then run `ow init` to repair worktrees and update the index |
| none | `ow run`, `ow stop`, `ow logs`, `ow exec`, `ow sandbox`, `ow trust` | See [Commands](commands.md) |

## Postgres and Mailpit

ow 3.0 starts its own PostgreSQL, pgweb and Mailpit containers on `ow run`. It does not read the 2.x compose stack or its volumes. The containers, their ports, how to keep your own server and the security note are in [Shared services](configuration.md#shared-services).

## Global config

These 2.x keys are unknown to ow 3. Commands warn until you remove them with `ow config unset --global <key>`:

- `version`, `theme`, `editor`, `owignore`
- `[remotes]`: ow 2 already wrote the remotes into the bare repos.

## Safe to delete

- `.ow/templates.lock.toml` and `.ow/templates/`
- a schema-1 root `mise.toml`
- `$XDG_CONFIG_HOME/ow/templates/`
- `$XDG_STATE_HOME/ow/backups/`
- `$XDG_STATE_HOME/ow/template-base/`

### Never delete

- `$XDG_CONFIG_HOME/ow/services/compose.yml` is yours to manage. ow 3 neither ships nor reads a compose stack.
- `$XDG_DATA_HOME/ow/volumes/` holds your 2.x databases (`postgres-data/`, `mailpit-data/`). ow 3 does not read it: `ow-postgres` starts empty in its own `ow-postgres-data` volume. To keep using those databases, run your own server on them and follow *Keep your own PostgreSQL* in [Shared services](configuration.md#shared-services). Set `odoo.db_name postgres` if that is where they live. `ow run --reset` refuses the `postgres` database.
- `$XDG_CONFIG_HOME/ow/local/` is the copy-once seed source for `.local/`.

## Behaviour changes to script against

- **No `--json`.** No command has a machine-readable mode. Read the plain output or the files ow writes.
- **Exit codes.** `ow run`, `ow exec` and `ow sandbox` return the child's exit code. `ow logs` exits 1 when there is nothing to show. See [Exit codes](commands.md#exit-codes).
- **Plugin trust.** A repo can carry its own ow plugin. It runs only after `ow trust`, which trusts a *location*. See [Trust](plugins.md#trust). Without a terminal, `ow trust` needs `-y`.

## Environment variables

- ow no longer reads `OW_WORKSPACE`. It walks up to `.ow/config.toml` or takes `-w/--workspace`. `ow exec` and `ow sandbox` still set it for the process they run.
- Templates (`[templates]`, `[vars]`) are gone. See [Generated files](files.md).
- ow no longer sets `COMPOSE_FILE`. Set it yourself if you need it, for example in `~/.config/mise/config.toml`.

## See also

- [Commands](commands.md): the command reference.
- [Configuration](configuration.md): settings, layers and shared services.
- [Generated files](files.md): generated files and ownership locks.
- [Plugins](plugins.md): built-in plugins and writing your own.
