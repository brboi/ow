# ow — Odoo Workspaces

ow keeps Odoo's repos on one machine and gives each task its own workspace of git worktrees. The
bare repos are shared, so Odoo is cloned once and every fetch is visible to every workspace; a
worktree is a cheap checkout, so a new branch costs seconds, not gigabytes. The core is plain git
and knows nothing about Odoo: Odoo, mise, VSCode and Zed support are built-in plugins.

## Install

```sh
pipx install odoo-workspaces   # recommended
pip install odoo-workspaces    # or, inside a venv
```

`ow` needs git ≥ 2.45 and an SSH key with access to the Odoo repos. Odoo's own system
dependencies (PostgreSQL client libraries, wkhtmltopdf, …) stay yours to install: see the
[Odoo source install docs](https://www.odoo.com/documentation/master/administration/on_premise/source.html#dependencies).
mise (≥ 2026.8.13) is optional; with it, ow writes a mise fragment and trusts it, and mise builds
the workspace's `.venv` and installs Odoo's requirements.

## First workspace

```sh
ow repo add community      # no URL needed: ow knows community, enterprise, design-themes
ow repo add enterprise
ow init ws/voip community enterprise --base 18.0 --branch 18.0-voip
cd ws/voip && mise install
ow status
```

`ow repo add` registers a bare repo under `~/.local/share/ow/repos/<name>.git` — one clone per
machine. `ow init` creates the workspace directory, adds one worktree per repo on the branch,
indexes the workspace, and writes every generated file:

```
ws/voip/
├── .ow/config.toml            the repos and their bases, plus your settings
├── community/                 worktree of the community bare, on 18.0-voip
├── enterprise/                worktree of the enterprise bare, on 18.0-voip
├── mise/conf.d/00-ow.toml     mise: .venv, env, Odoo's postinstall
├── odoorc                     Odoo config, addons paths pointing at the worktrees
├── odools.toml                Odoo LSP config
├── pyrightconfig.json         pyright, resolving into .venv
├── requirements-dev.txt      inotify and friends
├── .vscode/settings.json      search excludes and language servers
├── .vscode/launch.json        run and debug-test launches
├── .zed/settings.json
└── .zed/debug.json
```

`mise install` builds `.venv` and installs `community/requirements.txt` and
`requirements-dev.txt` into it. `ow status` then prints, one line per repo:

```
community	18.0-voip	origin/18.0	(none)	(even)
enterprise	18.0-voip	origin/18.0	(none)	(even)
```

From there: `ow switch -c <branch>` for the next task, `ow fetch` to refresh the cached refs,
`ow rebase` to move onto a newer base, `ow rm <ws>` to clean up. Every command prints the git it
runs.

## Documentation

- [Commands](docs/commands.md) — every command, its flags, exit codes and confirmation rules
- [Configuration](docs/configuration.md) — settings, precedence, and `ow config`
- [Generated files](docs/files.md) — the fixed outputs, ownership, file states and the refresh model
- [Services](docs/services.md) — the postgres + pgvector + mailpit compose stack
- [Sandboxing AI Coding Assistants](docs/sandboxing.md) — bwrap recipe for AI assistants with repo access
- [Migrating to 3.0](docs/migrating-to-3.0.md) — adoption path from 2.x, command changes, git requirements