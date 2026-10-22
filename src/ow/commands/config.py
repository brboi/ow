"""`ow config` — read and write settings in the global and workspace layers; a write
rewrites the whole file, prints what it dropped, and refreshes the resolved workspace.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import tomli_w

from ow import term
from ow.commands._shared import refresh_workspace, remotes_of
from ow.core.config import (
    Layer,
    closest_key,
    parse_cli_value,
    resolve_settings,
    update_global,
    update_workspace,
)
from ow.core.paths import global_config
from ow.core.session import Session
from ow.errors import UsageError

_NO_WORKSPACE = "not inside an ow workspace; pass --global or use -w"
_REPOS_KEY = "`[repos]` is not a setting; remove a repo with `ow repo rm <name>`"


def show_all(session: Session, *, global_: bool) -> int:
    """Print every declared key: `<key> = <value> (<origin>) # <help>`.

    With `global_`, the workspace layer is ignored and built-in defaults fill the rest.
    """
    values, origins = _values_and_origins(session, global_=global_)
    for key, setting in session.declared.items():
        term.out(f"{key} = {_render(values.get(key))} ({origins[key]}) # {setting.help}")
    return 0


def get(session: Session, key: str, *, global_: bool = False) -> int:
    """Print one key's value, or nothing when unset; an unknown key is a usage error.

    `global_` reads the global layer alone, otherwise the effective value.
    """
    setting = session.declared.get(key)
    if setting is None:
        raise _unknown_key(key, session.declared)
    if global_:
        value = session.global_layer.values.get(key)
    else:
        value = _values_and_origins(session, global_=False)[0].get(key)
    if value is None:
        return 0
    # A string is printed raw, so `$(ow config get key)` has no TOML quotes.
    term.out(value if isinstance(value, str) else _render(value))
    return 0


def set_(session: Session, key: str, values: Sequence[str], *, global_: bool) -> int:
    """Write one key in the global or the workspace layer, then refresh."""
    setting = session.declared.get(key)
    if setting is None:
        raise _unknown_key(key, session.declared)
    value = parse_cli_value(setting, list(values))
    return _update(session, lambda current: current | {key: value}, global_=global_)


def unset(session: Session, key: str, *, global_: bool) -> int:
    """Remove one key from the global or the workspace file, then refresh.

    An unknown key is allowed (that is how its warning gets fixed); an unset key is a no-op.
    """
    if key == "repos" or key.startswith("repos."):
        raise UsageError(_REPOS_KEY)
    path = global_config() if global_ else _workspace_config(session)
    if not _sets_key(path, key):
        return 0
    return _update(
        session, lambda current: {k: v for k, v in current.items() if k != key}, global_=global_
    )


def _values_and_origins(
    session: Session, *, global_: bool
) -> tuple[Mapping[str, object], Mapping[str, str]]:
    """The values and origins a listing shows."""
    if global_:
        return resolve_settings(
            session.declared,
            session.global_layer,
            Layer(values={}, unknown={}, wrong_type=()),
            ws_for_defaults=None,
        )
    if session.workspace is not None:
        return dict(session.workspace.settings), dict(session.origins)
    return resolve_settings(
        session.declared, session.global_layer, session.workspace_layer, ws_for_defaults=None
    )


def _workspace_config(session: Session) -> Path:
    """The workspace config path, or the usage error a `--global`-less write gets."""
    if session.workspace is None or session.config_path is None:
        raise UsageError(_NO_WORKSPACE)
    return session.config_path


def _update(
    session: Session,
    mutate: Callable[[dict[str, object]], Mapping[str, object]],
    *,
    global_: bool,
) -> int:
    """Apply `mutate` to the target file, print what the rewrite dropped, then refresh."""
    if global_:
        dropped = update_global(global_config(), mutate, declared=session.declared)
    else:
        # `[repos]` is not a setting: the rewrite keeps the workspace's repos,
        # taken from the session that already read them.
        dropped = update_workspace(
            _workspace_config(session),
            session.accepted_bases,
            mutate,
            declared=session.declared,
            remotes_of=remotes_of,
        )

    for dropped_key, raw in dropped.items():
        term.warn(f"dropped {dropped_key} = {_render(raw)}")

    if session.workspace is None:
        return 0
    return refresh_workspace(session.reload())


def _sets_key(path: Path, key: str) -> bool:
    """Whether the raw file sets `key` at all — known or unknown to ow."""
    try:
        with open(path, "rb") as f:
            data: object = tomllib.load(f)
    except FileNotFoundError:
        return False
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"Invalid TOML in {path}: {exc}") from exc
    except (UnicodeDecodeError, OSError) as exc:
        raise UsageError(f"cannot read {path}: {exc}") from exc
    current = data
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def _unknown_key(key: str, declared: Mapping[str, object]) -> UsageError:
    """The usage error an unknown key gets, suggesting the closest declared one."""
    suggestion = closest_key(key, declared)
    hint = f"; did you mean {suggestion!r}?" if suggestion else ""
    return UsageError(f"unknown setting {key!r}{hint}")


def _render(value: object) -> str:
    """A value in TOML spelling, so a listing reads like the config file would."""
    if value is None:
        return "(unset)"
    if isinstance(value, list):
        return "[" + ", ".join(_render(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_render(v)}" for k, v in value.items()) + "}"
    # tomli_w spells every scalar — bool, int, float, string, date — as the file would.
    return tomli_w.dumps({"v": value}).split("=", 1)[1].strip()
