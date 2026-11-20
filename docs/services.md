# Services

This is a copy-paste Docker Compose recipe for a local PostgreSQL + pgvector + Mailpit stack, providing a database for Odoo and an SMTP server for outgoing email testing. This is entirely optional: Odoo works with any PostgreSQL installation and any SMTP provider.

## Docker Compose Stack

Save this as `compose.yml` in your workspace root or anywhere you like:

```yaml
version: '3.8'

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

Run it once to start the services:

```sh
docker compose -f <file> up -d
```

The database listens on `localhost:5432`. Mailpit's web UI is at `http://localhost:8025`, and its SMTP server on `localhost:1025`.

## Connecting Odoo to the Stack

In your ow configuration, point Odoo to the database:

```sh
ow config set odoo.db_host localhost
ow config set odoo.db_user odoo
ow config set odoo.db_password odoo
```

To opt in to mail via Mailpit:

```sh
ow config set --global odoo.smtp_port 1025
```

Then configure Odoo's SMTP server via the Settings > General > Discuss section as usual, pointing at `localhost:1025` with no auth required.

## COMPOSE_FILE Tip

If you're running the stack in a fixed location, you can tell compose to use it automatically by setting an environment variable in your global mise configuration. Add to `~/.config/mise/config.toml`:

```toml
[env]
COMPOSE_FILE = "/path/to/compose.yml"
```

Then `docker compose` commands work from anywhere and always use that file. If you move workspaces around, use `-f` to specify the file explicitly instead.

## Your Compose File is Yours

If you had an existing `~/.config/ow/services/compose.yml` from ow 2, it is now your file to keep. The `volumes/` directory holds your database. ow 3 never manages these: they are entirely in your control.
