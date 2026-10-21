# ow — Odoo Workspaces

A CLI that keeps Odoo's repos (community, enterprise, your own) side by side as git worktrees, one workspace per task, ready to code. The core is plain git and knows nothing about Odoo: Odoo, mise and editor support are built-in plugins.

## First-time flow

```sh
ow repo add community                              # or: ow repo add community git@github.com:odoo/odoo.git
ow init voip community enterprise --base 18.0 --branch 18.0-voip-boi
cd voip && mise install && code .
ow status
```

Every workspace shares the same set of bare repos, so you clone Odoo once and fetch updates that are immediately visible to every workspace. Worktrees are cheap (no duplicate working trees), so you can keep multiple feature branches side by side without re-cloning gigabytes of history.

## Features

- **Clone once, many worktrees** — shared bare repos mean one clone per machine, one worktree per task.
- **Branch per task** — `ow switch`, `ow rebase` and `ow reset` move every repo in a workspace to a branch at once, and keep branches up to date idempotently.
- **Ready to code** — IDE configs (`pyrightconfig.json`, VSCode/Zed settings), Python environment (`.venv`), Odoo config (`odoorc`), and mise setup are generated every time, from a fixed set of outputs.
- **Workspace discovery** — workspaces live anywhere; an index remembers where, so `ow status -w myfeature` finds it by name.

## Prerequisites

- **Git** ≥ 2.45
- **SSH** — configured for access to Odoo repositories
- **mise** ≥ 2026.8.13 (optional at runtime; manages Python, virtualenvs, and dependencies)
- **Odoo system dependencies** — see [Odoo source install docs](https://www.odoo.com/documentation/master/administration/on_premise/source.html#dependencies) (includes wkhtmltopdf, PostgreSQL client libs, etc.)
- **Docker or Podman** (optional) — for the postgres + pgvector + mailpit stack; see [Services](docs/services.md)

## Installation

```sh
pipx install odoo-workspaces   # recommended
pip install odoo-workspaces    # or in an active venv
```

## Quick Start

```sh
mkdir voip && cd voip
ow init community enterprise --base 18.0 --branch 18.0-voip
mise install
code .
```

## Documentation

- [Commands](docs/commands.md) — every command with flags, exit codes and confirmation rules
- [Configuration](docs/configuration.md) — settings, precedence, and how to read/write them with `ow config`
- [Generated files](docs/files.md) — the fixed outputs, ownership, file states and the refresh model
- [Services](docs/services.md) — postgres + pgvector + mailpit compose stack
- [Sandboxing AI Coding Assistants](docs/sandboxing.md) — bwrap recipe for AI assistants with repo access
- [Migrating to 3.0](docs/migrating-to-3.0.md) — adoption path from 2.x, command changes, git requirements
