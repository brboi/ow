"""Tests for the built-in `postgres` (+ pgweb) and `mailsink` (Mailpit) plugins."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

from ow.core import paths
from ow.core.workspace import Workspace
from ow.plugins import _containers as containers
from ow.plugins import mailsink, postgres
from tests.conftest import add_repo, init_workspace
from tests.lab import make_fake_engine


def test_postgres_auto_port_falls_back_to_5433_when_5432_is_busy(monkeypatch):
    make_fake_engine(monkeypatch)
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": port != 5432)

    ws = Workspace(root=Path("/tmp/ws"), name="ws", repos=(), settings={"postgres.port": 0})
    svc = postgres.database_service(ws)
    assert svc is not None
    assert svc.port == 5433
    assert svc.web_url == "http://localhost:8081"


def test_postgres_before_run_creates_network_postgres_and_pgweb(monkeypatch):
    calls, running = make_fake_engine(monkeypatch)
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": True)

    ws = Workspace(root=Path("/tmp/ws"), name="ws", repos=(), settings={})
    notes = postgres.ow_before_run(ws, (ws,))
    assert notes == ["postgres: localhost:5432 (pgweb: http://localhost:8081)"]
    assert running["ow-postgres"] == {5432: 5432}
    assert running["ow-pgweb"] == {8081: 8081}
    assert any(c[:3] == ["docker", "network", "create"] for c in calls)
    pg_run = next(c for c in calls if c[:2] == ["docker", "run"] and "ow-postgres" in c)
    assert "POSTGRES_USER=postgres" in pg_run
    assert "0.0.0.0:5432:5432" in pg_run
    pgweb_run = next(c for c in calls if c[:2] == ["docker", "run"] and "ow-pgweb" in c)
    assert "127.0.0.1:8081:8081" in pgweb_run
    role_execs = [c for c in calls if c[:3] == ["docker", "exec", "ow-postgres"] and "psql" in c]
    assert any("NOSUPERUSER CREATEDB" in c[-1] for c in role_execs)


def test_postgres_before_run_migrates_legacy_superuser_odoo_role(monkeypatch):
    calls, _running = make_fake_engine(monkeypatch, running={"ow-postgres": {5432: 5432}})
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": True)

    migrated = False

    def fake_exec(engine, name, cmd, timeout=15.0):
        nonlocal migrated
        calls.append([engine, "exec", name, *cmd])
        user = cmd[cmd.index("-U") + 1]
        sql = cmd[-1]
        # Before migration, `postgres` role does not exist yet (legacy container where OID 10 is `odoo`).
        if user == "postgres" and not migrated:
            return subprocess.CompletedProcess([engine, "exec", name, *cmd], 2, "", 'role "postgres" does not exist')
        if user == "ow_migrator" and 'RENAME TO "postgres"' in sql:
            migrated = True
            return subprocess.CompletedProcess([engine, "exec", name, *cmd], 0, "", "")
        if "FROM pg_database" in sql:
            return subprocess.CompletedProcess([engine, "exec", name, *cmd], 0, "voip\n", "")
        return subprocess.CompletedProcess([engine, "exec", name, *cmd], 0, "", "")

    monkeypatch.setattr(containers, "exec_container", fake_exec)
    ws = Workspace(root=Path("/tmp/ws"), name="ws", repos=(), settings={"postgres.pgweb": False})

    notes = postgres.ow_before_run(ws, (ws,))
    assert notes == ["postgres: localhost:5432"]
    exec_calls = [c for c in calls if c[:3] == ["docker", "exec", "ow-postgres"]]
    assert any(c[c.index("-U") + 1] == "odoo" and 'CREATE ROLE "ow_migrator"' in c[-1] for c in exec_calls)
    assert any(
        c[c.index("-U") + 1] == "ow_migrator"
        and 'ALTER ROLE "odoo" RENAME TO "postgres"' in c[-1]
        and 'CREATE ROLE "odoo" WITH LOGIN NOSUPERUSER CREATEDB' in c[-1]
        for c in exec_calls
    )
    assert any(c[c.index("-U") + 1] == "postgres" and 'DROP ROLE IF EXISTS "ow_migrator"' in c[-1] for c in exec_calls)
    assert any("ALTER DATABASE %I OWNER TO %I" in c[-1] and "'voip'" in c[-1] for c in exec_calls)
    assert any(
        c[c.index("-d") + 1] == "voip"
        and "deptype IN ('a','i','e')" in c[-1]
        and "ALTER SEQUENCE public.%I OWNER TO %I" in c[-1]
        for c in exec_calls
    )


def test_postgres_respects_pgweb_disabled(monkeypatch):
    _calls, running = make_fake_engine(monkeypatch)
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": True)

    ws = Workspace(root=Path("/tmp/ws"), name="ws", repos=(), settings={"postgres.pgweb": False})
    notes = postgres.ow_before_run(ws, (ws,))
    assert notes == ["postgres: localhost:5432"]
    assert "ow-postgres" in running
    assert "ow-pgweb" not in running


def test_mailsink_before_run_and_status(monkeypatch):
    _calls, running = make_fake_engine(monkeypatch)
    ws = Workspace(root=Path("/tmp/ws"), name="ws", repos=(), settings={})

    notes = mailsink.ow_before_run(ws, (ws,))
    assert notes == ["mailsink: smtp://localhost:1025 (ui: http://localhost:8025)"]
    assert running["ow-mailsink"] == {1025: 1025, 8025: 8025}

    infos = mailsink.ow_status(ws, (), None, True, ws.settings)
    assert len(infos) == 1
    assert "running on smtp://localhost:1025" in infos[0].summary
    assert "http://localhost:8025" in infos[0].summary


def test_odoorc_picks_up_postgres_and_mailsink_ports_unless_overridden(lab, monkeypatch):
    make_fake_engine(monkeypatch)
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": port != 5432)

    add_repo(lab, "community", {"18.0": lab.odoo_files(series="18.0")})
    ws = init_workspace(lab, "community", name="svc-ws")

    odoorc = (ws / "odoorc").read_text()
    assert "db_port = 5433" in odoorc
    assert "smtp_port = 1025" in odoorc

    # Explicit odoo.db_port override wins over postgres plugin default.
    assert lab.ow("config", "set", "odoo.db_port", "5499", cwd=ws).exit_code == 0
    odoorc2 = (ws / "odoorc").read_text()
    assert "db_port = 5499" in odoorc2
    assert "smtp_port = 1025" in odoorc2

    # An explicit value equal to the default is still explicit: it wins over the service.
    assert lab.ow("config", "set", "odoo.db_port", "5432", cwd=ws).exit_code == 0
    odoorc3 = (ws / "odoorc").read_text()
    assert "db_port = 5432" in odoorc3
    assert "smtp_port = 1025" in odoorc3


def test_before_run_warns_without_blocking_when_engine_missing(monkeypatch, capsys):
    monkeypatch.setattr(containers, "detect_engine", lambda: None)
    ws = Workspace(root=Path("/tmp/ws"), name="ws", repos=(), settings={})

    assert postgres.ow_before_run(ws, (ws,)) is None
    assert mailsink.ow_before_run(ws, (ws,)) is None
    err = capsys.readouterr().err
    assert "postgres: docker/podman unavailable" in err
    assert "mailsink: docker/podman unavailable" in err


def test_stopped_postgres_container_recreated_on_fallback_port_when_5432_is_busy(monkeypatch):
    """When stopped `ow-postgres` is bound to busy 5432, it is recreated on 5433 preserving its volume."""
    stopped_ports: dict[str, dict[int, int]] = {"ow-postgres": {5432: 5432}}
    running_map: dict[str, dict[int, int]] = {}
    calls: list[list[str]] = []

    monkeypatch.setattr(containers, "detect_engine", lambda: "podman")
    containers.engine_ready.cache_clear()
    monkeypatch.setattr(containers, "engine_ready", lambda engine: True)
    monkeypatch.setattr(containers, "wait_for_tcp", lambda host, port, timeout=15.0: True)
    monkeypatch.setattr(containers, "wait_for_exec", lambda engine, name, cmd, timeout=30.0: True)
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": port != 5432)

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        cmd = argv[1:]
        if cmd[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if cmd[0] == "inspect":
            name = cmd[1]
            if name in running_map:
                ports = {
                    f"{c}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(h)}]
                    for c, h in running_map[name].items()
                }
                payload = [{"State": {"Running": True}, "NetworkSettings": {"Ports": ports}}]
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
            if name in stopped_ports:
                bindings = {
                    f"{c}/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(h)}]
                    for c, h in stopped_ports[name].items()
                }
                payload = [
                    {
                        "State": {"Running": False},
                        "NetworkSettings": {"Ports": {}},
                        "HostConfig": {"PortBindings": bindings},
                    }
                ]
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="No such object")
        if cmd[0] == "rm":
            stopped_ports.pop(cmd[1], None)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if cmd[0] == "run":
            name = cmd[cmd.index("--name") + 1]
            ports: dict[int, int] = {}
            for i, tok in enumerate(cmd):
                if tok == "-p" and i + 1 < len(cmd):
                    parts = cmd[i + 1].split(":")
                    ports[int(parts[-1])] = int(parts[-2])
            running_map[name] = ports
            return subprocess.CompletedProcess(argv, 0, stdout="cid\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(containers.subprocess, "run", fake_run)
    ws = Workspace(root=Path("/tmp/ws"), name="ws", repos=(), settings={"postgres.pgweb": False})

    notes = postgres.ow_before_run(ws, (ws,))
    assert notes == ["postgres: localhost:5433"]
    assert ["podman", "rm", "ow-postgres"] in calls
    assert running_map["ow-postgres"] == {5432: 5433}
    pg_run = next(c for c in calls if c[:2] == ["podman", "run"] and "ow-postgres" in c)
    assert "ow-postgres-data:/var/lib/postgresql/data" in pg_run
    assert "0.0.0.0:5433:5432" in pg_run


def test_running_postgres_container_recreated_when_bind_address_changes(monkeypatch):
    """When running `ow-postgres` is bound to 127.0.0.1, switching to 0.0.0.0 stops, removes, and recreates it."""
    calls, running = make_fake_engine(monkeypatch,
    running={"ow-postgres": {5432: 5433}},
    running_ips={"ow-postgres": {5432: "127.0.0.1"}},)
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": port != 5432)

    ws = Workspace(root=Path("/tmp/ws"), name="ws", repos=(), settings={"postgres.pgweb": False})
    notes = postgres.ow_before_run(ws, (ws,))
    assert notes == ["postgres: localhost:5433"]
    assert ["docker", "stop", "ow-postgres"] in calls
    assert ["docker", "rm", "ow-postgres"] in calls
    pg_run = next(c for c in calls if c[:2] == ["docker", "run"] and "ow-postgres" in c)
    assert "0.0.0.0:5433:5432" in pg_run
    assert "ow-postgres-data:/var/lib/postgresql/data" in pg_run
    assert running["ow-postgres"] == {5432: 5433}


def _state_password() -> str:
    return paths.postgres_admin_password_file().read_text(encoding="utf-8").strip()


def test_new_postgres_container_gets_random_superuser_password_from_state(monkeypatch):
    calls, _running = make_fake_engine(monkeypatch)
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": True)
    ws = Workspace(root=Path("/tmp/ws"), name="ws", repos=(), settings={"postgres.pgweb": False})

    postgres.ow_before_run(ws, (ws,))

    state_file = paths.postgres_admin_password_file()
    secret = _state_password()
    assert len(secret) >= 32
    assert secret != "odoo"
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    pg_run = next(c for c in calls if c[:2] == ["docker", "run"] and "ow-postgres" in c)
    assert f"POSTGRES_PASSWORD={secret}" in pg_run
    # The password is generated once: a later start reuses it.
    postgres.ow_before_run(ws, (ws,))
    assert _state_password() == secret


def test_existing_postgres_container_gets_alter_role_with_state_password(monkeypatch):
    calls, _running = make_fake_engine(monkeypatch, running={"ow-postgres": {5432: 5432}})
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": True)
    ws = Workspace(root=Path("/tmp/ws"), name="ws", repos=(), settings={"postgres.pgweb": False})

    postgres.ow_before_run(ws, (ws,))

    secret = _state_password()
    assert not any(c[:2] == ["docker", "run"] and "ow-postgres" in c for c in calls)
    alters = [
        c
        for c in calls
        if c[:3] == ["docker", "exec", "ow-postgres"]
        and c[c.index("-U") + 1] == "postgres"
        and c[-1] == f"ALTER ROLE \"postgres\" WITH PASSWORD '{secret}';"
    ]
    assert len(alters) == 1
    # The Odoo role keeps `postgres.password`.
    assert any("CREATE ROLE \"odoo\"" in c[-1] and "PASSWORD 'odoo'" in c[-1] for c in calls if "psql" in c)


def test_database_service_exposes_admin_credentials(monkeypatch):
    make_fake_engine(monkeypatch)
    monkeypatch.setattr(containers, "is_port_free", lambda port, host="127.0.0.1": True)
    ws = Workspace(root=Path("/tmp/ws"), name="ws", repos=(), settings={"postgres.password": "role-pw"})

    svc = postgres.database_service(ws)

    assert svc is not None
    assert (svc.user, svc.password) == ("odoo", "role-pw")
    assert svc.admin_user == "postgres"
    assert svc.admin_password == _state_password()
    assert svc.admin_password != svc.password


def test_detect_container_engine_names_the_engine_even_when_configured_with_a_path(monkeypatch, tmp_path):
    fake = tmp_path / "engines" / "podman"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("OW_CONTAINER_ENGINE", str(fake))

    # The binary to invoke stays the configured one; the public name is `podman`.
    assert containers.detect_engine() == str(fake)
    assert containers.detect_container_engine() == "podman"

    monkeypatch.setenv("OW_CONTAINER_ENGINE", "not-installed-anywhere")
    assert containers.detect_engine() is None
    assert containers.detect_container_engine() is None


def test_plugin_api_exposes_the_normalized_engine_name():
    from ow import plugin_api

    assert plugin_api.detect_container_engine is containers.detect_container_engine
