# Services

`ow` packages a Docker Compose stack (postgres, pgweb, mailpit) for local development.
No `ow` command starts or stops it — you drive it with plain `docker compose` — but `ow init`
and `ow apply` render the compose file into `$XDG_CONFIG_HOME/ow/services/compose.yml` for you,
with the container volume path baked in. The generated `mise.toml` exports `COMPOSE_FILE` so
`docker compose up` works from inside any workspace:

```sh
docker compose up -d      # from inside a workspace (COMPOSE_FILE is set)
```

| Service | Port | Description |
|---------|------|-------------|
| postgres | 5432 | PostgreSQL 17 with pgvector |
| pgweb | 8081 | Web-based PostgreSQL browser |
| mailpit | 8025 / 1025 | Email testing (web UI / SMTP) |

The generated `odoorc` sets `data_dir = <workspace>/.odoo`, so each workspace gets its own
filestore — `rm -rf <workspace>` cleans it up. Point Odoo at the services via `[vars]`,
either globally or per workspace:

```toml
[vars]
db_host = "localhost"
db_port = 5432
smtp_server = "localhost"
smtp_port = 1025
```
