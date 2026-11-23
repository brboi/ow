"""PostgreSQL (+ pgweb) plugin: global shared Postgres container auto-started on `ow run`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import subprocess
from typing import TYPE_CHECKING
from urllib.parse import quote

from ow import term
from ow.core import config
from ow.core.plugins import Setting, StatusInfo, hookimpl
from ow.errors import UsageError
from ow.plugins import _containers as containers
from ow.specs.services import DatabaseService

if TYPE_CHECKING:
    from ow.core.facts import RepoFacts
    from ow.core.workspace import Workspace

_PG_CONTAINER = "ow-postgres"
_PGWEB_CONTAINER = "ow-pgweb"
_PG_VOLUME = "ow-postgres-data"
_NETWORK = "ow-net"
_DEFAULT_IMAGE = "pgvector/pgvector:pg17"
_DEFAULT_PGWEB_IMAGE = "sosedoff/pgweb"
_DEFAULT_BIND_ADDRESS = "0.0.0.0"


@hookimpl
def ow_settings() -> list[Setting]:
    """Provide PostgreSQL and pgweb settings."""
    return [
        Setting(
            key="postgres.image",
            type=str,
            default=_DEFAULT_IMAGE,
            help="PostgreSQL container image (PG 17 + pgvector, compatible with Odoo 18/19/20.0)",
        ),
        Setting(
            key="postgres.port",
            type=int,
            default=0,
            help="Host port for PostgreSQL (0 = auto: 5432 if free, else 5433)",
        ),
        Setting(
            key="postgres.bind_address",
            type=str,
            default=_DEFAULT_BIND_ADDRESS,
            help="Host address to bind PostgreSQL on (0.0.0.0 allows containers via host.docker.internal; use 127.0.0.1 for loopback only)",
        ),
        Setting(
            key="postgres.user",
            type=str,
            default="odoo",
            help="PostgreSQL role name for Odoo (NOSUPERUSER CREATEDB)",
        ),
        Setting(
            key="postgres.password",
            type=str,
            default="odoo",
            help="PostgreSQL role password",
        ),
        Setting(
            key="postgres.pgweb",
            type=bool,
            default=True,
            help="Run pgweb alongside PostgreSQL",
        ),
        Setting(
            key="postgres.pgweb_image",
            type=str,
            default=_DEFAULT_PGWEB_IMAGE,
            help="pgweb container image",
        ),
        Setting(
            key="postgres.pgweb_port",
            type=int,
            default=8081,
            help="Host port for pgweb",
        ),
    ]


def _cfg(settings: Mapping[str, object]) -> tuple[str, int, str, str, str, bool, str, int]:
    image = str(settings.get("postgres.image") or _DEFAULT_IMAGE)
    raw_port = config.as_int(settings.get("postgres.port"))
    port = raw_port if raw_port is not None and 0 <= raw_port <= 65535 else 0
    bind_address = str(settings.get("postgres.bind_address") or _DEFAULT_BIND_ADDRESS).strip() or _DEFAULT_BIND_ADDRESS
    user = str(settings.get("postgres.user") or "odoo")
    password = str(settings.get("postgres.password") or "odoo")
    pgweb = bool(settings.get("postgres.pgweb", True))
    pgweb_image = str(settings.get("postgres.pgweb_image") or _DEFAULT_PGWEB_IMAGE)
    raw_pgweb_port = config.as_int(settings.get("postgres.pgweb_port"))
    pgweb_port = raw_pgweb_port if raw_pgweb_port is not None and 1 <= raw_pgweb_port <= 65535 else 8081
    return image, port, bind_address, user, password, pgweb, pgweb_image, pgweb_port


def _effective_port(settings: Mapping[str, object], engine: str | None) -> int:
    _image, port, bind_address, _user, _password, _pgweb, _pgweb_image, _pgweb_port = _cfg(settings)
    if port > 0:
        return port
    return containers.resolve_host_port(engine, _PG_CONTAINER, 5432, 5432, 5433, bind_address=bind_address)


@hookimpl
def database_service(ws: Workspace) -> DatabaseService | None:
    """Expose PostgreSQL connection details when a container engine is ready."""
    engine = containers.detect_engine()
    if engine is None or not containers.engine_ready(engine):
        return None
    _image, _port, _bind_address, user, password, pgweb, _pgweb_image, pgweb_port = _cfg(ws.settings)
    port = _effective_port(ws.settings, engine)
    return DatabaseService(
        host="localhost",
        port=port,
        user=user,
        password=password,
        admin_url=f"http://localhost:{pgweb_port}" if pgweb else None,
        drop=lambda db_name, _eng=engine: drop_database(_eng, db_name),
    )


def drop_database(engine: str, db_name: str) -> None:
    """Force-drop `db_name` inside the managed PostgreSQL container."""
    res = _psql(
        engine,
        _PG_ADMIN_USER,
        "postgres",
        f"DROP DATABASE IF EXISTS {_sql_ident(db_name)} WITH (FORCE);",
    )
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()
        raise UsageError(f"postgres: failed to drop database {db_name!r}: {detail}")


_PG_ADMIN_USER = "postgres"
_PG_MIGRATOR_USER = "ow_migrator"


def _sql_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _psql(engine: str, role: str, db: str, sql: str, *, tuples_only: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = ["psql", "-v", "ON_ERROR_STOP=1", "-U", role, "-d", db]
    if tuples_only:
        cmd.extend(["-t", "-A"])
    cmd.extend(["-c", sql])
    return containers.exec_container(engine, _PG_CONTAINER, cmd)


def _transfer_db_ownership(engine: str, db: str, user: str) -> None:
    """Transfer ownership of a migrated database and its OID-10 public-schema objects to `user`."""
    role_ident = _sql_ident(user)
    role_lit = _sql_literal(user)
    db_res = _psql(
        engine,
        _PG_ADMIN_USER,
        "postgres",
        "DO $$ BEGIN "
        f"IF EXISTS (SELECT 1 FROM pg_database WHERE datname = {_sql_literal(db)} AND datdba = 10) THEN "
        f"EXECUTE format('ALTER DATABASE %I OWNER TO %I', {_sql_literal(db)}, {role_lit}); "
        "END IF; END $$;",
    )
    if db_res.returncode != 0:
        detail = (db_res.stderr or db_res.stdout).strip()
        raise RuntimeError(f"failed to transfer database {db!r} ownership to {user!r}: {detail}")

    # Transfer tables/views first (ALTER TABLE automatically transfers linked SERIAL/IDENTITY
    # sequences), then standalone sequences (excluding deptype 'a'/'i'/'e' so PG never raises
    # "Sequence is linked to table"), then functions/procedures and custom types.
    transfer_sql = (
        "DO $$ DECLARE r RECORD; BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'public' AND nspowner = 10) THEN "
        f"EXECUTE format('ALTER SCHEMA public OWNER TO %I', {role_lit}); "
        "END IF; "
        "FOR r IN SELECT c.relname, c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relowner = 10 "
        "AND c.relkind IN ('r','p','v','m','f') "
        "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype = 'e') LOOP "
        "IF r.relkind = 'v' THEN "
        f"EXECUTE format('ALTER VIEW public.%I OWNER TO %I', r.relname, {role_lit}); "
        "ELSIF r.relkind = 'm' THEN "
        f"EXECUTE format('ALTER MATERIALIZED VIEW public.%I OWNER TO %I', r.relname, {role_lit}); "
        "ELSIF r.relkind = 'f' THEN "
        f"EXECUTE format('ALTER FOREIGN TABLE public.%I OWNER TO %I', r.relname, {role_lit}); "
        "ELSE "
        f"EXECUTE format('ALTER TABLE public.%I OWNER TO %I', r.relname, {role_lit}); "
        "END IF; "
        "END LOOP; "
        "FOR r IN SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relowner = 10 AND c.relkind = 'S' "
        "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = c.oid AND d.deptype IN ('a','i','e')) LOOP "
        f"EXECUTE format('ALTER SEQUENCE public.%I OWNER TO %I', r.relname, {role_lit}); "
        "END LOOP; "
        "FOR r IN SELECT p.oid::regprocedure AS sig, p.prokind FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proowner = 10 "
        "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e') LOOP "
        "IF r.prokind = 'p' THEN "
        f"EXECUTE format('ALTER PROCEDURE %s OWNER TO %I', r.sig, {role_lit}); "
        "ELSE "
        f"EXECUTE format('ALTER FUNCTION %s OWNER TO %I', r.sig, {role_lit}); "
        "END IF; "
        "END LOOP; "
        "FOR r IN SELECT t.typname FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE n.nspname = 'public' AND t.typowner = 10 AND t.typtype IN ('d','e','r') "
        "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = t.oid AND d.deptype = 'e') LOOP "
        f"EXECUTE format('ALTER TYPE public.%I OWNER TO %I', r.typname, {role_lit}); "
        "END LOOP; "
        "END $$;"
    )
    obj_res = _psql(engine, _PG_ADMIN_USER, db, transfer_sql)
    if obj_res.returncode != 0:
        detail = (obj_res.stderr or obj_res.stdout).strip()
        raise RuntimeError(f"failed to transfer objects in database {db!r} to {user!r}: {detail}")


def _transfer_legacy_databases(engine: str, user: str) -> None:
    """Ensure every non-template user DB owned by OID 10 or `user` has no leftover OID-10 objects."""
    role_lit = _sql_literal(user)
    dbs = _psql(
        engine,
        _PG_ADMIN_USER,
        "postgres",
        "SELECT datname FROM pg_database "
        "WHERE NOT datistemplate AND datname <> 'postgres' "
        f"AND (datdba = 10 OR datdba = (SELECT oid FROM pg_roles WHERE rolname = {role_lit}));",
        tuples_only=True,
    )
    if dbs.returncode != 0:
        return
    for db_name in (line.strip() for line in dbs.stdout.splitlines() if line.strip()):
        _transfer_db_ownership(engine, db_name, user)


def _migrate_bootstrap_role(engine: str, user: str, password: str) -> None:
    """Rename legacy bootstrap superuser `user` (OID 10) to `postgres` and recreate `user` non-superuser."""
    role_ident = _sql_ident(user)
    admin_ident = _sql_ident(_PG_ADMIN_USER)
    mig_ident = _sql_ident(_PG_MIGRATOR_USER)
    pw_lit = _sql_literal(password)

    # PostgreSQL forbids removing SUPERUSER from OID 10 and forbids renaming current_user,
    # so we create a short-lived superuser to rename OID 10 -> postgres and recreate `user`.
    create_mig = (
        "DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_sql_literal(_PG_MIGRATOR_USER)}) THEN "
        f"CREATE ROLE {mig_ident} WITH SUPERUSER LOGIN; "
        "ELSE "
        f"ALTER ROLE {mig_ident} WITH SUPERUSER LOGIN; "
        "END IF; "
        "END $$;"
    )
    res = _psql(engine, user, "postgres", create_mig)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()
        raise RuntimeError(f"failed to prepare bootstrap migration for role {user!r}: {detail}")

    rename_sql = (
        f"DROP ROLE IF EXISTS {admin_ident}; "
        f"ALTER ROLE {role_ident} RENAME TO {admin_ident}; "
        f"ALTER ROLE {admin_ident} WITH PASSWORD {pw_lit}; "
        f"CREATE ROLE {role_ident} WITH LOGIN NOSUPERUSER CREATEDB PASSWORD {pw_lit};"
    )
    renamed = _psql(engine, _PG_MIGRATOR_USER, "postgres", rename_sql)
    if renamed.returncode != 0:
        _psql(engine, user, "postgres", f"DROP ROLE IF EXISTS {mig_ident};")
        detail = (renamed.stderr or renamed.stdout).strip()
        raise RuntimeError(f"failed to migrate bootstrap role {user!r} to {_PG_ADMIN_USER!r}: {detail}")

    _psql(engine, _PG_ADMIN_USER, "postgres", f"DROP ROLE IF EXISTS {mig_ident};")
    _transfer_legacy_databases(engine, user)

    term.out(
        f"# postgres: migrated bootstrap superuser {user!r} to {_PG_ADMIN_USER!r} "
        f"and recreated {user!r} with NOSUPERUSER CREATEDB"
    )


def _ensure_odoo_role(engine: str, user: str, password: str) -> None:
    """Ensure `user` exists as a non-superuser role with CREATEDB (required by Odoo).

    Also migrates pre-existing `ow-postgres` containers created with `POSTGRES_USER=odoo`
    where `odoo` was the PostgreSQL bootstrap superuser (OID 10).
    """
    if user == _PG_ADMIN_USER:
        term.warn(
            "postgres: role 'postgres' is a superuser; Odoo refuses superuser roles "
            "(set `ow config set --global postgres.user odoo`)"
        )
        return

    role_ident = _sql_ident(user)
    role_lit = _sql_literal(user)
    pw_lit = _sql_literal(password)

    probe = _psql(
        engine,
        _PG_ADMIN_USER,
        "postgres",
        f"SELECT oid FROM pg_roles WHERE rolname = {role_lit};",
        tuples_only=True,
    )
    if probe.returncode != 0 or probe.stdout.strip() == "10":
        _migrate_bootstrap_role(engine, user, password)
        return

    ensure_sql = (
        "DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {role_lit}) THEN "
        f"CREATE ROLE {role_ident} WITH LOGIN NOSUPERUSER CREATEDB PASSWORD {pw_lit}; "
        "ELSE "
        f"ALTER ROLE {role_ident} WITH LOGIN NOSUPERUSER CREATEDB PASSWORD {pw_lit}; "
        "END IF; "
        "END $$;"
    )
    res = _psql(engine, _PG_ADMIN_USER, "postgres", ensure_sql)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout).strip()
        raise RuntimeError(f"failed to ensure non-superuser role {user!r}: {detail}")
    _transfer_legacy_databases(engine, user)


@hookimpl
def ow_before_run(ws: Workspace, workspaces: Sequence[Workspace]) -> list[str] | None:
    """Ensure the shared PostgreSQL (+ pgweb) container is running before `ow run`."""
    engine = containers.detect_engine()
    if engine is None or not containers.engine_ready(engine):
        term.warn("postgres: docker/podman unavailable; skipping container startup")
        return None

    image, _raw_port, bind_address, user, password, pgweb, pgweb_image, pgweb_port = _cfg(ws.settings)
    port = _effective_port(ws.settings, engine)

    try:
        containers.ensure_network(engine, _NETWORK)
        pg_state = containers.inspect_container(engine, _PG_CONTAINER)
        if pg_state is None or pg_state.status != "running":
            term.out(f"# postgres: starting {_PG_CONTAINER} ({image})...")
        elif pg_state.bind_addresses.get(5432) not in (None, bind_address) or pg_state.ports.get(5432) not in (
            None,
            port,
        ):
            term.out(f"# postgres: recreating {_PG_CONTAINER} ({image}) on {bind_address}:{port}...")
        action = containers.ensure_container(
            engine,
            _PG_CONTAINER,
            image=image,
            ports=((port, 5432),),
            env={
                "POSTGRES_USER": _PG_ADMIN_USER,
                "POSTGRES_PASSWORD": password,
                "POSTGRES_DB": "postgres",
            },
            volumes=((_PG_VOLUME, "/var/lib/postgresql/data"),),
            network=_NETWORK,
            bind_address=bind_address,
        )
        if action != "running":
            containers.wait_for_exec(engine, _PG_CONTAINER, ("pg_isready",), timeout=30.0)
            containers.wait_for_tcp("127.0.0.1", port, timeout=15.0)
        _ensure_odoo_role(engine, user, password)
    except RuntimeError as exc:
        term.warn(f"postgres: {exc}")
        return None

    note = f"postgres: localhost:{port}"
    if pgweb:
        safe_user = quote(user, safe="")
        safe_password = quote(password, safe="")
        db_url = f"postgres://{safe_user}:{safe_password}@{_PG_CONTAINER}:5432/postgres?sslmode=disable"
        try:
            pgweb_state = containers.inspect_container(engine, _PGWEB_CONTAINER)
            if pgweb_state is None or pgweb_state.status != "running":
                term.out(f"# postgres: starting {_PGWEB_CONTAINER} ({pgweb_image})...")
            containers.ensure_container(
                engine,
                _PGWEB_CONTAINER,
                image=pgweb_image,
                ports=((pgweb_port, 8081),),
                env={"PGWEB_DATABASE_URL": db_url},
                network=_NETWORK,
            )
            note += f" (pgweb: http://localhost:{pgweb_port})"
        except RuntimeError as exc:
            term.warn(f"postgres: pgweb failed to start: {exc}")

    return [note]


@hookimpl
def ow_stop_services(ws: Workspace | None) -> list[str] | None:
    """Stop shared PostgreSQL and pgweb containers (`ow stop --services` / `--all`)."""
    engine = containers.detect_engine()
    if engine is None or not containers.engine_ready(engine):
        return None
    notes: list[str] = []
    for name in (_PGWEB_CONTAINER, _PG_CONTAINER):
        try:
            if containers.stop_container(engine, name):
                notes.append(f"postgres: stopped {name}")
        except RuntimeError as exc:
            term.warn(f"postgres: {exc}")
    return notes or None


@hookimpl
def ow_status(
    ws: Workspace | None,
    facts: Sequence[RepoFacts],
    target: str | None,
    offline: bool,
    settings: Mapping[str, object],
) -> list[StatusInfo]:
    """Report shared PostgreSQL and pgweb status in `ow status`."""
    if target is not None or ws is None:
        return []
    engine = containers.detect_engine()
    if engine is None or not containers.engine_ready(engine):
        return []

    pg_state = containers.inspect_container(engine, _PG_CONTAINER)
    if pg_state is None:
        return []

    _image, _raw_port, _bind_address, _user, _password, pgweb, _pgweb_image, pgweb_port = _cfg(ws.settings)
    port = pg_state.ports.get(5432) or _effective_port(ws.settings, engine)
    data: dict[str, object] = {"status": pg_state.status, "host": "localhost", "port": port}
    summary = f"{pg_state.status} on localhost:{port}"

    if pgweb:
        pgweb_state = containers.inspect_container(engine, _PGWEB_CONTAINER)
        if pgweb_state is not None and pgweb_state.status == "running":
            web_port = pgweb_state.ports.get(8081, pgweb_port)
            url = f"http://localhost:{web_port}"
            data["pgweb_url"] = url
            summary += f" (pgweb: {url})"

    return [
        StatusInfo(
            plugin="postgres",
            scope="service",
            target="postgres",
            summary=summary,
            data=data,
        )
    ]
