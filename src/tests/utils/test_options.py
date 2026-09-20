"""Tests for ow.utils.options: sparse typed options, validation, inheritance
and deterministic Odoo/Python resolution.

`ow.utils.odoo` is developed concurrently and must not be imported here: a
local stand-in with identical field names substitutes for `OdooInfo`.
"""

from dataclasses import dataclass, field

import pytest

from ow.utils.options import (
    EffectiveOptions,
    MiseOverrides,
    OdooOverrides,
    OdooSettings,
    overlay,
    parse_mise_options,
    parse_odoo_options,
    parse_python_minor,
    resolve_options,
)


@dataclass(frozen=True)
class FakeOdooInfo:
    alias: str
    series: str
    major: int
    minor: int
    python_min: tuple[int, int]
    python_max: tuple[int, int]
    with_demo: bool
    sandbox_paths: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# parse_odoo_options
# ---------------------------------------------------------------------------


def test_parse_odoo_options_accepts_every_field():
    overrides = parse_odoo_options(
        {
            "http_port": 8068,
            "db_host": "db.internal",
            "db_port": 5433,
            "db_user": "custom",
            "db_password": "secret",
            "admin_passwd": "adminsecret",
            "smtp_server": "smtp.internal",
            "smtp_port": 2525,
            "debug_args": ["--dev=all"],
            "debug_test_args": ["--test-tags=foo"],
        }
    )
    assert overrides == OdooOverrides(
        http_port=8068,
        db_host="db.internal",
        db_port=5433,
        db_user="custom",
        db_password="secret",
        admin_passwd="adminsecret",
        smtp_server="smtp.internal",
        smtp_port=2525,
        debug_args=("--dev=all",),
        debug_test_args=("--test-tags=foo",),
    )


def test_parse_odoo_options_empty_mapping_is_all_unset():
    assert parse_odoo_options({}) == OdooOverrides()


def test_parse_odoo_options_converts_argument_lists_to_tuples():
    overrides = parse_odoo_options({"debug_args": ["--dev=all", "--without-demo=all"]})
    assert overrides.debug_args == ("--dev=all", "--without-demo=all")
    assert isinstance(overrides.debug_args, tuple)


def test_parse_odoo_options_empty_argument_list_is_kept():
    assert parse_odoo_options({"debug_args": []}).debug_args == ()


@pytest.mark.parametrize("field_name", ["db_password", "admin_passwd"])
def test_parse_odoo_options_keeps_empty_string_for_password_fields(field_name):
    overrides = parse_odoo_options({field_name: ""})
    assert getattr(overrides, field_name) == ""


@pytest.mark.parametrize("field_name", ["db_host", "db_user", "smtp_server"])
def test_parse_odoo_options_rejects_empty_string_for_nonempty_fields(field_name):
    with pytest.raises(ValueError, match=field_name):
        parse_odoo_options({field_name: ""})


@pytest.mark.parametrize("field_name", ["http_port", "db_port", "smtp_port"])
def test_parse_odoo_options_rejects_bool_ports(field_name):
    with pytest.raises(ValueError, match=field_name):
        parse_odoo_options({field_name: True})


@pytest.mark.parametrize("field_name", ["http_port", "db_port", "smtp_port"])
@pytest.mark.parametrize("bad_value", [0, -1, 65536, 100000])
def test_parse_odoo_options_rejects_out_of_range_ports(field_name, bad_value):
    with pytest.raises(ValueError, match=field_name):
        parse_odoo_options({field_name: bad_value})


@pytest.mark.parametrize("field_name", ["http_port", "db_port", "smtp_port"])
@pytest.mark.parametrize("good_value", [1, 65535, 8069])
def test_parse_odoo_options_accepts_boundary_ports(field_name, good_value):
    overrides = parse_odoo_options({field_name: good_value})
    assert getattr(overrides, field_name) == good_value


def test_parse_odoo_options_rejects_unknown_keys():
    with pytest.raises(ValueError, match="bogus"):
        parse_odoo_options({"bogus": 1})


@pytest.mark.parametrize("field_name", ["debug_args", "debug_test_args"])
def test_parse_odoo_options_rejects_wrong_array_element_types(field_name):
    with pytest.raises(ValueError, match=field_name):
        parse_odoo_options({field_name: ["--dev=all", 42]})


@pytest.mark.parametrize("field_name", ["debug_args", "debug_test_args"])
def test_parse_odoo_options_rejects_a_bare_string_for_argument_fields(field_name):
    with pytest.raises(ValueError, match=field_name):
        parse_odoo_options({field_name: "--dev=all"})


@pytest.mark.parametrize(
    "field_name,value",
    [
        ("db_host", "bad\nhost"),
        ("db_password", "bad\rpassword"),
        ("smtp_server", "bad\x00server"),
    ],
)
def test_parse_odoo_options_rejects_control_characters_in_scalars(field_name, value):
    with pytest.raises(ValueError, match=field_name):
        parse_odoo_options({field_name: value})


@pytest.mark.parametrize("field_name", ["debug_args", "debug_test_args"])
def test_parse_odoo_options_rejects_control_characters_in_argument_elements(field_name):
    with pytest.raises(ValueError, match=field_name):
        parse_odoo_options({field_name: ["--dev=all\x00"]})


def test_parse_odoo_options_error_names_key_not_value():
    with pytest.raises(ValueError, match="db_password") as excinfo:
        parse_odoo_options({"db_password": "swordfish\n"})
    assert "swordfish" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# parse_mise_options / parse_python_minor
# ---------------------------------------------------------------------------


def test_parse_mise_options_accepts_quoted_minor_string():
    assert parse_mise_options({"python": "3.12"}).python == "3.12"


def test_parse_mise_options_empty_mapping_is_unset():
    assert parse_mise_options({}) == MiseOverrides()


def test_parse_mise_options_rejects_unknown_keys():
    with pytest.raises(ValueError, match="bogus"):
        parse_mise_options({"bogus": "3.12"})


def test_parse_mise_options_rejects_float():
    with pytest.raises(ValueError):
        parse_mise_options({"python": 3.12})


def test_parse_mise_options_rejects_patch_pin():
    with pytest.raises(ValueError):
        parse_mise_options({"python": "3.12.1"})


def test_parse_mise_options_rejects_alias():
    with pytest.raises(ValueError):
        parse_mise_options({"python": "latest"})


def test_parse_mise_options_rejects_wrong_major():
    with pytest.raises(ValueError):
        parse_mise_options({"python": "2.7"})


def test_parse_python_minor_returns_integer_tuple():
    assert parse_python_minor("3.9") == (3, 9)
    assert isinstance(parse_python_minor("3.9")[1], int)


# ---------------------------------------------------------------------------
# overlay
# ---------------------------------------------------------------------------


def test_empty_password_and_argument_list_are_explicit_overrides():
    merged = overlay(
        OdooOverrides(db_password="global", debug_args=("--dev=all",)),
        OdooOverrides(db_password="", debug_args=()),
    )
    assert merged.db_password == ""
    assert merged.debug_args == ()


def test_overlay_inherits_unset_fields_from_base():
    merged = overlay(
        OdooOverrides(db_host="global-host", db_port=6000),
        OdooOverrides(http_port=8070),
    )
    assert merged.db_host == "global-host"
    assert merged.db_port == 6000
    assert merged.http_port == 8070


def test_overlay_independent_local_http_ports_inherit_shared_global_db_settings():
    global_odoo = OdooOverrides(db_host="db.internal", db_port=6000)
    community = overlay(global_odoo, OdooOverrides(http_port=8070))
    enterprise = overlay(global_odoo, OdooOverrides(http_port=8071))

    assert community.http_port == 8070
    assert enterprise.http_port == 8071
    assert community.db_host == enterprise.db_host == "db.internal"
    assert community.db_port == enterprise.db_port == 6000


# ---------------------------------------------------------------------------
# resolve_options
# ---------------------------------------------------------------------------


def test_non_odoo_workspace_does_not_choose_a_python():
    effective = resolve_options(
        OdooOverrides(),
        MiseOverrides(),
        OdooOverrides(),
        MiseOverrides(),
        core=None,
        workspace_name="docs",
    )
    assert effective.python is None
    assert effective.odoo is None


def test_generic_workspace_honors_explicit_python_opt_in_without_a_core():
    effective = resolve_options(
        OdooOverrides(),
        MiseOverrides(python="3.13"),
        OdooOverrides(),
        MiseOverrides(),
        core=None,
        workspace_name="tools",
    )
    assert effective.python == "3.13"
    assert effective.odoo is None


def test_clearing_a_local_override_follows_a_changed_global_value():
    core = FakeOdooInfo(
        alias="community",
        series="19.0",
        major=19,
        minor=0,
        python_min=(3, 10),
        python_max=(3, 13),
        with_demo=True,
    )
    before = resolve_options(
        OdooOverrides(http_port=9000),
        MiseOverrides(),
        OdooOverrides(http_port=9090),
        MiseOverrides(),
        core=core,
        workspace_name="ws",
    )
    assert before.odoo.http_port == 9090

    after = resolve_options(
        OdooOverrides(http_port=9500),
        MiseOverrides(),
        OdooOverrides(),
        MiseOverrides(),
        core=core,
        workspace_name="ws",
    )
    assert after.odoo.http_port == 9500


def test_resolve_options_fills_built_in_defaults_when_nothing_is_set():
    core = FakeOdooInfo(
        alias="community",
        series="19.0",
        major=19,
        minor=0,
        python_min=(3, 10),
        python_max=(3, 13),
        with_demo=False,
    )
    effective = resolve_options(
        OdooOverrides(), MiseOverrides(), OdooOverrides(), MiseOverrides(),
        core=core, workspace_name="ws",
    )
    assert isinstance(effective, EffectiveOptions)
    assert effective.odoo == OdooSettings(
        http_port=8069,
        db_host="localhost",
        db_port=5432,
        db_user="odoo",
        db_password="odoo",
        admin_passwd="Password",
        smtp_server="localhost",
        smtp_port=25,
        debug_args=("--dev=all", "--without-demo=all"),
        debug_test_args=("--test-tags=ws",),
    )
    assert effective.python == "3.12"
    assert effective.warnings == ()


def test_resolve_options_debug_args_default_enables_demo_when_declared():
    core = FakeOdooInfo(
        alias="community", series="19.0", major=19, minor=0,
        python_min=(3, 10), python_max=(3, 13), with_demo=True,
    )
    effective = resolve_options(
        OdooOverrides(), MiseOverrides(), OdooOverrides(), MiseOverrides(),
        core=core, workspace_name="ws",
    )
    assert effective.odoo.debug_args == ("--dev=all",)


def test_resolve_options_explicit_empty_test_args_are_not_replaced_by_defaults():
    core = FakeOdooInfo(
        alias="community", series="19.0", major=19, minor=0,
        python_min=(3, 10), python_max=(3, 13), with_demo=True,
    )
    local_odoo = parse_odoo_options({"debug_test_args": []})
    effective = resolve_options(
        OdooOverrides(), MiseOverrides(), local_odoo, MiseOverrides(),
        core=core, workspace_name="ws",
    )
    assert effective.odoo.debug_test_args == ()


def test_resolve_options_python_preference_defaults_to_3_12():
    core = FakeOdooInfo(
        alias="community", series="19.0", major=19, minor=0,
        python_min=(3, 10), python_max=(3, 14), with_demo=True,
    )
    effective = resolve_options(
        OdooOverrides(), MiseOverrides(), OdooOverrides(), MiseOverrides(),
        core=core, workspace_name="ws",
    )
    assert effective.python == "3.12"
    assert effective.warnings == ()


def test_resolve_options_explicit_python_within_bounds_is_used_unchanged():
    core = FakeOdooInfo(
        alias="community", series="19.0", major=19, minor=0,
        python_min=(3, 10), python_max=(3, 13), with_demo=True,
    )
    effective = resolve_options(
        OdooOverrides(), MiseOverrides(python="3.11"), OdooOverrides(), MiseOverrides(),
        core=core, workspace_name="ws",
    )
    assert effective.python == "3.11"
    assert effective.warnings == ()


def test_resolve_options_clamps_upward_at_inclusive_lower_bound():
    core = FakeOdooInfo(
        alias="community", series="19.0", major=19, minor=0,
        python_min=(3, 10), python_max=(3, 13), with_demo=True,
    )
    effective = resolve_options(
        OdooOverrides(), MiseOverrides(python="3.9"), OdooOverrides(), MiseOverrides(),
        core=core, workspace_name="ws",
    )
    assert effective.python == "3.10"
    assert len(effective.warnings) == 1
    assert "3.9" in effective.warnings[0]
    assert "3.10" in effective.warnings[0]


def test_resolve_options_clamps_downward_at_inclusive_upper_bound():
    core = FakeOdooInfo(
        alias="community", series="19.0", major=19, minor=0,
        python_min=(3, 9), python_max=(3, 13), with_demo=True,
    )
    effective = resolve_options(
        OdooOverrides(), MiseOverrides(python="3.15"), OdooOverrides(), MiseOverrides(),
        core=core, workspace_name="ws",
    )
    assert effective.python == "3.13"
    assert len(effective.warnings) == 1
    assert "3.15" in effective.warnings[0]
    assert "3.13" in effective.warnings[0]


def test_resolve_options_accepts_bounds_at_the_inclusive_edges_without_warning():
    core = FakeOdooInfo(
        alias="community", series="19.0", major=19, minor=0,
        python_min=(3, 10), python_max=(3, 13), with_demo=True,
    )
    lower = resolve_options(
        OdooOverrides(), MiseOverrides(python="3.10"), OdooOverrides(), MiseOverrides(),
        core=core, workspace_name="ws",
    )
    upper = resolve_options(
        OdooOverrides(), MiseOverrides(python="3.13"), OdooOverrides(), MiseOverrides(),
        core=core, workspace_name="ws",
    )
    assert lower.python == "3.10" and lower.warnings == ()
    assert upper.python == "3.13" and upper.warnings == ()


def test_resolve_options_local_python_overrides_global_python():
    core = FakeOdooInfo(
        alias="community", series="19.0", major=19, minor=0,
        python_min=(3, 9), python_max=(3, 14), with_demo=True,
    )
    effective = resolve_options(
        OdooOverrides(), MiseOverrides(python="3.11"),
        OdooOverrides(), MiseOverrides(python="3.13"),
        core=core, workspace_name="ws",
    )
    assert effective.python == "3.13"
