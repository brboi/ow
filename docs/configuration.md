# Configuration

Settings control what ow generates and how plugins behave. They are key-value pairs, read with `ow config` and stored in TOML files.

## Layers

Four layers, later ones winning:

1. Built-in defaults, declared by each plugin and by ow itself (`python.*`).
2. Global config: `$XDG_CONFIG_HOME/ow/config.toml`, usually `~/.config/ow/config.toml`.
3. Workspace config: `<workspace>/.ow/config.toml`, shared by all stacks of a multi-stack workspace.
4. Stack config: `[stacks.<name>]` inside `<workspace>/.ow/config.toml`. Stacks hold no `.ow/` directory of their own.

A value set in any layer above the defaults is *explicit*. A plugin that also takes values from a service (the `odoo` plugin and the shared Postgres, for example) fills only the settings you left at their default. An explicit value always wins, even when it equals the default.

## `ow config`

### List every setting

```sh
ow config
```

One line per setting: `key = value (origin) # help`. `origin` is `default`, `global`, `workspace` or `stack`, the layer that supplied the value. Unset values print as `(unset)`. Strings are quoted, as TOML stores them. The listing starts with `python.*`, then follows plugin order.

```
odoo.http_port = 9069 (workspace) # HTTP port for Odoo
odoo.db_name = "ws" (default) # Database name (defaults to workspace name)
odoo.gevent_port = 8072 (default) # Gevent/longpolling port for Odoo
```

Outside a workspace the same listing works, with the workspace-derived defaults unset. `--global` skips the workspace layer: values come from the global config and the defaults.

A key that no loaded plugin declares is listed too, marked `# unknown (plugin not loaded?)`.

### Get

```sh
ow config get odoo.http_port     # effective value
ow config get --global python.version
```

A string prints raw, so `$(ow config get odoo.db_host)` is `localhost`, not `"localhost"`. An unset key prints nothing (exit 0). An unknown key is a usage error (exit 2).

### Set

```sh
ow config set odoo.http_port 9069
ow config set --global odoo.smtp_port 1025
```

Without `--global` it writes the workspace config and refreshes the generated files. A list setting takes one item per argument. A value that begins with `-` needs `--` after the key:

```sh
ow config set -w voip odoo.debug_args -- --dev=all --log-level debug
```

ow refuses an unknown key (exit 2) and suggests the closest known one. An `int` must parse as an integer. A `bool` must be `true` or `false`.

### Unset

```sh
ow config unset odoo.http_port
```

`unset` accepts an unknown key. It is the only command that removes one. `[repos]` is not a plugin setting: use `ow repo rm <name>` to remove a repo.

## File format

A mono-stack workspace's `.ow/config.toml` is TOML with a `[repos]` table (repo name → default base) and one table per plugin:

```toml
[repos]
community = "origin/master"

[odoo]
http_port = 9069
debug_args = ["--dev=all", "--log-level", "debug"]

[python]
version = "3.12"
```

ow validates each `[repos]` value against the bare repo's remotes. The longest remote-name prefix wins, and a bare ref with no known remote prefix reads as `origin/<name>`. A value ow cannot parse as a base is rejected with a warning that names it: a SHA, an empty or `refs/`-prefixed ref, an invalid branch name, a non-string. The `<base>..<feature>` format of 3.0.0a1 is rejected too: `ow init` migrates it ([Migrating](migrating-to-3.0.md#from-300a1-and-2x)).

A multi-stack workspace stores all its stacks in the single root `.ow/config.toml`, under `[stacks.<name>]`. Top-level plugin tables are workspace-wide settings shared by every stack. A stack's own tables override them:

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

There is no other metadata table. `[stacks]` decides whether a directory is a multi-stack workspace.

Every write rewrites the whole file. `[repos]` or `[stacks]` is kept, known values are grouped by plugin in declaration order, unset keys are left out, and unknown keys are carried over untouched. Writes are atomic: a `0600` temp file is renamed into place. An exclusive lock on a sidecar file next to the target serializes them: `<workspace>/.ow/.config.toml.lock` or `~/.config/ow/.config.toml.lock`. The config file itself is `0600`.

## Settings

`ow config` prints each setting with its help text. This table lists key, type and default. Every key is `<plugin>.<name>`. A plugin cannot declare a setting outside its own prefix, and a duplicate key is dropped with a warning ([Plugins](plugins.md#settings)). Every plugin also has `<plugin>.enabled` (bool, default `true`). A workspace disables a plugin locally with `<plugin>.enabled = false`.

| Key | Type | Default |
|---|---|---|
| `python.version` | str | none: the newest version the checkout supports |
| `python.shared_venv` | bool | `true` |
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
| `postgres.pgweb_image` | str | `"sosedoff/pgweb:0.17.0"` |
| `postgres.pgweb_port` | int | `8081` |
| `mailsink.image` | str | `"axllent/mailpit:v1.34.0"` |
| `mailsink.smtp_port` | int | `1025` |
| `mailsink.web_port` | int | `8025` |
| `github.default_org` | str | `"odoo"` |
| `github.cache_ttl` | int | `120` |
| `runbot.url` | str | `"https://runbot.odoo.com"` |
| `runbot.cache_ttl` | int | `120` |
| `sandbox.profile` | str | none |
| `sandbox.ro_paths` | list | `[]` |
| `sandbox.rw_paths` | list | `[]` |
| `sandbox.pass_env` | list | `[]` |

**`python.*`.** These belong to ow, which reserves the `python.` prefix. `python.version` is a `3.MINOR` string. It is clamped to the range the checkout supports. `python.shared_venv` shares virtualenvs under `$XDG_DATA_HOME/ow/venvs/py<ver>-<series>`. The sharing happens only when an enabled plugin can build the venv (today `mise`). Otherwise the venv is `<workspace>/.venv`. These settings were `mise.python` and `mise.shared_venv` in 3.0.0a1.

**Computed defaults.** `odoo.db_name`, `odoo.debug_args` and `odoo.debug_test_args` are computed from the workspace, so they read as `(unset)` outside one. `odoo.debug_args` also gets `--without-demo=all` when the Odoo core predates 19.

**Unique settings.** A setting declared `unique` must differ across workspaces. When ow creates a workspace or stack whose value is still the default and another indexed workspace already resolves to the same value, it writes `<value>-2` (next free suffix) into the new workspace's config. So `~/x/rev` and `~/y/rev` get the database names `rev` and `rev-2`, and `ow run --reset` in one cannot drop the other's database. A value you set yourself is never changed.

Disabling a plugin stops its generated files from being produced ([Generated files](files.md)). Its settings stay readable and stay in the file.

## Shared services

`ow run` and `ow sandbox` start three shared containers before they launch anything. They use Docker or Podman, whichever is ready. Every workspace on the machine shares them. They keep running until `ow stop --services` or `ow stop --all`.

| Container | Plugin | What | Where |
|---|---|---|---|
| `ow-postgres` | `postgres` | PostgreSQL (`postgres.image`) with its data in the named volume `ow-postgres-data` | host port `5432` if free, else `5433` (`postgres.port` pins it) |
| `ow-pgweb` | `postgres` | pgweb, a web UI on the database (`postgres.pgweb`) | `http://localhost:8081` |
| `ow-mailsink` | `mailsink` | Mailpit, which catches the mail Odoo sends | SMTP `1025`, UI `http://localhost:8025` |

The generated `odoorc` points at them (`db_host`, `db_port`, `db_user`, `db_password`, `smtp_server`, `smtp_port`) for every value you left at its default. ow uses `docker`, else `podman`. `OW_CONTAINER_ENGINE` forces one. Without a working engine ow warns (`docker/podman unavailable; skipping container startup`) and `odoorc` keeps the `odoo.db_*` values as configured. `ow exec` never starts the services.

**`postgres.*` details**

- `postgres.user` and `postgres.password` are the `NOSUPERUSER CREATEDB` role Odoo connects with.
- `postgres.port`: `0` picks `5432` when free, else `5433`. A number pins it.
- `postgres.enabled = false` stops ow from managing Postgres at all. Then `odoo.db_*` point at your own server.
- `postgres.bind_address` is the host address the container publishes Postgres on. pgweb and Mailpit always publish on `127.0.0.1`.

**Keep your own PostgreSQL.** If you already run a server, tell ow not to manage one and point Odoo at yours:

```sh
ow config set --global postgres.enabled false
ow config set --global odoo.db_host localhost
ow config set --global odoo.db_port 5432
ow config set --global odoo.db_user odoo
ow config set --global odoo.db_password odoo
```

Without `postgres.enabled = false`, ow starts `ow-postgres` next to yours (on `5433` when `5432` is taken). A default `odoo.db_port` then follows the container, and you land on an empty server. The same goes for Mailpit: set `mailsink.enabled = false` and an explicit `odoo.smtp_server` and `odoo.smtp_port`.

### Security: Postgres

`postgres.bind_address` defaults to `0.0.0.0`. The container publishes Postgres on every interface, because containers such as the Wazo stack of `voip` reach it through `host.docker.internal`. Any machine that can reach your host on that port can log in as the `odoo` role with `postgres.password`, which is `odoo` unless you changed it. That role is `CREATEDB` and owns the Odoo databases. This is a deliberate decision for 3.0.

To restrict it, bind to loopback, change the password, or both:

```sh
ow config set --global postgres.bind_address 127.0.0.1
ow config set --global postgres.password <something else>
```

The `postgres` superuser is a different role. Its password is random, generated once, kept in `$XDG_STATE_HOME/ow/postgres-admin-password` (mode `0600`) and applied to an existing `ow-postgres` container the next time it starts. ow gives it to the container through an `--env-file` of mode `0600`, never on a command line. ow administers the server through the container's local socket. The password is for plugins that connect over TCP as the superuser (`DatabaseService.admin_password`).

## Unknown keys and wrong types

ow warns once per config file and names every unknown and wrong-typed key:

```
warning: unknown setting future.x in .ow/config.toml; remove it with `ow config unset future.x`; odoo.http_port in .ow/config.toml must be int; using its default
```

- **An unknown key** is ignored until some ow version or plugin declares it. A plugin that is not loaded leaves its keys unknown: an in-repo plugin that is untrusted, or that fails to import. Every rewrite keeps the key. Only `ow config unset <key>` removes it.
- **A key of the wrong type** keeps its default. A rewrite drops it and prints `warning: dropped <key> = <value>`. An int is not a bool.
- **A renamed key** (`mise.python`, `mise.shared_venv`) is an error (exit 2): `mise.python was renamed to python.version`. Edit the file by hand.

## See also

- [Commands](commands.md): every command with its flags and exit codes.
- [Generated files](files.md): what settings produce, and how ownership works.
- [Plugins](plugins.md): declaring settings from a plugin.
