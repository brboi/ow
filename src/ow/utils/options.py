"""Typed sparse Odoo/mise options and their deterministic resolution.

`OdooOverrides`/`MiseOverrides` are sparse: every field is `None` when unset,
never a secretly-meaningful empty value. `resolve_options` merges the global
and local overrides over the built-in defaults (built-in < global < local)
and produces the fully-populated `EffectiveOptions` a generator consumes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ow.utils.odoo import OdooInfo


@dataclass(frozen=True)
class OdooOverrides:
    http_port: int | None = None
    db_host: str | None = None
    db_port: int | None = None
    db_user: str | None = None
    db_password: str | None = None
    admin_passwd: str | None = None
    smtp_server: str | None = None
    smtp_port: int | None = None
    debug_args: tuple[str, ...] | None = None
    debug_test_args: tuple[str, ...] | None = None


@dataclass(frozen=True)
class MiseOverrides:
    python: str | None = None


@dataclass(frozen=True)
class OdooSettings:
    http_port: int
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    admin_passwd: str
    smtp_server: str
    smtp_port: int
    debug_args: tuple[str, ...]
    debug_test_args: tuple[str, ...]


@dataclass(frozen=True)
class EffectiveOptions:
    python: str | None
    odoo: OdooSettings | None
    warnings: tuple[str, ...] = ()


_PORT_FIELDS = ("http_port", "db_port", "smtp_port")
_NONEMPTY_STR_FIELDS = ("db_host", "db_user", "smtp_server")
_PASSWORD_FIELDS = ("db_password", "admin_passwd")
_ARG_LIST_FIELDS = ("debug_args", "debug_test_args")
_ODOO_KNOWN_KEYS = frozenset(
    _PORT_FIELDS + _NONEMPTY_STR_FIELDS + _PASSWORD_FIELDS + _ARG_LIST_FIELDS
)
_MISE_KNOWN_KEYS = frozenset({"python"})

_ODOO_DEFAULTS: dict[str, int | str] = {
    "http_port": 8069,
    "db_host": "localhost",
    "db_port": 5432,
    "db_user": "odoo",
    "db_password": "odoo",
    "admin_passwd": "Password",
    "smtp_server": "localhost",
    "smtp_port": 25,
}


def _reject_control_chars(value: str, key: str) -> None:
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{key} must not contain NUL, CR, or LF characters")


def _parse_port(value: object, key: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{key} must be an integer, not a boolean or other type")
    if not (1 <= value <= 65535):
        raise ValueError(f"{key} must be between 1 and 65535")
    return value


def _parse_nonempty_str(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    _reject_control_chars(value, key)
    if value == "":
        raise ValueError(f"{key} must not be empty")
    return value


def _parse_password_str(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    _reject_control_chars(value, key)
    return value


def _parse_arg_list(value: object, key: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{key} must be a list of strings")
    items: list[str] = []
    for element in value:
        if not isinstance(element, str):
            raise ValueError(f"{key} must be a list of strings")
        _reject_control_chars(element, key)
        items.append(element)
    return tuple(items)


def parse_odoo_options(data: Mapping[str, object]) -> OdooOverrides:
    """Validate a raw `[odoo]` table into a sparse `OdooOverrides`.

    Unknown keys, bool ports, out-of-range ports, wrong array element types
    and control characters (NUL/CR/LF) in scalars or argument elements are
    all rejected by key name, never by the value they carried.
    """
    unknown = set(data) - _ODOO_KNOWN_KEYS
    if unknown:
        raise ValueError(f"unknown odoo option(s): {', '.join(sorted(unknown))}")

    kwargs: dict[str, object] = {}
    for key in _PORT_FIELDS:
        if key in data:
            kwargs[key] = _parse_port(data[key], key)
    for key in _NONEMPTY_STR_FIELDS:
        if key in data:
            kwargs[key] = _parse_nonempty_str(data[key], key)
    for key in _PASSWORD_FIELDS:
        if key in data:
            kwargs[key] = _parse_password_str(data[key], key)
    for key in _ARG_LIST_FIELDS:
        if key in data:
            kwargs[key] = _parse_arg_list(data[key], key)
    return OdooOverrides(**kwargs)


def parse_python_minor(value: object) -> tuple[int, int]:
    if not isinstance(value, str):
        raise ValueError("mise.python must be a quoted 3.MINOR string")
    pieces = value.split(".")
    if (
        len(pieces) != 2
        or pieces[0] != "3"
        or not pieces[1].isascii()
        or not pieces[1].isdigit()
    ):
        raise ValueError("mise.python must be a quoted 3.MINOR string")
    return 3, int(pieces[1])


def parse_mise_options(data: Mapping[str, object]) -> MiseOverrides:
    """Validate a raw `[mise]` table into a sparse `MiseOverrides`.

    `python` must be a quoted `3.MINOR` string with an integer minor: floats,
    patch pins (`3.12.1`) and aliases (`latest`) are all rejected.
    """
    unknown = set(data) - _MISE_KNOWN_KEYS
    if unknown:
        raise ValueError(f"unknown mise option(s): {', '.join(sorted(unknown))}")
    if "python" not in data:
        return MiseOverrides()
    major, minor = parse_python_minor(data["python"])
    return MiseOverrides(python=f"{major}.{minor}")


def overlay(base: OdooOverrides, override: OdooOverrides) -> OdooOverrides:
    """Merge `override` over `base`, field by field, without truthiness.

    A field is inherited from `base` only when `override` left it `None`.
    An explicit empty string or empty tuple in `override` always wins.
    """
    return OdooOverrides(
        **{
            item.name: (
                getattr(override, item.name)
                if getattr(override, item.name) is not None
                else getattr(base, item.name)
            )
            for item in fields(OdooOverrides)
        }
    )


def resolve_options(
    global_odoo: OdooOverrides,
    global_mise: MiseOverrides,
    local_odoo: OdooOverrides,
    local_mise: MiseOverrides,
    *,
    core: "OdooInfo | None",
    workspace_name: str,
) -> EffectiveOptions:
    """Resolve sparse global/local overrides into `EffectiveOptions`.

    Precedence is built-in default < explicit global key < explicit local
    key. `debug_args`/`debug_test_args` defaults depend on `core` and
    `workspace_name` rather than being static. Without a core there is no
    Odoo checkout to configure, so `odoo` is always `None`; Python selection
    is still honored for a generic workspace that explicitly opts in.
    """
    merged_odoo = overlay(global_odoo, local_odoo)
    merged_python = local_mise.python if local_mise.python is not None else global_mise.python

    if core is None:
        return EffectiveOptions(python=merged_python, odoo=None)

    preference = parse_python_minor(merged_python) if merged_python is not None else (3, 12)
    clamped = min(max(preference, core.python_min), core.python_max)
    python = f"{clamped[0]}.{clamped[1]}"
    warnings: tuple[str, ...] = ()
    if clamped != preference:
        warnings = (
            f"requested python {preference[0]}.{preference[1]} is outside "
            f"{core.series} bounds {core.python_min[0]}.{core.python_min[1]}-"
            f"{core.python_max[0]}.{core.python_max[1]}; clamped to {python}",
        )

    default_debug_args = (
        ("--dev=all",) if core.with_demo else ("--dev=all", "--without-demo=all")
    )
    default_debug_test_args = (f"--test-tags={workspace_name}",)

    def pick(name: str, default: object) -> object:
        value = getattr(merged_odoo, name)
        return value if value is not None else default

    odoo_settings = OdooSettings(
        **{name: pick(name, default) for name, default in _ODOO_DEFAULTS.items()},
        debug_args=pick("debug_args", default_debug_args),
        debug_test_args=pick("debug_test_args", default_debug_test_args),
    )

    return EffectiveOptions(python=python, odoo=odoo_settings, warnings=warnings)
