"""The `local` plugin seeds `.local/` on `ow init` workspace creation."""

from __future__ import annotations

import configparser

from tests.conftest import add_repo


def _seed(lab):
    seed = lab.home / ".config" / "ow" / "local"
    seed.mkdir(parents=True, exist_ok=True)
    return seed


def test_seed_copies_before_the_first_refresh(lab):
    seed = _seed(lab)
    (seed / "l10n_seed").mkdir()
    (seed / "l10n_seed" / "__manifest__.py").write_text("{'name': 'Seed'}")
    add_repo(lab)
    ws = lab.ws("voip")
    ws.mkdir(parents=True, exist_ok=True)

    result = lab.ow("init", "community@18.0", cwd=ws)

    assert result.exit_code == 0, result.stderr + result.stdout
    assert (ws / ".local" / "l10n_seed" / "__manifest__.py").is_file()
    parser = _odoorc(ws)
    assert parser["options"]["addons_path"].startswith(str(ws / ".local"))


def test_a_disabled_local_plugin_does_not_seed(lab):
    seed = _seed(lab)
    (seed / "first").mkdir()
    (seed / "first" / "__manifest__.py").write_text("{}")
    add_repo(lab)
    ws = lab.ws("voip")
    ws.mkdir(parents=True, exist_ok=True)
    assert lab.ow("config", "set", "--global", "local.enabled", "false").exit_code == 0

    result = lab.ow("init", "community@18.0", cwd=ws)

    assert result.exit_code == 0, result.stderr + result.stdout
    assert not (ws / ".local" / "first").exists()


def _odoorc(ws):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string((ws / "odoorc").read_text())
    return parser
