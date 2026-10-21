# Migrating to ow 3.0

ow 3.0 is a rewrite. Breaking changes from 2.x are documented here.

## Adopting from 2.x or 3.0.0a1

If you have an existing workspace, `ow init` will adopt it in each workspace:

```sh
cd ~/workspace && ow init
```

### What `ow init` does

On adoption, ow 3.0:

1. **Infers missing default bases.** A repo with a rejected or missing base value gets the agreement rule (computed from the values that passed), else `--base` if you passed it, else `origin/HEAD`. Each inferred base is printed with its source.

2. **Rewrites `.ow/config.toml` whole.** The dropped keys (`version`, `templates`, `[vars]`, and rejected `[repos]` values) are printed with their values for reference.

3. **Refreshes.** The summary lists any "yours" files (files you edited) with the hint "delete it and run `ow refresh`".

4. **No prompts, no git commands, no backfill.** `branch.<b>.ow-base` is not added retroactively to existing branches.

Before `ow init`, every command already works on a 2.x workspace and warns about unknown keys (pointing at `ow config unset [--global] <key>`) and rejected bases (pointing at `ow init`).

## What is read as is

- The lock (same path and format)
- `[odoo]`/`[mise]` tables as settings (schema 1's `[vars]` is not read)
- `[repos]` default bases (through the `--base` reader: `18.0`, `master`, and `origin/master` pass; `x..y` and SHAs are rejected)
- The index file

## Global config: unknown keys

The following are unknown and will be warned about on every command run. To remove them, use `ow config unset --global <key>`:

- `version`
- `theme`
- `editor`
- `owignore`
- `[remotes]` (ow 2 already wrote these to the bare repos themselves)

## Safe to Delete

After adopting and verifying your workspace runs, these are safe to delete:

- `.ow/templates.lock.toml`
- `.ow/templates/`
- A schema-1 root `mise.toml`
- Generated files no plugin produces any more
- `$XDG_CONFIG_HOME/ow/templates/`
- `$XDG_STATE_HOME/ow/backups/`
- `$XDG_STATE_HOME/ow/template-base/`

### Do NOT delete

- **`~/.config/ow/services/compose.yml`** is now the user's; ow never manages it
- **`$XDG_DATA_HOME/ow/volumes/`** holds your database; neither the compose file nor this directory is **ever** safe to delete
- **`$XDG_CONFIG_HOME/ow/local/`** stays: it is the copy-once seed source for `.local/`

## Command Changes

The following commands have been removed or renamed:

| Old | New | Notes |
|---|---|---|
| `ow render` | `ow refresh` | Recompute and write generated files |
| `ow mv` | `mv` + `ow init` | Use a plain filesystem move, then `ow init` in the new location |
| `ow open` | `ow cd` + your editor | Use `ow cd` to find the workspace path |
| `ow archive` / `unarchive` | (removed) | — |
| `ow remote` | `git remote` | Use plain `git remote` commands |
| `ow status --fetch` | `ow fetch` | Use `ow fetch` instead |
| `ow init --configuration` | (removed) | — |
| `ow switch --all` / `--include-detached-specs` | (removed) | — |
| `ow prune --also-backups` | (removed) | — |
| `ow ls --archived` | (removed) | — |
| TUI and `$OW_WORKSPACE` | (removed) | Use `ow status -w <name>` to check a specific workspace |
| `-v` short flag | (removed) | Use `--verbose` or remove it from scripts |

## Environment Variables

- **`OW_WORKSPACE`**: no longer used. The current directory determines the workspace (walk up to `.ow/config.toml`), or use `-w/--workspace <name>` to specify it.

## COMPOSE_FILE and the mise fragment

ow 3.0 does not require or manage `COMPOSE_FILE`. At your first `ow refresh`, it disappears from the mise fragment.

If you use Docker Compose for services (postgres, pgvector, mailpit), add this optional tip to your global mise config at `$XDG_CONFIG_HOME/ow/config.toml` or in your own `mise.toml`:

```toml
[env]
COMPOSE_FILE = "~/.config/ow/services/compose.yml"
```

See [Services](docs/services.md) for the recommended compose stack.

## Sandbox tasks

The sandbox tasks are gone. See [Sandboxing AI Coding Assistants](docs/sandboxing.md) for a bwrap recipe that works with the new layout.

## Templates

Implicit template handling is gone. The old `[templates]` and `[vars]` are not read. Odoo configs are generated from the workspace's state at each refresh.

## Git Version Requirement

ow 3.0 requires **git ≥ 2.45** for the `cherry-pick --empty=drop` flag used in rebase conflict recovery.

## SSH and Fetches

Fetches rely on `ssh-agent`. There is no askpass broker, and ow never prompts for a passphrase. Ensure your SSH keys are loaded:

```sh
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_rsa
```

For HTTPS clones, configure a git credential helper.

## Testing the Migration

After adoption, run a quick smoke test:

```sh
# Check status
ow status

# Refresh generated files
ow refresh

# Check a specific workspace by name
ow status -w myfeature
```

---

**Version:** 3.0.0  
**Release Date:** 2026-10-21  
**Minimum git:** 2.45  
**Minimum Python:** 3.11
