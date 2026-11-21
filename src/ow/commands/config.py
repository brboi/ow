"""`ow config` — read and write settings in the global and workspace layers; a write
rewrites the whole file, prints what it dropped, and refreshes the resolved workspace.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path

from ow import term
from ow.commands._shared import print_dropped, refresh_workspace, remotes_of, render_value
from ow.core import stacks
from ow.core.config import (
    Layer,
    closest_key,
    parse_cli_value,
    read_multi_config,
    resolve_settings,
    update_global,
    update_multi_workspace,
    update_stack,
    update_workspace,
)
from ow.core.json_schema import envelope
from ow.core.paths import global_config
from ow.core.session import Session
from ow.errors import UsageError

_NO_WORKSPACE = "not inside an ow workspace; pass --global or use -w"
_REPOS_KEY = "`[repos]` is not a setting; remove a repo with `ow repo rm <name>`"
_STACKS_KEY = "`[stacks]` is workspace layout metadata, not a plugin setting"


def show_all(
    session: Session,
    *,
    global_: bool,
    multi_root: Path | None = None,
    json_output: bool = False,
) -> int:
    """Print every declared key: `<key> = <value> (<origin>) # <help>`, or `--json`.

    With `global_`, the workspace layer is ignored and built-in defaults fill the rest.
    """
    with term.json_mode(json_output):
        values, origins = _values_and_origins(session, global_=global_, multi_root=multi_root)
        if json_output:
            term.emit_json(
                envelope(
                    {
                        "scope": "global" if global_ else "effective",
                        "settings": {
                            key: {
                                "value": values.get(key),
                                "origin": origins[key],
                                "help": setting.help,
                            }
                            for key, setting in session.declared.items()
                        },
                    }
                )
            )
            return 0
        for key, setting in session.declared.items():
            term.out(f"{key} = {render_value(values.get(key))} ({origins[key]}) # {setting.help}")
        return 0


def get(
    session: Session,
    key: str,
    *,
    global_: bool = False,
    multi_root: Path | None = None,
    json_output: bool = False,
) -> int:
    """Print one key's value, or nothing when unset; an unknown key is a usage error.

    `global_` reads the global layer alone, otherwise the effective value.
    """
    with term.json_mode(json_output):
        setting = session.declared.get(key)
        if setting is None:
            raise _unknown_key(key, session.declared)
        if global_:
            value = session.global_layer.values.get(key)
        else:
            value = _values_and_origins(session, global_=False, multi_root=multi_root)[0].get(key)
        if json_output:
            term.emit_json(envelope({"key": key, "value": value}))
            return 0
        if value is None:
            return 0
        # A string is printed raw, so `$(ow config get key)` has no TOML quotes.
        term.out(value if isinstance(value, str) else render_value(value))
        return 0


def set_(
    session: Session,
    key: str,
    values: Sequence[str],
    *,
    global_: bool,
    multi_root: Path | None = None,
) -> int:
    """Write one key in the global, workspace, or stack layer, then refresh."""
    setting = session.declared.get(key)
    if setting is None:
        raise _unknown_key(key, session.declared)
    value = parse_cli_value(setting, list(values))
    return _update(
        session,
        lambda current: current | {key: value},
        global_=global_,
        multi_root=multi_root,
    )


def unset(
    session: Session,
    key: str,
    *,
    global_: bool,
    multi_root: Path | None = None,
) -> int:
    """Remove one key from the global, workspace, or stack layer, then refresh.

    An unknown key is allowed (that is how its warning gets fixed); an unset key is a no-op.
    """
    if key == "repos" or key.startswith("repos."):
        raise UsageError(_REPOS_KEY)
    if key == "stacks" or key.startswith("stacks."):
        raise UsageError(_STACKS_KEY)
    if global_:
        path = global_config()
        stack_name = None
    elif multi_root is not None:
        path = multi_root / ".ow" / "config.toml"
        stack_name = None
    else:
        path = _workspace_config(session)
        stack_name = session.stack_name
    if not _sets_key(path, key, stack_name=stack_name):
        return 0
    return _update(
        session,
        lambda current: {k: v for k, v in current.items() if k != key},
        global_=global_,
        multi_root=multi_root,
        ignore_dropped=(key,),
    )


def _values_and_origins(
    session: Session, *, global_: bool, multi_root: Path | None = None
) -> tuple[Mapping[str, object], Mapping[str, str]]:
    """The values and origins a listing shows."""
    if global_:
        return resolve_settings(
            session.declared,
            session.global_layer,
            Layer(values={}, unknown={}, wrong_type=()),
            ws_for_defaults=None,
        )
    if multi_root is not None:
        ws_layer, _stacks = read_multi_config(
            multi_root / ".ow" / "config.toml", session.declared, remotes_of=remotes_of
        )
        return resolve_settings(
            session.declared, session.global_layer, ws_layer, ws_for_defaults=None
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
    multi_root: Path | None = None,
    ignore_dropped: Collection[str] = (),
) -> int:
    """Apply `mutate` to the target file, print what the rewrite dropped, then refresh."""
    if global_:
        dropped = update_global(global_config(), mutate, declared=session.declared)
    elif multi_root is not None:
        dropped = update_multi_workspace(
            multi_root / ".ow" / "config.toml", mutate, declared=session.declared
        )
    elif session.stack_name is not None:
        dropped = update_stack(
            _workspace_config(session),
            session.stack_name,
            session.accepted_bases,
            mutate,
            declared=session.declared,
            remotes_of=remotes_of,
        )
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

    print_dropped({k: v for k, v in dropped.items() if k not in ignore_dropped})

    if multi_root is not None:
        from ow.commands import refresh as refresh_cmd

        return refresh_cmd.run_multi(multi_root, session.plugins)
    if session.workspace is None:
        return 0
    reloaded = session.reload()
    code = refresh_workspace(reloaded)
    if code == 0 and reloaded.workspace is not None and reloaded.workspace.is_stack:
        parent = stacks.parent_multi_workspace(reloaded.workspace.root)
        if parent is not None:
            loaded = stacks.load_multi_workspace(parent, reloaded.plugins)
            if loaded is not None:
                stacks.sync_multi_workspace_files(loaded[0], reloaded.plugins)
    return code


def _sets_key(path: Path, key: str, *, stack_name: str | None = None) -> bool:
    """Whether the raw file (or `[stacks.<stack_name>]`) sets `key` at all — known or unknown to ow."""
    try:
        with open(path, "rb") as f:
            data: object = tomllib.load(f)
    except FileNotFoundError:
        return False
    except tomllib.TOMLDecodeError as exc:
        raise UsageError(f"Invalid TOML in {path}: {exc}") from exc
    except (UnicodeDecodeError, OSError) as exc:
        raise UsageError(f"cannot read {path}: {exc}") from exc
    current: object = data
    if stack_name is not None:
        if not isinstance(current, dict):
            return False
        stacks_tbl = current.get("stacks")
        if not isinstance(stacks_tbl, dict) or stack_name not in stacks_tbl:
            return False
        current = stacks_tbl[stack_name]
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
