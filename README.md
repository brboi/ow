# ow — Odoo Workspaces

ow keeps Odoo's repos on one machine and gives each workspace its own git worktrees. The bare repos
are shared, so Odoo is cloned once and every fetch is visible to every workspace; a worktree is a
cheap checkout, so a new branch costs seconds, not gigabytes. The core is plain git; `odoo`, `github`
(`gh`), `runbot`, `mise`, `vscode`, `zed`, and `local` are built-in plugins.

## Install

```sh
pipx install odoo-workspaces   # recommended
pip install odoo-workspaces    # or, inside a venv
```

`ow` needs git ≥ 2.45 and an SSH key with access to the Odoo repos. GitHub CLI (`gh`) is used by
the built-in `github` plugin for PR resolution (`ow init enterprise#12345`), PR status, and
merged-PR pruning. The built-in `runbot` plugin queries Runbot's public JSON API to pin companion
repos (`community`) to the exact Runbot `base_match` commit, resolve direct Runbot bundle/batch
URLs, and display build status (printing a fallback note when Runbot is unreachable). mise
(≥ 2026.8.13) is optional; with it, ow writes a mise fragment pointing at a shared Python
virtualenv per `(python, odoo-series)` under `~/.local/share/ow/venvs/`.

## Quick start

### Initialize a mono-stack or multi-stack workspace (`ow init`)

```sh
# 1. Mono-stack workspace on 18.0 (expands odoo.default_repos: community + enterprise, each on branch 18.0-my-fix)
mkdir my-fix && cd my-fix
ow init @18.0 --branch 18.0-my-fix

# 2. Review a multi-version, multi-service feature in one multi-stack workspace
mkdir voip-review && cd voip-review
ow init \
  odoo=community#56789,enterprise#12345,upgrade#2345 \
  iap=iap#4321 \
  voip=voip
```

Each stack gets its own worktrees and non-colliding HTTP and gevent ports; stacks with an Odoo core
also get an `odoorc`. The root gets `voip-review.code-workspace`.

Upstream base checkouts (`@18.0`, `iap@18.0`, `voip`) stay detached, so workspaces never collide on
local `18.0`/`master` branches. PR targets pin companion repos (`community`) to Runbot's exact
`base_match` commit when available.

### Run locally (`ow run`)

```sh
# Inside a mono-stack workspace (or a single stack): inherited TTY (pdb / shell work directly)
ow run -- -i sale

# At a multi-stack workspace root: runs every runnable stack in parallel with [stack] logs
ow run
ow run odoo:"-i voip --dev=all" iap:"--log-level=debug"

# Throwaway run without initializing a persistent workspace (cleaned up on exit)
ow run --tmp enterprise#12345 -- -i sale
ow run --tmp odoo=enterprise#12345:"-i voip" iap=iap#4321
```

### Evolve an existing workspace (`ow add` & `ow retarget`)

```sh
ow add upgrade                  # add a missing repo to the current workspace/stack
ow add enterprise#12345         # attach a PR onto an existing clean/detached worktree
ow add iap=iap#4321             # promote a mono-stack workspace to multi-stack (confirmed)
ow retarget 19.0                # replay commits & retarget detached repos onto origin/19.0
```

### Inspect & clean up

```sh
ow status                       # workspace or multi-stack status (with cached PR + Runbot info)
ow status --json                # machine-readable JSON for UIs & coding harnesses
ow ls                           # list all indexed workspaces and their repos or stacks
ow rm voip-review/iap           # remove a single stack (or `ow rm voip-review` for the whole workspace)
ow prune                        # clean up stale state and merged/closed-PR workspaces & stacks
```

## Documentation

- [Commands](docs/commands.md) — every command, specs, flags, `--json` contract, exit codes and confirmation rules
- [Configuration](docs/configuration.md) — settings, precedence, and `ow config`
- [Generated files](docs/files.md) — the fixed outputs, ownership, file states and the refresh model
- [Services](docs/services.md) — the postgres + pgvector + mailpit compose stack
- [Migrating to 3.0](docs/migrating-to-3.0.md) — what changed since ow 2 and what to delete
- [Sandboxing AI Coding Assistants](docs/sandboxing.md) — bwrap recipe for AI assistants with repo access
