"""The `local` plugin's seed copy: on `ow init`'s create path, before the first refresh (§17.3)."""

from __future__ import annotations

import configparser

def test_seed_copies_before_the_first_refresh(lab):
    # `.local/` before refreshing, so the Odoo plugin already sees the addon.
    seed = lab.home / ".config" / "ow" / "local"
    (seed / "l10n_seed").mkdir(parents=True)
    (seed / "l10n_seed" / "__manifest__.py").write_text("{'name': 'Seed'}")

    community = lab.remote("community", {"18.0": lab.odoo_files()}, head="18.0")
    assert lab.ow("repo", "add", "community", f"file://{community}").exit_code == 0
    ws = lab.ws("voip")

    result = lab.ow("init", str(ws), "community", "--base", "18.0")

    assert result.exit_code == 0
    manifest = ws / ".local" / "l10n_seed" / "__manifest__.py"
    assert manifest.is_file(), "the seed did not land in .local/"
    parser = _odoorc(lab, ws)
    assert parser["options"]["addons_path"].startswith(str(ws / ".local"))


def test_a_later_init_does_not_seed_again(lab):
    # The event fires on the create path only: a repair re-run copies nothing new.
    seed = lab.home / ".config" / "ow" / "local"
    (seed / "first").mkdir(parents=True)
    (seed / "first" / "__manifest__.py").write_text("{}")
    community = lab.remote("community", {"18.0": lab.odoo_files()}, head="18.0")
    assert lab.ow("repo", "add", "community", f"file://{community}").exit_code == 0
    ws = lab.ws("voip")
    assert lab.ow("init", str(ws), "community", "--base", "18.0").exit_code == 0

    (seed / "second").mkdir()
    (seed / "second" / "__manifest__.py").write_text("{}")
    assert lab.ow("init", str(ws), "community").exit_code == 0

    assert (ws / ".local" / "first" / "__manifest__.py").is_file()
    assert not (ws / ".local" / "second").exists()


def _odoorc(lab, ws):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string((ws / "odoorc").read_text())
    return parser
