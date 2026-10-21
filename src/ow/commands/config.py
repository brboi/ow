"""`ow config` — read and write settings in the global and workspace layers (§9).

The functions are plain: `cli.py` owns the Typer signatures and the session.
A write rewrites the whole file through `ow.core.config`, prints the keys the
rewrite dropped, and refreshes the resolved workspace (§17.5) — including for
`--global`, because the workspace's resolved settings just changed.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

import tomli_w

from ow import term
from ow.commands._shared import refresh_workspace
from ow.core import git
from ow.core.config import (
    Layer,
    closest_key,
    parse_cli_value,
    read_workspace,
    resolve_settings,
    write_global,
    write_workspace,
)
from ow.core.paths import global_config, repos_dir
from ow.core.session import Session
from ow.errors import UsageError

_NO_WORKSPACE = "not inside an ow workspace; pass --global or use -w"


def show_all(session: Session, *, global_: bool) -> int:
    """Print every declared key: `<key> = <value> (<origin>) # <help>`.

    With `global_`, the global layer is the only source: a key the global file
    does not set prints `(unset)` with origin `global`.
    """
    values, origins = _values_and_origins(session, global_=global_)
    for key, setting in session.declared.items():
        term.out(f"{key} = {_render(values.get(key))} ({origins[key]}) # {setting.help}")
    return 0


def get(session: Session, key: str) -> int:
    """Print one key's effective value; an unknown key is a usage error.

    An unset value prints nothing, which is not an error.
    """
    setting = session.declared.get(key)
    if setting is None:
        raise _unknown_key(key, session.declared)
    values, _ = _values_and_origins(session, global_=False)
    value = values.get(key)
    if value is None:
        return 0
    term.out(_render(value))
    return 0


def set_(session: Session, key: str, values: Sequence[str], *, global_: bool) -> int:
    """Write one key in the global or the workspace layer, then refresh."""
    setting = session.declared.get(key)
    if setting is None:
        raise _unknown_key(key, session.declared)
    value = parse_cli_value(setting, list(values))
    updated = dict(_layer(session, global_=global_).values) | {key: value}
    return _write(session, updated, global_=global_)


def unset(session: Session, key: str, *, global_: bool) -> int:
    """Remove one key from the global or the workspace file, then refresh.

    An unknown key is allowed: that is how the warnings about unknown keys get
    fixed. A key the file does not set is a no-op.
    """
    layer = _layer(session, global_=global_)
    path = global_config() if global_ else _workspace_config(session)
    if not _sets_key(path, key):
        return 0
    values = {k: v for k, v in layer.values.items() if k != key}
    return _write(session, values, global_=global_)


def _values_and_origins(
    session: Session, *, global_: bool
) -> tuple[Mapping[str, object], Mapping[str, str]]:
    """The effective values and origins a listing shows."""
    if global_:
        return dict(session.global_layer.values), {key: "global" for key in session.declared}
    if session.workspace is not None:
        return dict(session.workspace.settings), dict(session.origins)
    return resolve_settings(
        session.declared, session.global_layer, session.workspace_layer, ws_for_defaults=None
    )


def _layer(session: Session, *, global_: bool) -> Layer:
    """The layer a write targets; a workspace write needs a workspace."""
    if global_:
        return session.global_layer
    _workspace_config(session)
    return session.workspace_layer


def _workspace_config(session: Session) -> Path:
    """The workspace config path, or the usage error a `--global`-less write gets."""
    if session.workspace is None or session.config_path is None:
        raise UsageError(_NO_WORKSPACE)
    return session.config_path


def _write(session: Session, values: Mapping[str, object], *, global_: bool) -> int:
    """Rewrite the target file, print what the rewrite dropped, then refresh."""
    if global_:
        dropped = write_global(global_config(), values, declared=session.declared)
    else:
        config_path = _workspace_config(session)
        # `[repos]` is not a setting: a rewrite keeps the workspace's repos, so
        # they are re-read (and re-qualified) rather than taken from the layer.
        _, repos, _ = read_workspace(config_path, session.declared, remotes_of=_remotes_of)
        dropped = write_workspace(
            config_path,
            repos,
            values,
            declared=session.declared,
            remotes_of=_remotes_of,
        )

    for dropped_key, raw in dropped.items():
        term.warn(f"dropped {dropped_key} = {_render(raw)}")

    if session.workspace is None:
        return 0
    return refresh_workspace(session.reload())


def _remotes_of(repo_name: str) -> tuple[str, ...]:
    """A repo's remote names, for validating `[repos]` values (§5).

    Reads the bare repo, the same way `open_session` does; a missing or
    unreadable bare falls back to `origin` alone.
    """
    bare = (repos_dir() / f"{repo_name}.git").resolve()
    if not bare.is_dir():
        return ("origin",)
    try:
        output = git.read(["git", "-C", str(bare), "remote"])
    except git.GitFailed:
        return ("origin",)
    return tuple(output.split("\n")) if output else ("origin",)


def _sets_key(path: Path, key: str) -> bool:
    """Whether the raw file sets `key` at all — known or unknown to ow."""
    try:
        with open(path, "rb") as f:
            data: object = tomllib.load(f)
    except FileNotFoundError:
        return False
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"Invalid TOML in {path}: {exc}") from exc
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
