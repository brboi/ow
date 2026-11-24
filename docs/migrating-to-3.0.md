# Migrating to ow 3.0

ow 3.0 is a rewrite: a generic worktree core with a Workspace + Stack model, ten built-in plugins (`mise`, `odoo`, `postgres`, `mailsink`, `github`, `runbot`, `vscode`, `zed`, `local`, `sandbox`), shared Python virtualenvs, shared Postgres and mail-capture services, parallel multi-stack `ow run`, fixed generated files instead of templates, plugins you can ship inside a repo, and no TUI. What changed at a glance is in the [changelog](../CHANGELOG.md); this page is what to do about it.

## Creating and evolving workspaces in 3.0

`ow init` initializes the current directory (`.`): empty, mono-stack, or multi-stack. Re-running `ow init` inside a moved or existing workspace repairs git worktrees and refreshes files. `ow add` evolves an initialized workspace (adding repos, attaching PRs, or promoting a mono-stack workspace to multi-stack):

```sh
# Mono-stack workspace on a PR (pins community to Runbot's exact base_match commit)
mkdir my-review && cd my-review
ow init enterprise#12345

# Multi-stack workspace in one command (coordinated ports + <workspace>.code-workspace)
mkdir voip-review && cd voip-review
ow init odoo=community#56789,enterprise#12345,upgrade#2345 iap=iap#4321 voip=voip

# Fresh workspace on 18.0 (expands odoo.default_repos on branch 18.0-my-fix)
mkdir my-fix && cd my-fix
ow init @18.0 --branch 18.0-my-fix
```

When `ow repo add` touches a bare repo left behind by ow 2.x, it repairs it in place (`remote.origin.tagOpt --no-tags`, `core.logAllRefUpdates true`, and `origin/HEAD`) without adding a wide fetch refspec.

## Services

`ow run` and `ow sandbox` start three shared containers (Docker or Podman, whichever is ready) before launching anything. They are shared by every workspace on the machine and keep running until `ow stop --services` (or `ow stop --all`):

| Container | Plugin | What | Where |
|---|---|---|---|
| `ow-postgres` | `postgres` | PostgreSQL (`postgres.image`, default `pgvector/pgvector:pg17`) with its data in the named volume `ow-postgres-data` | host port `5432` if free, else `5433` (`postgres.port` pins it) |
| `ow-pgweb` | `postgres` | pgweb, a web UI on the database (`postgres.pgweb`) | `http://localhost:8081` |
| `ow-mailsink` | `mailsink` | Mailpit, catching the mail Odoo sends | SMTP `1025`, UI `http://localhost:8025` |

The generated `odoorc` points at them (`db_host`, `db_port`, `db_user`, `db_password`, `smtp_server`, `smtp_port`) for every value you left at its default. **A value you set yourself, in any config layer, always wins**, even when it equals the default. ow uses `docker`, else `podman` (`OW_CONTAINER_ENGINE` forces one); without a working engine it warns (`docker/podman unavailable; skipping container startup`) and `odoorc` keeps the `odoo.db_*` values as configured. `ow exec` never starts the services.

**Keep your own PostgreSQL.** If you already run a server (on `5432`, from a compose file, or on the host), tell ow not to manage one, and point Odoo at yours explicitly:

```sh
ow config set --global postgres.enabled false
ow config set --global odoo.db_host localhost
ow config set --global odoo.db_port 5432
ow config set --global odoo.db_user odoo
ow config set --global odoo.db_password odoo
```

Without `postgres.enabled = false`, ow starts `ow-postgres` next to yours (on `5433` when `5432` is taken) and a default `odoo.db_port` follows the container, so you would land on an empty server. The same applies to Mailpit: `mailsink.enabled = false` plus an explicit `odoo.smtp_server` / `odoo.smtp_port`.

**Passwords.** Odoo connects as the `odoo` role (`postgres.user`, `postgres.password`, both default `odoo`; `NOSUPERUSER CREATEDB`). The `postgres` superuser is a different role: its password is random, generated once, kept in `$XDG_STATE_HOME/ow/postgres-admin-password` (mode `0600`) and applied to an existing `ow-postgres` container the next time it starts. ow itself administers the server through the container's local socket; the password is for plugins that connect over TCP as the superuser (`DatabaseService.admin_password`).

**LAN exposure.** `postgres.bind_address` defaults to `0.0.0.0`, because containers (the Wazo stack of `voip`, for one) reach the server through `host.docker.internal`. Any machine that can reach your host on that port can therefore log in as the `odoo` role with `postgres.password`, which is `odoo` unless you changed it. On an untrusted network, either run `ow config set --global postgres.bind_address 127.0.0.1` or change `postgres.password`. pgweb and Mailpit only listen on `127.0.0.1`.

**`ow run --reset`** drops the workspace's database and filestore, so it prints `database <user>@<host>:<port>/<db>` and asks first (`-y` skips the question). Two workspaces never share a default database name: a workspace created with the same directory name as another gets `odoo.db_name = "<name>-2"`.

## Global config

These 2.x keys are unknown to ow 3, so commands warn about them until you remove them with `ow config unset --global <key>`:

- `version`, `theme`, `editor`, `owignore`
- `[remotes]`: ow 2 already wrote the remotes into the bare repos.

## Safe to delete

- `.ow/templates.lock.toml`, `.ow/templates/`
- a schema-1 root `mise.toml`
- `$XDG_CONFIG_HOME/ow/templates/`
- `$XDG_STATE_HOME/ow/backups/`
- `$XDG_STATE_HOME/ow/template-base/`

### Never delete

- `$XDG_CONFIG_HOME/ow/services/compose.yml` — yours to manage; ow 3 no longer ships or reads a compose stack (see [Services](#services) for what replaced it).
- `$XDG_DATA_HOME/ow/volumes/` — holds your 2.x databases (`postgres-data/`, `mailpit-data/`). ow 3 does not read it: `ow-postgres` starts empty in its own `ow-postgres-data` volume. To keep using those databases, run your own server on them (bind-mount the directories in your compose file) and follow *Keep your own PostgreSQL* above, setting `odoo.db_name postgres` if that is where they live. `ow run --reset` refuses the `postgres` database.
- `$XDG_CONFIG_HOME/ow/local/` — copy-once seed source for `.local/`.

## Commands that changed

| Old | New | Notes |
|---|---|---|
| `ow init` | `ow init [SPEC…]` + `ow add SPEC…` | `ow init` initializes/composes/repairs `.`; `ow add` evolves an existing workspace |
| `ow apply` | `ow refresh` | Recompute and write owned generated files (and `<workspace>.code-workspace`) |
| `ow templates` | `ow files` | Generated files and their state; `--diff` for the differences |
| `ow switch` / `ow pull` / `ow reset` | plain `git` | Daily git operations stay with git |
| `ow rebase` | `ow retarget` | Replays branches, switches detached repos, updates `[repos]` |
| `ow fetch` | `ow status --fetch` | Surgical parallel fetch of tracked refs |
| `ow open` | `ow cd` + editor | `ow cd` prints workspace/stack paths; multi-stack generates `<workspace>.code-workspace` |
| `ow mv` / `ow archive` / `ow unarchive` / TUI | (removed) | Move with `mv`, then run `ow init` to repair worktrees and update the index |
| (none) | `ow run`, `ow stop`, `ow logs`, `ow exec`, `ow sandbox`, `ow trust` | Run, detach, follow and enter workspaces; sandbox agents; trust in-repo plugins. See [Commands](commands.md) |

## Behaviour changes to script against

- **No `--json`.** No command has a machine-readable mode; read the plain output, or the files ow writes. The JSON envelope that existed in earlier 3.0 alphas is gone.
- **Exit codes.** `ow run`, `ow exec` and `ow sandbox` return the child's exit code (`128 + n` on a signal, `130` on Ctrl+C); `ow logs` exits 1 when there is nothing to show. See [Commands](commands.md#exit-codes).
- **Plugin trust.** A repo can carry its own ow plugin (`ow_plugin.py`, or `[tool.ow] plugin` in `pyproject.toml`). It runs only after `ow trust`, which trusts a *location*: trusting a directory trusts any code that later appears under it, including a PR checked out with `ow add`. See [Plugins](plugins.md#trust).

## Environment variables & templates

- ow no longer reads `OW_WORKSPACE`; it walks up to `.ow/config.toml` or takes `-w/--workspace`. (`ow exec` and `ow sandbox` still set it for the process they run.)
- Templates (`[templates]`, `[vars]`) are gone; see [Generated files](files.md).
- `COMPOSE_FILE` is no longer set by ow; set it yourself (e.g. in `~/.config/mise/config.toml`) if needed.
- Sandboxes must mount the bare repos and shared venvs; see [Generated files](files.md#outside-the-workspace).

## Requirements

- **git ≥ 2.45** (`git cherry-pick --empty=drop` for `ow retarget`).
- **Python ≥ 3.11**.
- **`gh` (GitHub CLI)** for GitHub PR specs/status/pruning (or disable via `ow config set --global github.enabled false`).
- **Docker or Podman** (optional) for the [shared services](#services).
- **SSH access.** On a terminal, git's SSH and credential prompts pass through to you, and when several repos fetch in parallel, one that fails with an SSH key denial is retried on its own so the passphrase prompt is readable. Without a terminal (scripts, CI) nothing ever prompts: ow sets `GIT_TERMINAL_PROMPT=0` and `ssh -o BatchMode=yes`, unless you set `GIT_SSH_COMMAND`, `GIT_SSH` or a global `core.sshCommand` yourself. Load your key with `ssh-add` or configure a credential helper for unattended use.

## See also

- [Commands](commands.md) — full 3.0 command reference.
- [Configuration](configuration.md) — settings, layers and `ow config`.
- [Generated files](files.md) — generated files and ownership locks.
- [Plugins](plugins.md) — writing and shipping a plugin.
