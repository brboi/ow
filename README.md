# ow — Odoo Workspaces

ow keeps Odoo's repos on one machine and gives each task its own workspace of git worktrees. The
bare repos are shared, so Odoo is cloned once and every fetch is visible to every workspace; a
worktree is a cheap checkout, so a new branch costs seconds, not gigabytes. The core is plain git
and knows nothing about Odoo: Odoo, GitHub (`gh`), mise, VSCode, Zed, local seeding and opt-in
`retarget` are built-in plugins.

## Install

```sh
pipx install odoo-workspaces   # recommended
pip install odoo-workspaces    # or, inside a venv
```

`ow` needs git ≥ 2.45 and an SSH key with access to the Odoo repos. GitHub CLI (`gh`) is used by
the built-in `github` plugin for PR resolution (`ow add enterprise#12345`), PR status, and
merged-PR pruning. mise (≥ 2026.8.13) is optional; with it, ow writes a mise fragment pointing at
a shared Python virtualenv per `(python, odoo-series)` under `~/.local/share/ow/venvs/`.

## Quick start

### Review or run a PR in one command

```sh
ow run enterprise#12345 -- -i sale
```

`ow` asks `gh` for the PR's head and base branches, discovers any sister PR with the same branch
on `community` (or detaches `community` on the base branch), creates `12345-<branch>/`, generates
config/IDE files, and launches Odoo locally. Pass `--tmp` for a throwaway run cleaned up on exit.

### Create a workspace or a multi-workspace task

```sh
# Fresh workspace on 18.0 (defaults to community + enterprise)
ow add my-fix --base 18.0 --branch 18.0-my-fix

# Multi-workspace task in one command (distinct HTTP ports + multi-root .code-workspace)
ow add -t voip-task iap-apps#42+enterprise enterprise#15402 voip#89

# Inspect status (includes PR state + task siblings in --json for UIs & coding harnesses)
ow status
ow status --json
ow status -t voip-task

# Clean up a workspace, a whole task, or merged PRs machine-wide
ow rm 12345-18.0-fix-invoice-mic
ow rm -t voip-task
ow prune
```

### Opt-in guarded replay (`ow retarget`)

```sh
ow config set --global retarget.enabled true
ow retarget 18.0
```

## Documentation

- [Commands](docs/commands.md) — every command, its flags, `--json` contract, exit codes and confirmation rules
- [Configuration](docs/configuration.md) — settings, precedence, and `ow config`
- [Generated files](docs/files.md) — the fixed outputs, ownership, file states and the refresh model
- [Services](docs/services.md) — the postgres + pgvector + mailpit compose stack
- [Sandboxing AI Coding Assistants](docs/sandboxing.md) — bwrap recipe for AI assistants with repo access
