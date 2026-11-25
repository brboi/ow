# ow — Odoo Workspaces

ow keeps Odoo's repos on one machine and gives each workspace its own git worktrees. The bare repos are shared: Odoo is cloned once and every fetch is visible to every workspace. A worktree is a cheap checkout, so a new branch is a new directory. The core is plain git. Plugins add the Odoo, GitHub and Runbot knowledge ([built-in plugins](docs/plugins.md#built-in-plugins)), and a repo can ship its own ([writing a plugin](docs/plugins.md)).

## Install

```sh
pipx install --pip-args=--pre odoo-workspaces   # 3.0 is in beta: a plain install gets 2.x
pip install --pre odoo-workspaces               # or, inside a venv
```

Coming from 3.0.0a1 or 2.x? Read [Migrating to 3.0](docs/migrating-to-3.0.md) first.

## Requirements

- Linux.
- Python ≥ 3.11 and git ≥ 2.45.
- SSH access to the repos you use. How ow handles git's prompts: [Commands](docs/commands.md#confirmation-rules).

Optional:

- **`gh`** (GitHub CLI) for PR specs, PR status and merged-PR pruning. Turn the plugin off with `ow config set --global github.enabled false`.
- **mise** ≥ 2026.8.13 for Python virtualenvs shared by Python version and Odoo series. Without it, ow uses `<workspace>/.venv` if you create it.
- **Docker or Podman** for the [shared services](docs/configuration.md#shared-services): PostgreSQL, pgweb and Mailpit.
- **bubblewrap** (`bwrap`) for the experimental [`ow sandbox`](docs/commands.md#ow-sandbox).

## Quick start

### Create a workspace (`ow init`)

```sh
# 1. A mono-stack workspace on 18.0: community and enterprise on a new branch 18.0-my-fix
mkdir my-fix && cd my-fix
ow init @18.0..18.0-my-fix

# 2. A multi-version, multi-service review in one multi-stack workspace
mkdir voip-review && cd voip-review
ow init \
  odoo=community#56789,enterprise#12345,upgrade#2345 \
  iap=iap#4321 \
  voip=voip
```

Each stack gets its own worktrees and its own HTTP and gevent ports. A stack with an Odoo core also gets an `odoorc`. The root gets `voip-review.code-workspace`. ow prints the whole plan before it acts.

Base checkouts (`@18.0`, `iap@18.0`, `voip`) stay detached, so workspaces never collide on a local `18.0` or `master`. The spec grammar is in [Commands](docs/commands.md#specs).

### Run (`ow run`)

```sh
# In a mono-stack workspace (or a stack): the runner gets your terminal, so pdb works
ow run -- -i sale

# At a multi-stack root: every runnable stack, in parallel, with [stack] prefixes
ow run
ow run odoo:"-i voip --dev=all" iap:"--log-level=debug"

# A throwaway run, cleaned up on exit
ow run --tmp enterprise#12345 -- -i sale

# A command or a shell inside a stack's Python environment
ow exec -- pip install phonenumbers
ow exec iap
```

### Evolve a workspace (`ow add`)

```sh
ow add upgrade               # add a repo to the current workspace or stack
ow add enterprise#12345      # attach a PR to an existing clean or detached worktree
ow add iap=iap#4321          # promote a mono-stack workspace to multi-stack (asks first)
```

### Update my branch on its base (`ow rebase`)

```sh
ow rebase                    # fetch, then put each branch back on its base
ow rebase 19.0               # change series: move every repo onto origin/19.0
```

### Inspect and clean up

```sh
ow status                    # branches, bases, upstreams, PR and Runbot state
ow status --fetch            # fetch tracked refs in parallel first
ow ls                        # every indexed workspace
ow rm voip-review/iap        # remove one stack (`ow rm voip-review` removes all)
ow prune                     # stale state, and workspaces whose PRs are merged or closed
```

## Documentation

- [Commands](docs/commands.md): every command, the spec grammar, flags, exit codes, confirmation rules
- [Configuration](docs/configuration.md): settings, layers, shared services
- [Generated files](docs/files.md): where ow keeps its files, ownership and the refresh model
- [Plugins](docs/plugins.md): built-in plugins, writing and trusting a plugin, the hook reference, versions
- [Migrating to 3.0](docs/migrating-to-3.0.md): from 3.0.0a1 and 2.x
- [Changelog](CHANGELOG.md)
