"""The machine-wide compose file: fixed service data, exact bytes, mtime kept."""

import json
import os

from ow.utils import paths
from ow.utils.services import compose_bytes, compose_document, ensure_services_compose

import pytest


def test_compose_document_describes_the_fixed_services(tmp_path):
    volumes = tmp_path / "volumes"

    services = compose_document(volumes)["services"]

    assert sorted(services) == ["mailpit", "pgweb", "postgres"]
    assert services["postgres"]["image"] == "pgvector/pgvector:pg17"
    assert services["postgres"]["ports"] == ["5432:5432"]
    assert services["postgres"]["environment"] == {
        "POSTGRES_USER": "odoo",
        "POSTGRES_PASSWORD": "odoo",
        "POSTGRES_DB": "postgres",
    }
    assert services["postgres"]["volumes"] == [
        {
            "type": "bind",
            "source": str(volumes / "postgres-data"),
            "target": "/var/lib/postgresql/data",
        }
    ]
    assert services["mailpit"]["image"] == "axllent/mailpit"
    assert services["mailpit"]["ports"] == ["8025:8025", "1025:1025"]
    assert services["mailpit"]["volumes"] == [
        {"type": "bind", "source": str(volumes / "mailpit-data"), "target": "/data"}
    ]
    assert services["pgweb"]["image"] == "sosedoff/pgweb"
    assert services["pgweb"]["ports"] == ["8081:8081"]
    assert services["pgweb"]["environment"] == {
        "DATABASE_URL": "postgres://odoo:odoo@postgres:5432/postgres?sslmode=disable"
    }
    assert services["pgweb"]["depends_on"] == ["postgres"]
    assert {name: service["restart"] for name, service in services.items()} == {
        "postgres": "unless-stopped",
        "mailpit": "unless-stopped",
        "pgweb": "unless-stopped",
    }


def test_compose_bytes_is_one_json_document_ending_in_a_newline(tmp_path):
    volumes = tmp_path / "volumes"

    data = compose_bytes(volumes)

    assert data.endswith(b"\n")
    assert json.loads(data.decode("utf-8")) == compose_document(volumes)


def test_a_volumes_root_with_a_colon_and_a_quote_round_trips(tmp_path):
    """Long bind syntax, read as JSON: neither the colon nor the quote splits a path."""
    volumes = tmp_path / 'weir:d "quoted"'

    document = json.loads(compose_bytes(volumes).decode("utf-8"))

    assert document["services"]["postgres"]["volumes"] == [
        {
            "type": "bind",
            "source": f'{volumes}/postgres-data',
            "target": "/var/lib/postgresql/data",
        }
    ]
    assert document["services"]["mailpit"]["volumes"][0]["source"] == f"{volumes}/mailpit-data"


def test_ensure_writes_the_compose_file_under_the_services_directory(xdg):
    destination = ensure_services_compose()

    assert destination == paths.services_dir() / "compose.yml"
    assert destination.read_bytes() == compose_bytes(paths.volumes_dir())
    assert [path.name for path in destination.parent.iterdir()] == ["compose.yml"]


def test_equal_bytes_leave_the_file_and_its_mtime_alone(xdg):
    destination = ensure_services_compose()
    stamp = 1_000_000_000 * 10**9
    os.utime(destination, ns=(stamp, stamp))

    assert ensure_services_compose() == destination

    assert destination.stat().st_mtime_ns == stamp


def test_changed_content_is_replaced_without_leaving_temporaries(xdg):
    destination = ensure_services_compose()
    destination.write_bytes(b"{}\n")

    assert ensure_services_compose() == destination

    assert destination.read_bytes() == compose_bytes(paths.volumes_dir())
    assert [path.name for path in destination.parent.iterdir()] == ["compose.yml"]


def test_a_symlinked_services_directory_is_refused(xdg, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    services = paths.services_dir()
    services.parent.mkdir(parents=True)
    services.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(ValueError) as caught:
        ensure_services_compose()

    assert str(services) in str(caught.value)
    assert list(elsewhere.iterdir()) == []


def test_a_symlinked_compose_file_is_refused(xdg, tmp_path):
    target = tmp_path / "target.yml"
    target.write_bytes(b"mine\n")
    destination = paths.services_dir() / "compose.yml"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(target)

    with pytest.raises(ValueError) as caught:
        ensure_services_compose()

    assert str(destination) in str(caught.value)
    assert target.read_bytes() == b"mine\n"


def test_a_services_path_that_is_a_file_is_refused(xdg):
    services = paths.services_dir()
    services.parent.mkdir(parents=True, exist_ok=True)
    services.write_bytes(b"not a directory\n")

    with pytest.raises(ValueError) as caught:
        ensure_services_compose()

    assert str(services) in str(caught.value)