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
- **Setup boilerplate** — `mise.toml`, `odoorc`, `odools.toml`, `pyrightconfig.json`, and IDE
  configs are generated from [templates](docs/templates.md) every time.
- **Branch juggling** — concise `base..feature` branch specs control detached vs attached
  worktrees; `ow rebase` keeps them up to date idempotently.
- **Workspace discovery** — workspaces live anywhere; an index remembers where, so `ow status
  myfeature` finds it by name.

## Prerequisites

- **[mise](https://mise.jdx.dev/)** — manages Python, virtualenvs, and dependencies in generated workspaces
- **Odoo system dependencies** — see [Odoo source install docs](https://www.odoo.com/documentation/master/administration/on_premise/source.html#dependencies) (includes wkhtmltopdf, PostgreSQL client libs, etc.)
- **SSH** — configured for access to Odoo repositories
- **Docker or Podman** (optional) — `ow` ships a compose file for postgres, pgweb and mailpit; see [Services](docs/services.md)

## Installation

```sh
pipx install odoo-workspaces   # recommended
pip install odoo-workspaces    # or in an active venv
```

## Quick Start

```sh
mkdir my_work && cd my_work
ow init                            # interactive form: templates, repos, branch specs
mise install
code .                             # open in your IDE and enjoy
```

On first use, `ow` writes a commented default config to `~/.config/ow/config.toml` — edit it to
point at your Odoo remotes. See [Configuration](docs/configuration.md) for the full layout.

## Documentation

- [Commands](docs/commands.md) — full command reference with flags and workspace resolution
- [Configuration](docs/configuration.md) — XDG paths, remotes, variables, branch spec syntax
- [Template System](docs/templates.md) — bundles, overrides, context keys, custom bundles
- [Services](docs/services.md) — Docker Compose stack (postgres, pgweb, mailpit)
- [Sandboxing AI Coding Assistants](docs/sandboxing.md) — bubblewrap isolation for Opencode and Claude Code
- [Tab Completion](docs/commands.md#tab-completion) — fish, bash, zsh, powershell
- [Migrating from 1.x](docs/migrating-to-2.0.md) — one-time move from the project-scoped layout

## Thanks

This is a small personal project I built with AI coding. It scratches a very specific itch I have — managing multiple Odoo worktrees side by side.

If you find it useful, please consider contributing!

Contributions are welcome! If something is broken, confusing, or missing — open an issue. If you have a fix or improvement in mind, PRs are appreciated.
