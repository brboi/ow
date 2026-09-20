# ow — Odoo Workspaces

CLI tool that turns interactive prompts into ready-to-code Odoo workspaces using git worktrees.

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/brboi/ow)

## What it is

`ow` manages Odoo development workspaces. Each workspace is a directory holding git worktrees for
the Odoo repos you work on, plus generated IDE configs, Python environment, and Odoo config files
— all ready to open and start coding.

Every workspace on the machine shares the same set of bare repos, so you clone Odoo once and
fetch updates that are immediately visible to every workspace. Worktrees are cheap (no duplicate
working trees), so you can keep multiple feature branches side by side without re-cloning
gigabytes of history.

## What it solves

- **Clone fatigue** — Odoo repos are large. Shared bare repos mean one clone, many worktrees.
- **Setup boilerplate** — the mise fragment, `odoorc`, `odools.toml`, `pyrightconfig.json`, and
  the IDE configs are [generated](docs/files.md) every time, from a fixed set of outputs.
- **Branch juggling** — concise `base..feature` branch specs control detached vs attached
  worktrees; `ow rebase` keeps them up to date idempotently, and `ow switch` moves every repo in
  a workspace to a branch at once.
- **Workspace discovery** — workspaces live anywhere; an index remembers where, so `ow status
  myfeature` finds it by name.

## Prerequisites

- **[mise](https://mise.jdx.dev/)** — manages Python, virtualenvs, and dependencies in generated workspaces
- **Odoo system dependencies** — see [Odoo source install docs](https://www.odoo.com/documentation/master/administration/on_premise/source.html#dependencies) (includes wkhtmltopdf, PostgreSQL client libs, etc.)
- **SSH** — configured for access to Odoo repositories
- **Docker or Podman** (optional) — `ow` writes a compose file for postgres, pgweb and mailpit; see [Services](docs/services.md)

## Installation

```sh
pipx install odoo-workspaces   # recommended
pip install odoo-workspaces    # or in an active venv
```

## Quick Start

```sh
mkdir my_work && cd my_work
ow init                            # interactive form: repos and branch specs
mise install
code .                             # open in your IDE and enjoy
ow                                 # launch the interactive dashboard
```

There is no global config until you save one: `ow` runs on built-in defaults, the community remote
included, and the dashboard's global-config editor writes a commented
`~/.config/ow/config.toml` for you to edit. See [Configuration](docs/configuration.md) for the
full layout.

## Documentation

- [Commands](docs/commands.md) — full command reference with flags and workspace resolution
- [Interactive Dashboard](docs/commands.md#interactive-dashboard) — TUI for managing workspaces without memorising flags
- [Configuration](docs/configuration.md) — XDG paths, remotes, typed options, ignore list, branch spec syntax
- [Generated files](docs/files.md) — the fixed outputs, ownership, render and file states
- [Services](docs/services.md) — Docker Compose stack (postgres, pgweb, mailpit)
- [Sandboxing AI Coding Assistants](docs/sandboxing.md) — bubblewrap and firejail tasks for Opencode, Claude Code and Pi
- [Tab Completion](docs/commands.md#tab-completion) — fish, bash, zsh, powershell
- [Migrating to 3.0](docs/migrating-to-3.0.md) — schema 2, removed commands, backups
- [Migrating from 1.x to the 2.0 layout](docs/migrating-to-2.0.md) — the first step of the move, for a project-scoped 1.x install

## Thanks

This is a small personal project I built with AI coding. It scratches a very specific itch I have — managing multiple Odoo worktrees side by side.

If you find it useful, please consider contributing!

Contributions are welcome! If something is broken, confusing, or missing — open an issue. If you have a fix or improvement in mind, PRs are appreciated.
