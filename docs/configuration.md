# Configuration

Settings control what ow generates and how plugins behave. They are key-value pairs, read with `ow config` and stored in TOML files.

## Layers

Four layers, later ones winning:

1. Built-in defaults, declared by each plugin.
2. Global config: `$XDG_CONFIG_HOME/ow/config.toml`, usually `~/.config/ow/config.toml`.
3. Workspace config: `<workspace>/.ow/config.toml` (shared across all stacks in a multi-stack workspace).
4. Stack config: `[stacks.<name>]` inside `<workspace>/.ow/config.toml` (stacks hold no `.ow/` directory of their own).

A value set in any layer above the defaults is *explicit*. Plugins that also take values from a service (for example the `odoo` plugin and the shared Postgres) only fill the settings you left at their default: an explicit value always wins, even when it equals the default.

## `ow config`

### List every setting

```sh
ow config
```

One line per setting: `key = value (origin) # help`. `origin` is `default`, `global`, `workspace` or `stack` (the layer that supplied the value). Unset values print as `(unset)`; strings are quoted, as TOML would store them.

```
odoo.http_port = 9069 (workspace) # HTTP port for Odoo
odoo.db_name = "ws" (default) # Database name (defaults to workspace name)
odoo.gevent_port = 8072 (default) # Gevent/longpolling port for Odoo
```

Outside a workspace the same listing works, with workspace-derived defaults unset. `--global` skips the workspace layer: values come from the global config and defaults only.

### Get

```sh
ow config get odoo.http_port     # effective value
ow config get --global mise.python
```

A string prints raw, so `$(ow config get odoo.db_host)` is `localhost`, not `"localhost"`. An unset key prints nothing (exit 0). An unknown key is a usage error (exit 2).

### Set

```sh
ow config set odoo.http_port 9069
ow config set --global odoo.smtp_port 1025
```

Without `--global` it writes the workspace config and refreshes the generated files. A list setting takes one item per argument; values beginning with `-` need `--` after the key:

```sh
ow config set -w voip odoo.debug_args -- --dev=all --log-level debug
```

An unknown key is refused (exit 2), with the closest known key suggested. An `int` must parse as an integer, a `bool` must be `true` or `false`.

### Unset

```sh
ow config unset odoo.http_port
```

An unknown key is accepted: unsetting is how you clear the unknown-key warning. `[repos]` is not a plugin setting; use `ow repo rm <name>` to remove a repo.

## File format

A mono-stack workspace's `.ow/config.toml` is TOML with a `[repos]` table (name → base ref) and one table per plugin:

```toml
[repos]
community = "origin/master"

[odoo]
http_port = 9069
debug_args = ["--dev=all", "--log-level", "debug"]

[mise]
python = "3.12"
```

Each `[repos]` value is validated against the bare repo's remotes: the longest remote-name prefix wins, and a bare ref with no known remote prefix is read as `origin/<name>`. A value ow cannot parse as a base — a SHA, an empty or `refs/`-prefixed ref, an invalid branch name, a non-string — is rejected and a warning names the value.

A multi-stack workspace stores all of its stacks in the single root `.ow/config.toml` under `[stacks.<name>]`; top-level plugin tables are workspace-wide settings shared by every stack, and a stack's own tables override them:

```toml
# Workspace-wide settings shared across stacks
[odoo]
db_host = "localhost"

[stacks.odoo.repos]
community = "origin/master"
enterprise = "origin/master"

[stacks.odoo.odoo]
http_port = 8069
gevent_port = 8072

[stacks.voipstack.repos]
voip = "origin/master"
```

There is no other metadata table: whether a directory is a multi-stack workspace is decided by `[stacks]`, and any other top-level table that is not a plugin's is an unknown key (below).

Every write rewrites the whole file: `[repos]` or `[stacks]` kept, known values grouped by plugin in declaration order, unset keys left out. Writes are atomic (a `0600` temp file renamed into place) and serialized by an exclusive lock on a sidecar file next to the target — `<workspace>/.ow/.config.toml.lock` or `~/.config/ow/.config.toml.lock`. The config file itself is `0600`.

## Settings

`ow config` prints each setting with its help text; this table lists only key, type and default. Every key is `<plugin>.<name>`: a plugin cannot declare a setting outside its own prefix, and a duplicate key is dropped with a warning ([Plugins](plugins.md#settings)). `<plugin>.enabled` (bool, default `true`) exists for every plugin; a workspace can disable a plugin locally with `<plugin>.enabled = false`.

| Key | Type | Default |
|---|---|---|
| `mise.python` | str | — |
| `mise.shared_venv` | bool | `true` |
| `odoo.http_port` | int | `8069` |
| `odoo.gevent_port` | int | `8072` |
| `odoo.db_name` | str, **unique** | workspace name |
| `odoo.db_host` | str | `"localhost"` |
| `odoo.db_port` | int | `5432` |
| `odoo.db_user` | str | `"odoo"` |
| `odoo.db_password` | str | `"odoo"` |
| `odoo.admin_passwd` | str | `"Password"` |
| `odoo.smtp_server` | str | `"localhost"` |
| `odoo.smtp_port` | int | `25` |
| `odoo.debug_args` | list | `["--dev=all"]` |
| `odoo.debug_test_args` | list | `["--test-tags=<workspace>"]` |
| `odoo.default_repos` | list | `["community", "enterprise"]` |
| `postgres.image` | str | `"pgvector/pgvector:pg17"` |
| `postgres.port` | int | `0` (auto: `5432` if free, else `5433`) |
| `postgres.bind_address` | str | `"0.0.0.0"` |
| `postgres.user` | str | `"odoo"` |
| `postgres.password` | str | `"odoo"` |
| `postgres.pgweb` | bool | `true` |
| `postgres.pgweb_image` | str | `"sosedoff/pgweb"` |
| `postgres.pgweb_port` | int | `8081` |
| `mailsink.image` | str | `"axllent/mailpit"` |
| `mailsink.smtp_port` | int | `1025` |
| `mailsink.web_port` | int | `8025` |
| `github.default_org` | str | `"odoo"` |
| `github.cache_ttl` | int | `120` |
| `runbot.url` | str | `"https://runbot.odoo.com"` |
| `runbot.cache_ttl` | int | `120` |
| `sandbox.profile` | str | — |
| `sandbox.ro_paths` | list | `[]` |
| `sandbox.rw_paths` | list | `[]` |
| `sandbox.pass_env` | list | `[]` |

`odoo.debug_args` also gets `--without-demo=all` when the Odoo core predates 19. `odoo.db_name` and `odoo.debug_test_args` are computed from the workspace, so they read as `(unset)` outside one.

**Unique settings.** A setting declared `unique` must differ across workspaces. When ow creates a workspace or stack whose value is still the default and another indexed workspace already resolves to the same value, it writes `<value>-2` (next free suffix) into the new workspace's config. So `~/x/rev` and `~/y/rev` get the database names `rev` and `rev-2`, and `ow run --reset` in one cannot drop the other's database. A value you set yourself is never changed.

**`postgres.*`.** These configure the shared Postgres container (see [Services](migrating-to-3.0.md#services)):

- `postgres.bind_address` — the host address the container publishes Postgres on. The default `0.0.0.0` exposes the server to the whole LAN (it is needed so containers such as Wazo's reach it through `host.docker.internal`); set `127.0.0.1` for loopback only. pgweb and mailsink always publish on `127.0.0.1`.
- `postgres.user` / `postgres.password` — the `NOSUPERUSER CREATEDB` role Odoo connects with. The `postgres` superuser is a different role with a random password kept in `$XDG_STATE_HOME/ow/postgres-admin-password` (mode `0600`).
- `postgres.port` — `0` picks `5432` when free, else `5433`; set a number to pin it.
- `postgres.enabled = false` stops ow from managing Postgres at all; then `odoo.db_*` point at your own server.

Disabling a plugin stops its generated files from being produced; see [Generated files](files.md). Its settings stay readable and are kept in the file.

## Unknown keys and wrong types

Both are kept in the file, and ow reports one warning per config file, naming every unknown key and every wrong-typed key:

```
warning: unknown setting future.x in .ow/config.toml; remove it with `ow config unset future.x`; odoo.http_port in .ow/config.toml must be int; using its default
```

- An unknown key is ignored until some ow version (or plugin) declares it. `ow config unset` clears it.
- A key whose TOML type does not match its declared type keeps its default. An int is not a bool.

Because a write rewrites the whole file, any key the rewrite does not carry over is dropped, and every dropped key is printed with its old value:

```
warning: dropped future.x = 1
```

## See also

- [Commands](commands.md) — every command with its flags and exit codes.
- [Generated files](files.md) — what settings produce, and how ownership works.
- [Plugins](plugins.md) — declaring settings from a plugin.
