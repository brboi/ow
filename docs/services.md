# Services

Odoo needs PostgreSQL, and Mailpit catches outgoing email while you test. This page is a copy-paste Docker Compose stack for both, and how to point a workspace at it. It is optional: Odoo works with any PostgreSQL installation and any SMTP provider, and ow never reads, writes or deletes the compose file or its volumes.

## The stack

Save this as `compose.yml`, in a workspace or anywhere else:

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg17
    restart: unless-stopped
    environment:
      POSTGRES_USER: odoo
      POSTGRES_PASSWORD: odoo
      POSTGRES_DB: odoo
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

  mailpit:
    image: axllent/mailpit:latest
    restart: unless-stopped
    ports:
      - "8025:8025"
      - "1025:1025"
    volumes:
      - mailpit_data:/data
    environment:
      MP_MAX_MESSAGES: "5000"
      MP_DATABASE: "/data/mailpit.db"

volumes:
  pgdata:
  mailpit_data:
```

- `pgvector/pgvector:pg17` is PostgreSQL 17 with the pgvector extension. It listens on `localhost:5432`.
- Mailpit serves its web UI on `http://localhost:8025` and SMTP on `localhost:1025`.

Start it from the directory holding the file:

```sh
docker compose up -d
```

## Point a workspace at the stack

The stack creates a database named `odoo`. `odoo.db_name` defaults to the workspace name, so the two disagree. From the workspace, either use the database the stack made:

```sh
ow config set odoo.db_name odoo
```

or, from the compose project directory, create one named after the workspace and keep the default:

```sh
docker compose exec postgres createdb -U odoo <workspace>
```

`odoo.db_host`, `odoo.db_user` and `odoo.db_password` already default to `localhost`, `odoo` and `odoo`, so they need no change.

## Mail through Mailpit

`odoorc` already carries `smtp_server` and `smtp_port` from the settings. Point the port at Mailpit:

```sh
ow config set --global odoo.smtp_port 1025
```

## COMPOSE_FILE

Keep the stack in a fixed place and tell compose about it once, in your global mise config at `~/.config/mise/config.toml`:

```toml
[env]
COMPOSE_FILE = "/path/to/compose.yml"
```

`docker compose` then works from anywhere. If you move workspaces around, pass `-f <file>` instead.

## Coming from ow 2

ow 2 kept its compose file at `$XDG_CONFIG_HOME/ow/services/compose.yml` and its data in `$XDG_DATA_HOME/ow/volumes/` (`postgres-data/`, `mailpit-data/`), with a database named `postgres`. ow 3 never reads, writes or deletes any of it: the file and the data are yours.

The stack above uses named volumes and a database named `odoo`, so it starts with an empty database and never sees the old data. To reuse it, bind-mount the old directories instead:

```yaml
    volumes:
      - "${XDG_DATA_HOME:-$HOME/.local/share}/ow/volumes/postgres-data:/var/lib/postgresql/data"
      - "${XDG_DATA_HOME:-$HOME/.local/share}/ow/volumes/mailpit-data:/data"
```

and point Odoo at the old database:

```sh
ow config set odoo.db_name postgres
```

## See also

- [Configuration](configuration.md) — the `odoo.*` settings, their defaults and their layers.
- [Migrating to 3.0](migrating-to-3.0.md) — what to do with an ow 2 compose file and its volumes.
