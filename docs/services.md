# Services

`ow` writes one Compose file for a local development stack — postgres with pgvector, pgweb, and
Mailpit — and nothing more. No `ow` command starts, stops or upgrades a container: you drive the
stack with plain `docker compose`, and `ow` only keeps the file current.

## The file

`$XDG_CONFIG_HOME/ow/services/compose.yml`. It is written by a render that found a **supported**
Odoo core in the workspace, and only then: a generic workspace, or one whose core major ow does
not support, leaves the services directory alone. It is machine-wide, not per workspace — it is
not one of the generated files listed in [Generated files](files.md), is not tracked in any
workspace's rendered lock, and can be regenerated at any time.

The file is **JSON**, despite the `.yml` name — Compose reads JSON as YAML. That is deliberate:
the postgres and Mailpit volumes are bind mounts of absolute paths under
`$XDG_DATA_HOME/ow/volumes/`, and JSON's long bind syntax (`{"type": "bind", "source": ...,
"target": ...}`) carries a colon or a quote in a path as a string, where the `host:container`
shorthand could not.

```json
{
  "services": {
    "postgres": { "image": "pgvector/pgvector:pg17", "ports": ["5432:5432"] },
    "mailpit": { "image": "axllent/mailpit", "ports": ["8025:8025", "1025:1025"] },
    "pgweb": { "image": "sosedoff/pgweb", "ports": ["8081:8081"] }
  }
}
```

| Service | Image | Ports | Volume |
|---------|-------|-------|--------|
| postgres | `pgvector/pgvector:pg17` | `5432:5432` | `$XDG_DATA_HOME/ow/volumes/postgres-data` |
| mailpit | `axllent/mailpit` | `8025:8025` (web UI), `1025:1025` (SMTP) | `$XDG_DATA_HOME/ow/volumes/mailpit-data` |
| pgweb | `sosedoff/pgweb` | `8081:8081` | — |

The generated `mise/conf.d/00-ow.toml` exports `COMPOSE_FILE` pointing at that path whenever the
workspace has an Odoo core, so `docker compose up -d` works from inside any workspace without
naming the file. The generated `odoorc` sets `data_dir = <workspace>/.odoo`, so each workspace
gets its own filestore — `rm -rf <workspace>` cleans it up.

Equal bytes are left alone, mtime included; a real change lands atomically, so Compose never reads
a half-written file. A `compose.yml` or services directory ow cannot safely replace — a symlink, a
path that is not a directory — is an error naming it rather than a write through the conflict.

## Pointing Odoo at it

The connection defaults already match the stack: `db_host = localhost`, `db_port = 5432`,
`db_user = odoo`, `db_password = odoo`, `smtp_server = localhost`. Only the SMTP port needs a
decision: Odoo's own default is `25`, and Mailpit listens on `1025`, so pointing Odoo at the
bundled Mailpit is an explicit opt-in:

```toml
[odoo]
smtp_port = 1025
```

Set it in the global config to apply everywhere, or in a workspace's `.ow/config.toml` to apply
there. See [Configuration](configuration.md).

## ow-owned versus your own

The file at `$XDG_CONFIG_HOME/ow/services/compose.yml` is ow's: a render overwrites it whenever
the document changes, so edits made there do not survive. If you need a customized stack — extra
services, different ports, a non-bind volume — keep your own compose file somewhere else and point
`docker compose -f` at it. Nothing in `ow` reads that file; the only thing ow guarantees is the one
it writes.
