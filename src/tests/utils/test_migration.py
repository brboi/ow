"""Explicit migration planning: frozen inventory, retirement, backups, commit.

The legacy checkout is never executed and no Jinja is imported: the frozen
digest asset is the only evidence of stock content, so most tests build a
small synthetic inventory from literal bytes and drive the real planning
functions with it. Backup and commit tests redirect XDG into `tmp_path`
through the shared `xdg` fixture, which is also what keeps
`paths.backups_dir()` out of the developer's real home.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from ow.utils import generate, migration, paths, render
from ow.utils.migration import (
    CUSTOM,
    CUSTOM_MISSING,
    GENERATED_OUTPUT_PATHS,
    OVERRIDDEN,
    STOCK,
    MigrationPlan,
    MigrationWrite,
    RetirementDecision,
    backup_path,
    commit_migration,
    inventory_bundles,
    legacy_source_output,
    load_template_digests,
    plan_lock_retirement,
    plan_retirement,
    write_backup,
)

V1 = b'version = 1\n[repos]\ncommunity = "origin/master"\n'
V2 = b'version = 2\n[repos]\ncommunity = "origin/master"\n'
STOCK_TEMPLATE = b'{\n  "stock": true\n}\n'
EDITED_TEMPLATE = b'{\n  "mine": true\n}\n'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def frozen_inventory(
    bundles: dict[str, dict[str, bytes]],
) -> dict[str, dict[str, tuple[str, ...]]]:
    """A frozen inventory built from literal bytes: one digest per source."""
    return {
        bundle: {relative: (sha(data),) for relative, data in sources.items()}
        for bundle, sources in bundles.items()
    }


def place(root: Path, relative: str, data: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def write_lock(ws: Path, entries: dict[str, str]) -> Path:
    path = ws / render.RENDERED_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render.dumps_lock(entries))
    return path


def only(bundles) -> dict[str, object]:
    return {bundle.name: bundle for bundle in bundles}


# ---------------------------------------------------------------------------
# The frozen digest asset.
# ---------------------------------------------------------------------------


def test_frozen_asset_covers_the_shipped_bundles():
    digests = load_template_digests()

    assert set(digests) == {"bwrap", "common", "odoo", "vscode", "zed"}
    assert set(digests["bwrap"]) == {"bwrap-claude.j2", "bwrap-opencode.j2"}
    assert set(digests["vscode"]) == {".vscode/launch.json.j2", ".vscode/settings.json.j2"}
    assert set(digests["zed"]) == {".zed/debug.json.j2", ".zed/settings.json.j2"}
    # Both pinned trees: the reviewed main shipped these under `common/`, the
    # separation branch relocated them to `odoo/` without touching bytes.
    assert digests["common"]["mise.toml.j2"]
    assert digests["common"]["mise/conf.d/00-ow.toml.j2"]
    assert digests["common"]["odoorc.j2"] == digests["odoo"]["odoorc.j2"]
    for sources in digests.values():
        for relative, hashes in sources.items():
            assert hashes, relative
            assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hashes)


def test_frozen_asset_ships_paths_and_digests_only():
    raw = (
        Path(migration.__file__).resolve().parent.parent
        / "_static"
        / "legacy-template-hashes.json"
    ).read_text(encoding="utf-8")

    assert "{{" not in raw
    assert "{%" not in raw


def test_malformed_frozen_asset_is_an_error_naming_the_asset(tmp_path):
    asset = tmp_path / "hashes.json"
    asset.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError, match=re.escape(str(asset))):
        load_template_digests(asset)

    asset.write_text(json.dumps({"common": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        load_template_digests(asset)

    asset.write_text(json.dumps({"common": {"odoorc.j2": ["not-hex"]}}), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid digest"):
        load_template_digests(asset)


def test_every_generator_path_has_a_frozen_producer():
    """Attribution must be possible for every path a 3.0 render can write."""
    frozen = {
        legacy_source_output(relative)
        for sources in load_template_digests().values()
        for relative in sources
    }
    assert set(GENERATED_OUTPUT_PATHS) <= frozen


# ---------------------------------------------------------------------------
# Inventory: names, bytes and derivable outputs, without rendering.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        (".vscode/settings.json.j2", ".vscode/settings.json"),
        ("requirements-dev.txt", "requirements-dev.txt"),
        ("weird.j2.j2", "weird.j2"),
        ("not-j2.txt", "not-j2.txt"),
    ],
)
def test_source_output_strips_j2_once(relative, expected):
    assert legacy_source_output(relative) == expected


def test_inventory_classifies_stock_overridden_custom_and_missing(tmp_path):
    digests = frozen_inventory(
        {
            "vscode": {".vscode/settings.json.j2": STOCK_TEMPLATE},
            "common": {"mise/conf.d/00-ow.toml.j2": STOCK_TEMPLATE},
        }
    )
    overrides = tmp_path / "config" / "ow" / "templates"
    workspace_templates = tmp_path / "ws" / ".ow" / "templates"
    place(overrides, "vscode/.vscode/settings.json.j2", STOCK_TEMPLATE)
    place(workspace_templates, "common/mise/conf.d/00-ow.toml.j2", EDITED_TEMPLATE)
    place(overrides, "mine/thing.j2", b"custom\n")

    bundles = only(
        inventory_bundles(
            ["vscode", "common", "mine", "gone"],
            digests,
            overrides_root=overrides,
            workspace_templates_root=workspace_templates,
        )
    )

    assert set(bundles) == {"common", "gone", "mine", "vscode"}
    assert bundles["vscode"].status == STOCK
    assert [(f.relative, f.output, f.stock) for f in bundles["vscode"].files] == [
        (".vscode/settings.json.j2", ".vscode/settings.json", True)
    ]
    assert bundles["vscode"].frozen_outputs == (".vscode/settings.json",)
    assert bundles["common"].status == OVERRIDDEN
    assert [(f.relative, f.output, f.stock) for f in bundles["common"].files] == [
        ("mise/conf.d/00-ow.toml.j2", "mise/conf.d/00-ow.toml", False)
    ]
    assert bundles["mine"].status == CUSTOM
    assert [(f.relative, f.output) for f in bundles["mine"].files] == [("thing.j2", "thing")]
    assert bundles["mine"].frozen_outputs == ()
    assert bundles["gone"].status == CUSTOM_MISSING
    assert bundles["gone"].files == ()


def test_inventory_treats_an_extra_source_file_as_overridden(tmp_path):
    digests = frozen_inventory({"vscode": {".vscode/settings.json.j2": STOCK_TEMPLATE}})
    overrides = tmp_path / "templates"
    place(overrides, "vscode/.vscode/settings.json.j2", STOCK_TEMPLATE)
    place(overrides, "vscode/.vscode/mine.json.j2", b"{}\n")

    [bundle] = inventory_bundles(
        ["vscode"],
        digests,
        overrides_root=overrides,
        workspace_templates_root=tmp_path / "ws",
    )

    assert bundle.status == OVERRIDDEN
    assert [(f.relative, f.output, f.stock) for f in bundle.files] == [
        (".vscode/mine.json.j2", ".vscode/mine.json", False),
        (".vscode/settings.json.j2", ".vscode/settings.json", True),
    ]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads past file modes")
def test_inventory_counts_an_unreadable_source_as_unprovable(tmp_path):
    digests = frozen_inventory({"vscode": {"settings.json.j2": STOCK_TEMPLATE}})
    overrides = tmp_path / "templates"
    source = place(overrides, "vscode/settings.json.j2", STOCK_TEMPLATE)
    source.chmod(0o000)

    [bundle] = inventory_bundles(
        ["vscode"],
        digests,
        overrides_root=overrides,
        workspace_templates_root=tmp_path / "ws",
    )

    assert bundle.status == OVERRIDDEN
    assert bundle.files[0].digest is None
    assert bundle.files[0].stock is False


@pytest.mark.parametrize("name", ["", "a/b", ".", ".."])
def test_inventory_rejects_unusable_bundle_names(tmp_path, name):
    with pytest.raises(ValueError, match="invalid legacy bundle name"):
        inventory_bundles(
            [name], {}, overrides_root=tmp_path, workspace_templates_root=tmp_path
        )


# ---------------------------------------------------------------------------
# Retirement: the pure decision, before any lock is touched.
# ---------------------------------------------------------------------------


def test_retirement_retires_an_overridden_generated_output(tmp_path):
    digests = frozen_inventory(
        {
            "vscode": {
                ".vscode/settings.json.j2": STOCK_TEMPLATE,
                ".vscode/launch.json.j2": STOCK_TEMPLATE,
            }
        }
    )
    overrides = tmp_path / "templates"
    edited = place(overrides, "vscode/.vscode/settings.json.j2", EDITED_TEMPLATE)
    locked = {
        ".vscode/settings.json": sha(b"ow rendered settings"),
        ".vscode/launch.json": sha(b"ow rendered launch"),
    }

    bundles = inventory_bundles(
        ["vscode"],
        digests,
        overrides_root=overrides,
        workspace_templates_root=tmp_path / "ws",
    )
    decision = plan_retirement(bundles, locked)

    assert decision.retired == (".vscode/settings.json",)
    assert decision.kept == (".vscode/launch.json",)
    assert f"legacy source {edited} -> .vscode/settings.json (not stock)" in decision.report
    assert any(
        line.startswith("retired lock entry .vscode/settings.json:") for line in decision.report
    )


def test_retirement_keeps_a_stock_bundle_with_no_override_intact(tmp_path):
    digests = frozen_inventory(
        {
            "vscode": {
                ".vscode/settings.json.j2": STOCK_TEMPLATE,
                ".vscode/launch.json.j2": STOCK_TEMPLATE,
            }
        }
    )
    locked = {
        ".vscode/settings.json": sha(b"ow rendered settings"),
        ".vscode/launch.json": sha(b"ow rendered launch"),
    }

    [bundle] = inventory_bundles(
        ["vscode"],
        digests,
        overrides_root=tmp_path / "absent",
        workspace_templates_root=tmp_path / "ws",
    )
    decision = plan_retirement([bundle], locked)

    assert bundle.status == STOCK
    assert decision.retired == ()
    assert decision.kept == tuple(sorted(locked))
    assert any("has no local copy; frozen outputs:" in line for line in decision.report)


def test_retirement_keeps_a_locked_output_no_generator_produces(tmp_path):
    digests = frozen_inventory({"bwrap": {"bwrap-claude.j2": STOCK_TEMPLATE}})
    overrides = tmp_path / "templates"
    edited = place(overrides, "bwrap/bwrap-claude.j2", EDITED_TEMPLATE)
    locked = {"bwrap-claude": sha(b"old wrapper")}

    [bundle] = inventory_bundles(
        ["bwrap"],
        digests,
        overrides_root=overrides,
        workspace_templates_root=tmp_path / "ws",
    )
    decision = plan_retirement([bundle], locked)

    assert decision.retired == ()
    assert decision.kept == ("bwrap-claude",)
    assert f"legacy source {edited} -> bwrap-claude (not stock)" in decision.report
    assert not any(line.startswith("retired lock entry") for line in decision.report)


def test_retirement_from_a_missing_custom_bundle_covers_generated_entries(tmp_path):
    locked = {
        ".vscode/settings.json": sha(b"a"),
        "odoorc": sha(b"b"),
        "bwrap-claude": sha(b"c"),
        "notes/plan.md": sha(b"d"),
    }

    [bundle] = inventory_bundles(
        ["mybundle"],
        {},
        overrides_root=tmp_path / "templates",
        workspace_templates_root=tmp_path / "ws" / ".ow" / "templates",
    )
    assert bundle.status == CUSTOM_MISSING

    decision = plan_retirement([bundle], locked)

    assert decision.retired == (".vscode/settings.json", "odoorc")
    assert decision.kept == ("bwrap-claude", "notes/plan.md")
    assert any("mybundle" in line and "no source under" in line for line in decision.report)


def test_retirement_from_a_present_custom_bundle_covers_only_its_outputs(tmp_path):
    overrides = tmp_path / "templates"
    place(overrides, "mybundle/odoorc.j2", b"my odoorc\n")
    place(overrides, "mybundle/wrapper.sh", b"#!/bin/sh\n")
    locked = {"odoorc": sha(b"a"), "mybundle/wrapper.sh": sha(b"b")}

    [bundle] = inventory_bundles(
        ["mybundle"],
        {},
        overrides_root=overrides,
        workspace_templates_root=tmp_path / "ws",
    )
    assert bundle.status == CUSTOM

    decision = plan_retirement([bundle], locked)

    assert decision.retired == ("odoorc",)
    assert decision.kept == ("mybundle/wrapper.sh",)


def test_one_overriding_contributor_retires_a_shared_output(tmp_path):
    digests = frozen_inventory({"vscode": {".vscode/settings.json.j2": STOCK_TEMPLATE}})
    overrides = tmp_path / "templates"
    place(overrides, "vscode/.vscode/settings.json.j2", STOCK_TEMPLATE)
    place(overrides, "mine/.vscode/settings.json.j2", b"mine\n")

    bundles = inventory_bundles(
        ["vscode", "mine"],
        digests,
        overrides_root=overrides,
        workspace_templates_root=tmp_path / "ws",
    )
    assert {bundle.status for bundle in bundles} == {STOCK, CUSTOM}

    decision = plan_retirement(bundles, {".vscode/settings.json": sha(b"ow")})

    assert decision.retired == (".vscode/settings.json",)


def test_lock_retirement_is_none_when_nothing_retires(tmp_path):
    decision = RetirementDecision(retired=(), kept=("a",), report=())
    assert plan_lock_retirement(tmp_path, decision) is None


def test_lock_retirement_writes_through_dumps_lock(tmp_path):
    ws = tmp_path / "ws"
    kept = sha(b"kept")
    lock_path = write_lock(ws, {"a/b": kept, "c": sha(b"c")})

    write = plan_lock_retirement(
        ws, RetirementDecision(retired=("c",), kept=("a/b",), report=())
    )

    assert write is not None
    assert write.path == lock_path
    assert write.original == lock_path.read_bytes()
    assert write.replacement == render.dumps_lock({"a/b": kept})


# ---------------------------------------------------------------------------
# Backups: exact bytes, private modes, hash-addressed retry.
# ---------------------------------------------------------------------------


def test_backup_is_byte_exact_and_private(xdg, tmp_path):
    source = tmp_path / "ws" / ".ow" / "config.toml"
    original = b'version = 1\n\x00\xffdb_password = "s3cret"\n'
    source.parent.mkdir(parents=True)
    source.write_bytes(original)

    dest = write_backup(source, original)

    assert dest == backup_path(source, original)
    assert dest.read_bytes() == original
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert stat.S_IMODE(dest.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(dest.parent.parent.stat().st_mode) == 0o700
    assert dest.parent.name == sha(str(source.absolute()).encode("utf-8"))
    assert dest.name == f"{sha(original)}.toml"


def test_backup_reuses_an_equal_destination(xdg, tmp_path):
    source = tmp_path / "config.toml"
    original = b"[repos]\n"
    dest = backup_path(source, original)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(original)

    assert write_backup(source, original) == dest
    assert dest.read_bytes() == original
    assert sorted(path.name for path in dest.parent.iterdir()) == [dest.name]


def test_backup_refuses_a_mismatching_destination(xdg, tmp_path):
    source = tmp_path / "config.toml"
    original = b'[repos]\ncommunity = "origin/master"\n'
    dest = backup_path(source, original)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(original[:4])

    with pytest.raises(ValueError, match=re.escape(str(dest))):
        write_backup(source, original)

    assert dest.read_bytes() == original[:4]


def test_backup_retries_after_an_interrupted_run(xdg, tmp_path):
    """A crash leaves a temp file, never a truncated destination, so a rerun works."""
    source = tmp_path / "config.toml"
    original = b"[repos]\n"
    dest = backup_path(source, original)
    dest.parent.mkdir(parents=True)
    (dest.parent / f".{dest.name}.leftover.tmp").write_bytes(b"half written")

    assert write_backup(source, original) == dest
    assert dest.read_bytes() == original


def test_backup_keeps_each_original_under_its_own_digest(xdg, tmp_path):
    source = tmp_path / "config.toml"
    first = write_backup(source, b"version = 1\n")
    second = write_backup(source, b"version = 2\n")

    assert first != second
    assert first.read_bytes() == b"version = 1\n"
    assert second.read_bytes() == b"version = 2\n"


# ---------------------------------------------------------------------------
# Commit: blockers first, every backup, then the atomic replacements.
# ---------------------------------------------------------------------------


def test_blocking_errors_stop_every_write(xdg, tmp_path):
    config = tmp_path / "config.toml"
    config.write_bytes(V1)
    plan = MigrationPlan(
        writes=(MigrationWrite(path=config, original=V1, replacement=V2),),
        errors=(f"unknown var 'nope' in {config}",),
    )

    with pytest.raises(ValueError, match="unknown var"):
        commit_migration(plan)

    assert config.read_bytes() == V1
    assert not (paths.backups_dir() / "migrations").exists()


def test_commit_backs_up_lock_and_both_configs_then_replaces_them(xdg, tmp_path):
    ws = tmp_path / "ws"
    user_settings = b'{"editor.formatOnSave": false}\n'
    settings = place(ws, ".vscode/settings.json", user_settings)
    launch = place(ws, ".vscode/launch.json", b'{"version": "0.2.0"}\n')
    lock_path = write_lock(
        ws,
        {
            ".vscode/settings.json": sha(user_settings),
            ".vscode/launch.json": sha(launch.read_bytes()),
        },
    )
    original_lock = lock_path.read_bytes()

    global_config = tmp_path / "ow" / "config.toml"
    workspace_config = ws / ".ow" / "config.toml"
    global_config.parent.mkdir(parents=True, exist_ok=True)
    global_config.write_bytes(V1)
    workspace_config.write_bytes(V1)

    decision = RetirementDecision(
        retired=(".vscode/settings.json",), kept=(".vscode/launch.json",), report=()
    )
    lock_write = plan_lock_retirement(ws, decision)
    assert lock_write is not None
    plan = MigrationPlan(
        writes=(
            MigrationWrite(path=global_config, original=V1, replacement=V2),
            lock_write,
            MigrationWrite(path=workspace_config, original=V1, replacement=V2),
        )
    )

    before = tree(ws)
    commit_migration(plan)

    assert render.read_lock(ws) == {".vscode/launch.json": sha(launch.read_bytes())}
    assert global_config.read_bytes() == V2
    assert workspace_config.read_bytes() == V2
    assert stat.S_IMODE(global_config.stat().st_mode) == 0o600
    assert stat.S_IMODE(workspace_config.stat().st_mode) == 0o600
    for source, original in (
        (global_config, V1),
        (workspace_config, V1),
        (lock_path, original_lock),
    ):
        backup = backup_path(source, original)
        assert backup.read_bytes() == original
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert settings.read_bytes() == user_settings
    assert set(before) <= set(tree(ws))


def test_backups_all_precede_replacements_and_the_lock_is_retired_first(
    xdg, tmp_path, monkeypatch
):
    ws = tmp_path / "ws"
    original_lock = write_lock(
        ws, {".vscode/settings.json": sha(b"ow"), "bwrap-claude": sha(b"wrap")}
    ).read_bytes()
    lock_path = ws / render.RENDERED_LOCK
    config = ws / ".ow" / "config.toml"
    config.write_bytes(V1)

    lock_write = plan_lock_retirement(
        ws,
        RetirementDecision(
            retired=(".vscode/settings.json",), kept=("bwrap-claude",), report=()
        ),
    )
    assert lock_write is not None
    config_write = MigrationWrite(path=config, original=V1, replacement=V2)

    replaced: list[Path] = []
    real_replace = os.replace

    def record(source, dest, *args, **kwargs):
        replaced.append(Path(dest))
        if Path(dest) == config:
            raise OSError("disk went away")
        return real_replace(source, dest, *args, **kwargs)

    monkeypatch.setattr(os, "replace", record)
    with pytest.raises(OSError, match="disk went away"):
        # The plan deliberately lists the config first: retirement must win.
        commit_migration(MigrationPlan(writes=(config_write, lock_write)))

    replacements = [path for path in replaced if not path.is_relative_to(paths.backups_dir())]
    assert replacements == [lock_path, config]
    assert replacements[0] == lock_path
    assert render.read_lock(ws) == {"bwrap-claude": sha(b"wrap")}
    assert config.read_bytes() == V1
    assert backup_path(config, V1).read_bytes() == V1
    assert backup_path(lock_path, original_lock).read_bytes() == original_lock


def test_commit_refuses_a_source_edited_since_planning(xdg, tmp_path):
    config = tmp_path / "config.toml"
    edited = b"version = 1 # edited\n"
    config.write_bytes(edited)

    with pytest.raises(ValueError, match=re.escape(str(config))):
        commit_migration(
            MigrationPlan(writes=(MigrationWrite(path=config, original=V1, replacement=V2),))
        )

    assert config.read_bytes() == edited
    assert not (paths.backups_dir() / "migrations").exists()


def test_commit_refuses_a_source_that_disappeared(xdg, tmp_path):
    missing = tmp_path / "config.toml"

    with pytest.raises(ValueError, match="disappeared"):
        commit_migration(
            MigrationPlan(writes=(MigrationWrite(path=missing, original=V1, replacement=V2),))
        )


def test_commit_refuses_a_source_that_appeared(xdg, tmp_path):
    config = tmp_path / "config.toml"
    config.write_bytes(V1)

    with pytest.raises(ValueError, match="appeared"):
        commit_migration(
            MigrationPlan(writes=(MigrationWrite(path=config, original=None, replacement=V2),))
        )

    assert config.read_bytes() == V1


# ---------------------------------------------------------------------------
# End to end: a workspace keeps every byte and loses exactly one lock entry.
# ---------------------------------------------------------------------------


def test_retirement_end_to_end_leaves_files_and_drops_only_the_retired_entry(xdg, tmp_path):
    ws = tmp_path / "ws"
    user_settings = b'{"editor.formatOnSave": false}\n'
    settings = place(ws, ".vscode/settings.json", user_settings)
    launch_bytes = b'{"version": "0.2.0"}\n'
    launch = place(ws, ".vscode/launch.json", launch_bytes)
    wrapper = place(ws, "bwrap-claude", b"#!/bin/sh\n")
    write_lock(
        ws,
        {
            ".vscode/settings.json": sha(user_settings),
            ".vscode/launch.json": sha(launch_bytes),
            "bwrap-claude": sha(b"#!/bin/sh\n"),
        },
    )

    digests = frozen_inventory(
        {
            "vscode": {
                ".vscode/settings.json.j2": STOCK_TEMPLATE,
                ".vscode/launch.json.j2": STOCK_TEMPLATE,
            }
        }
    )
    overrides = tmp_path / "config" / "templates"
    overridden = place(overrides, "vscode/.vscode/settings.json.j2", EDITED_TEMPLATE)

    bundles = inventory_bundles(
        ["vscode"],
        digests,
        overrides_root=overrides,
        workspace_templates_root=ws / ".ow" / "templates",
    )
    decision = plan_retirement(bundles, render.read_lock(ws))
    assert decision.retired == (".vscode/settings.json",)
    assert f"legacy source {overridden} -> .vscode/settings.json (not stock)" in decision.report

    write = plan_lock_retirement(ws, decision)
    assert write is not None
    before = tree(ws)
    commit_migration(MigrationPlan(writes=(write,), warnings=decision.report))

    assert set(before) <= set(tree(ws))
    assert settings.read_bytes() == user_settings
    assert launch.read_bytes() == launch_bytes
    assert wrapper.read_bytes() == b"#!/bin/sh\n"
    assert render.read_lock(ws) == {
        ".vscode/launch.json": sha(launch_bytes),
        "bwrap-claude": sha(b"#!/bin/sh\n"),
    }

    outputs = (
        generate.GeneratedFile(PurePosixPath(".vscode/settings.json"), b'{"ow": true}\n'),
    )
    states = {state.path: state.state for state in render.plan_files(ws, outputs, ()).states}
    assert states[".vscode/settings.json"] == render.YOURS
    assert states["bwrap-claude"] == render.NOT_RENDERED


def test_retirement_without_candidates_never_rewrites_the_lock(xdg, tmp_path):
    ws = tmp_path / "ws"
    entries = {".vscode/settings.json": sha(b"ow"), "bwrap-claude": sha(b"wrap")}
    lock_path = write_lock(ws, entries)
    before = lock_path.read_bytes()

    bundles = inventory_bundles(
        ["vscode"],
        frozen_inventory({"vscode": {".vscode/settings.json.j2": STOCK_TEMPLATE}}),
        overrides_root=tmp_path / "absent",
        workspace_templates_root=ws / ".ow" / "templates",
    )
    decision = plan_retirement(bundles, render.read_lock(ws))

    assert decision.retired == ()
    assert plan_lock_retirement(ws, decision) is None
    assert lock_path.read_bytes() == before


# ---------------------------------------------------------------------------
# No templating engine: migration must not be able to render anything.
# ---------------------------------------------------------------------------


def test_migration_imports_no_templating_engine():
    repo = Path(__file__).resolve().parents[3]
    code = (
        "import sys\n"
        "import ow.utils.migration\n"
        "assert 'jinja2' not in sys.modules, 'jinja2 was imported'\n"
        "assert 'ow.utils.templates' not in sys.modules, 'legacy templates was imported'\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=repo,
        env=dict(os.environ, PYTHONPATH=str(repo / "src")),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"