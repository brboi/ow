"""The runtime configuration model, and its version-aware I/O.

Two schemas live here. Schema 2 is the typed model: `Config` and
`WorkspaceConfig` carry sparse `OdooOverrides`/`MiseOverrides` and nothing
else, and a schema-2 file is serialized whole from those fields. Schema 1 is
the legacy template manifest: it is *read* through a private translation
(spec §6's key map) into the same typed fields, and written back
byte-conservatively — only the fields the model owns are updated, in the
document that is on disk at save time, so comments and data ow has no model
for survive a `ow switch` or a TUI edit. Converting a file is the job of
`ow init`/`ow render` alone; nothing here does it as a side effect.

`load_global_config()` reads, and creates nothing: the documented bootstrap
(`_DEFAULT_CONFIG`) is written only by an explicit persist, so a status
probe, a completion callback or a dashboard launch leaves no file behind.
"""

import contextlib
import os
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import tomli_w
import tomlkit
import typer

from ow.utils import paths
from ow.utils.display import err_console
from ow.utils.options import (
    MiseOverrides,
    OdooOverrides,
    overlay,
    parse_mise_options,
    parse_odoo_options,
)

SCHEMA_VERSION = 2


@dataclass
class BranchSpec:
    base_ref: str  # e.g. "origin/master", "dev/master-phoenix"
    local_branch: str | None = None  # None = detached

    @property
    def is_detached(self) -> bool:
        return self.local_branch is None

    @property
    def remote(self) -> str:
        return self.base_ref.split("/")[0]

    @property
    def branch(self) -> str:
        return "/".join(self.base_ref.split("/")[1:])

    def to_spec_str(self) -> str:
        # `origin/` is implicit in a spec — but only for a branch whose own
        # name carries no slash. Dropping it from `origin/dev/18.0-fix`
        # writes `dev/18.0-fix`, which parse_branch_spec reads back as the
        # `dev` remote: a different ref, on any machine that has one. A spec
        # that does not re-read as itself is worse than a verbose one.
        implicit = self.remote == "origin" and "/" not in self.branch
        base = self.branch if implicit else self.base_ref
        if self.local_branch is None:
            return base
        return f"{base}..{self.local_branch}"


def parse_branch_spec(spec: str) -> BranchSpec:
    """
    "master"                  → BranchSpec("origin/master")
    "master..master-feature"  → BranchSpec("origin/master", "master-feature")
    "dev/master-phoenix..fix" → BranchSpec("dev/master-phoenix", "fix")
    "origin/master"           → BranchSpec("origin/master")
    """
    spec = spec.strip()
    if not spec:
        raise ValueError("invalid branch spec: empty string")
    if ".." not in spec:
        if "/" in spec:
            return BranchSpec(spec)
        return BranchSpec(f"origin/{spec}")
    base, local = spec.split("..", 1)
    if not base or not local or ".." in local or any(c.isspace() for c in base) or any(c.isspace() for c in local):
        raise ValueError(f"invalid branch spec: {spec!r}")
    if "/" not in base:
        base = f"origin/{base}"
    return BranchSpec(base, local)


@dataclass
class RemoteConfig:
    url: str
    pushurl: str | None = None
    fetch: str | None = None


@dataclass(frozen=True)
class LegacyConfig:
    """The schema-1 file a record was loaded from, and what it cannot say.

    `original` is the whole file, byte for byte: it is what a version-1 save
    updates in place and what a migration backs up. `issues` names data the
    typed model cannot represent — an unknown var, a value the option
    parsers reject, an unknown owned key. They never stop a read or a
    version-1 save, and they block every migration write until the user
    resolves them: guessing a default would silently discard the data.
    """

    source: Path
    original: bytes
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class WorkspaceConfig:
    """One workspace's intent: repo specs, and sparse typed overrides.

    `kw_only` is deliberate: every field is named at every construction site,
    so a caller written for an older field order cannot silently bind its
    second positional argument to `version` or `legacy`.
    """

    repos: dict[str, BranchSpec]
    odoo: OdooOverrides = field(default_factory=OdooOverrides)
    mise: MiseOverrides = field(default_factory=MiseOverrides)
    version: int = SCHEMA_VERSION
    legacy: LegacyConfig | None = None


@dataclass(frozen=True, kw_only=True)
class Config:
    """The global model: remotes, typed defaults, and the output ignore list."""

    remotes: dict[str, dict[str, RemoteConfig]]  # alias -> remote_name -> cfg
    odoo: OdooOverrides = field(default_factory=OdooOverrides)
    mise: MiseOverrides = field(default_factory=MiseOverrides)
    owignore: tuple[str, ...] = ()
    version: int = SCHEMA_VERSION
    editor: str = "code"
    theme: str = "textual-dark"
    legacy: LegacyConfig | None = None


_WS_HEADER = "# Managed by ow. Do not edit `version` — ow reads it to migrate this file.\n"

# The bootstrap text. Written only by an explicit persist (`ow init`/`ow
# render`, and a TUI save of an absent config); a read returns the same
# document in memory and creates nothing. Typed keys are shown commented
# out: an uncommented `[mise] python` would hand Python tooling to every
# generic workspace that never asked for it.
_DEFAULT_CONFIG = '''\
# ow configuration. Everything here is optional.
# `version` is ow's schema marker — do not edit it.
version = 2

# editor = "code"   # the command `ow open` runs; may include flags, e.g. "code -n"
# theme = "textual-dark"   # dashboard theme: textual-dark, textual-light, monokai, dracula
# owignore = [".zed/**"]   # gitignore-style patterns ow never generates

[remotes.community]
origin.url = "git@github.com:odoo/odoo.git"
# dev.url = "git@github.com:odoo-dev/odoo.git"
# dev.pushurl = "git@github.com:odoo-dev/odoo.git"
# dev.fetch = "+refs/heads/*:refs/remotes/dev/*"

# [remotes.enterprise]
# origin.url = "git@github.com:odoo/enterprise.git"

# Typed defaults. Every key is optional, and a workspace may override any of
# them; the built-in values apply when neither level sets a key.
# [odoo]
# http_port = 8069
# db_host = "localhost"
# db_port = 5432
# db_user = "odoo"
# db_password = "odoo"
# admin_passwd = "Password"
# smtp_server = "localhost"
# smtp_port = 25
# debug_args = ["--dev=all"]
# debug_test_args = ["--test-tags=my-workspace"]

# Only for a workspace that opts into Python tooling.
# [mise]
# python = "3.12"
'''

# Schema-1 keys ow translates (spec §6). `python` is the mise override; the
# rest are the odoo option keys, both debug argument arrays included.
_LEGACY_MISE_VAR = "python"
_LEGACY_VAR_KEYS = frozenset(
    {
        "http_port",
        "db_host",
        "db_port",
        "db_user",
        "db_password",
        "admin_passwd",
        "smtp_server",
        "smtp_port",
        "debug_args",
        "debug_test_args",
    }
)

_V1_WORKSPACE_KEYS = frozenset({"version", "repos", "templates", "vars", "odoo", "mise"})
_V1_GLOBAL_KEYS = frozenset(
    {"version", "remotes", "editor", "theme", "owignore", "vars", "odoo", "mise"}
)
_V2_WORKSPACE_KEYS = frozenset({"version", "repos", "odoo", "mise"})
_V2_GLOBAL_KEYS = frozenset(
    {"version", "editor", "theme", "owignore", "remotes", "odoo", "mise"}
)


@dataclass(frozen=True)
class _LegacyTranslation:
    """What a schema-1 document's options translate to, and what they cannot."""

    odoo: OdooOverrides
    mise: MiseOverrides
    issues: tuple[str, ...] = ()


def _legacy_options(document: Mapping[str, object]) -> _LegacyTranslation:
    """Translate a schema-1 document's `vars` and transitional typed tables.

    Exactly spec §6's key map: `python` → `mise.python`, the ten odoo keys →
    their `odoo.*` counterparts, and a typed `[odoo]`/`[mise]` key wins over
    a `vars` key for its own field only. Anything else — an unknown var, a
    wrong type, a port out of range — becomes an issue naming the key, not a
    load failure: the file stays readable and rewritable, and migration
    refuses it instead of discarding what it says.
    """
    issues: list[str] = []
    odoo = OdooOverrides()
    mise = MiseOverrides()

    def apply_odoo(key: str, value: object, label: str) -> None:
        nonlocal odoo
        try:
            odoo = overlay(odoo, parse_odoo_options({key: value}))
        except ValueError as exc:
            issues.append(f"{label}: {exc}")

    def apply_mise(key: str, value: object, label: str) -> None:
        nonlocal mise
        try:
            parsed = parse_mise_options({key: value})
        except ValueError as exc:
            issues.append(f"{label}: {exc}")
            return
        if parsed.python is not None:
            mise = MiseOverrides(python=parsed.python)

    for key, value in _table(document, "vars", issues).items():
        if key == _LEGACY_MISE_VAR:
            apply_mise(key, value, f"vars.{key}")
        elif key in _LEGACY_VAR_KEYS:
            apply_odoo(key, value, f"vars.{key}")
        else:
            issues.append(f"unknown legacy var {key!r}")
    for key, value in _table(document, "odoo", issues).items():
        apply_odoo(key, value, f"odoo.{key}")
    for key, value in _table(document, "mise", issues).items():
        apply_mise(key, value, f"mise.{key}")

    return _LegacyTranslation(odoo=odoo, mise=mise, issues=tuple(issues))


def _legacy_templates(document: Mapping[str, object]) -> tuple[str, ...]:
    """The bundle names a schema-1 workspace declared, or () when it declared none.

    Migration inventories them; the loader records a malformed value as an
    issue rather than failing, because `ow render` is what has to refuse it.
    """
    declared = document.get("templates")
    if declared is None:
        return ()
    if not isinstance(declared, list) or not all(isinstance(name, str) for name in declared):
        raise ValueError("'templates' must be a list of bundle names")
    return tuple(declared)


def _legacy_document(legacy: LegacyConfig) -> Mapping[str, object]:
    """The parsed document behind a legacy record, as plain Python values.

    Parsed on demand from the retained bytes: keeping a parsed document on
    the record would make it mutable state two saves could disagree about,
    and every legacy write has to be a function of the bytes that were
    actually read. Plain values matter too — the option parsers distinguish
    a bool from an int, and tomlkit's wrappers would have to be unwrapped
    before that distinction survived.
    """
    return tomllib.loads(legacy.original.decode("utf-8"))


def _legacy_tree(legacy: LegacyConfig) -> Any:
    """The same document as a tomlkit tree, for updating it in place.

    Comments, key order and the data ow has no model for live in the tomlkit
    tree and nowhere else, so this is the one a *write* works from — which
    is what makes a legacy save preserve all three.
    """
    return tomlkit.parse(legacy.original.decode("utf-8"))


def _table(document: Mapping[str, object], key: str, issues: list[str]) -> Mapping[str, object]:
    """One optional sub-table, or an empty mapping plus an issue."""
    table = document.get(key, {})
    if not isinstance(table, Mapping):
        issues.append(f"'{key}' must be a table")
        return {}
    return table


def _unknown_key_problem(
    document: Mapping[str, object], allowed: frozenset[str], *, scope: str
) -> str | None:
    unknown = sorted(set(document) - allowed)
    if not unknown:
        return None
    if "owignore" in unknown and scope == "workspace":
        return "'owignore' is global only; a workspace has no ignore list"
    listed = ", ".join(repr(key) for key in unknown)
    noun = "key" if len(unknown) == 1 else "keys"
    return f"unknown {noun} {listed}; allowed: {', '.join(sorted(allowed))}"


def _document(path: Path) -> tuple[bytes, dict[str, Any]]:
    """A config file's exact bytes and its parsed TOML.

    Invalid TOML and unreadable files stay ordinary load errors: they are
    not data ow can preserve or translate, they are files it cannot read.
    """
    original = path.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: not valid UTF-8: {exc}") from exc
    return original, tomllib.loads(text)


def _schema_version(data: Mapping[str, Any], path: Path) -> int:
    version = data.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"{path}: 'version' must be an integer")
    if version < 1:
        raise ValueError(f"{path}: unknown config schema version {version}")
    if version > SCHEMA_VERSION:
        raise ValueError(
            f"{path}: config schema version {version} is newer than ow supports "
            f"({SCHEMA_VERSION}); upgrade ow to read this file"
        )
    return version


def _repos(document: Mapping[str, object], issues: list[str]) -> dict[str, BranchSpec]:
    table = document.get("repos", {})
    if not isinstance(table, Mapping):
        issues.append("'repos' must be a table of alias = branch spec")
        return {}
    repos: dict[str, BranchSpec] = {}
    for alias, spec in table.items():
        if not isinstance(spec, str):
            issues.append(f"repos.{alias} must be a branch spec string")
            continue
        try:
            repos[alias] = parse_branch_spec(spec)
        except ValueError as exc:
            issues.append(f"repos.{alias}: {exc}")
    return repos


_REMOTE_KEYS = frozenset({"url", "pushurl", "fetch"})


def _remotes(
    document: Mapping[str, object], issues: list[str], *, strict: bool
) -> dict[str, dict[str, RemoteConfig]]:
    """The `[remotes]` table, entry by entry.

    Strict mode (schema 2) refuses a subkey ow does not own, because the
    writer would drop it on the next save: better a named error than a key
    that quietly disappears. A schema-1 entry is not rebuilt but updated in
    place, so a key ow never heard of there is evidence to keep, not a
    reason to refuse the file.
    """
    table = document.get("remotes", {})
    if not isinstance(table, Mapping):
        issues.append("'remotes' must be a table of remote entries")
        return {}
    remotes: dict[str, dict[str, RemoteConfig]] = {}
    for alias, remote_map in table.items():
        if not isinstance(remote_map, Mapping):
            issues.append(f"[remotes.{alias}] must be a table of remote entries")
            continue
        remotes[alias] = {}
        for remote_name, remote_cfg in remote_map.items():
            if not isinstance(remote_cfg, Mapping) or "url" not in remote_cfg:
                issues.append(f"[remotes.{alias}.{remote_name}] must have a 'url' key")
                continue
            if strict:
                for key in sorted(set(remote_cfg) - _REMOTE_KEYS):
                    issues.append(f"[remotes.{alias}.{remote_name}] unknown key {key!r}")
            remotes[alias][remote_name] = RemoteConfig(
                url=remote_cfg["url"],
                pushurl=remote_cfg.get("pushurl"),
                fetch=remote_cfg.get("fetch"),
            )
    return remotes


def _odoo_options(document: Mapping[str, object], issues: list[str]) -> OdooOverrides:
    table = document.get("odoo", {})
    if not isinstance(table, Mapping):
        issues.append("'odoo' must be a table")
        return OdooOverrides()
    try:
        return parse_odoo_options(table)
    except ValueError as exc:
        issues.append(str(exc))
        return OdooOverrides()


def _mise_options(document: Mapping[str, object], issues: list[str]) -> MiseOverrides:
    table = document.get("mise", {})
    if not isinstance(table, Mapping):
        issues.append("'mise' must be a table")
        return MiseOverrides()
    try:
        return parse_mise_options(table)
    except ValueError as exc:
        issues.append(str(exc))
        return MiseOverrides()


def _owignore(document: Mapping[str, object], issues: list[str]) -> tuple[str, ...]:
    value = document.get("owignore")
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, list) or not all(
        isinstance(pattern, str) for pattern in value
    ):
        issues.append("'owignore' must be a list of patterns")
        return ()
    return tuple(value)


def _editor(document: Mapping[str, object], issues: list[str]) -> str:
    value = document.get("editor", "code")
    if not isinstance(value, str):
        issues.append("'editor' must be a string")
        return "code"
    return value


def _theme(document: Mapping[str, object], issues: list[str]) -> str:
    value = document.get("theme", "textual-dark")
    if not isinstance(value, str):
        issues.append("'theme' must be a string")
        return "textual-dark"
    return value


def _load_workspace_v2(path: Path, document: Mapping[str, object]) -> WorkspaceConfig:
    issues: list[str] = []
    problem = _unknown_key_problem(document, _V2_WORKSPACE_KEYS, scope="workspace")
    if problem is not None:
        issues.append(problem)
    repos = _repos(document, issues)
    odoo = _odoo_options(document, issues)
    mise = _mise_options(document, issues)
    if issues:
        raise ValueError(f"{path}: " + "; ".join(issues))
    return WorkspaceConfig(repos=repos, odoo=odoo, mise=mise)


def _load_workspace_v1(
    path: Path, original: bytes, document: Mapping[str, object]
) -> WorkspaceConfig:
    # A malformed repo is not data ow can preserve: it is the manifest's own
    # intent, unreadable. Everything else in a schema-1 file is evidence to
    # keep and report, never a reason to refuse to read the workspace.
    repo_issues: list[str] = []
    repos = _repos(document, repo_issues)
    if repo_issues:
        raise ValueError(f"{path}: " + "; ".join(repo_issues))

    translation = _legacy_options(document)
    issues = list(translation.issues)
    problem = _unknown_key_problem(document, _V1_WORKSPACE_KEYS, scope="workspace")
    if problem is not None:
        issues.append(problem)
    try:
        _legacy_templates(document)
    except ValueError as exc:
        issues.append(str(exc))

    return WorkspaceConfig(
        repos=repos,
        odoo=translation.odoo,
        mise=translation.mise,
        version=1,
        legacy=LegacyConfig(source=path, original=original, issues=tuple(issues)),
    )


def load_workspace_config(path: Path) -> WorkspaceConfig:
    """Read the .ow/config.toml file from an individual workspace.

    Schema 2 is validated strictly: every top-level key is one ow owns, and
    every option goes through the same parser a render uses. Schema 1 (or a
    file with no `version` at all) loads through the legacy translation and
    keeps its exact bytes, so a save or a migration works from the file
    rather than from a memory of it.
    """
    original, document = _document(path)
    version = _schema_version(document, path)
    if version == 1:
        return _load_workspace_v1(path, original, document)
    return _load_workspace_v2(path, document)


def _load_global_v2(path: Path, document: Mapping[str, object]) -> Config:
    issues: list[str] = []
    problem = _unknown_key_problem(document, _V2_GLOBAL_KEYS, scope="global")
    if problem is not None:
        issues.append(problem)
    remotes = _remotes(document, issues, strict=True)
    odoo = _odoo_options(document, issues)
    mise = _mise_options(document, issues)
    owignore = _owignore(document, issues)
    editor = _editor(document, issues)
    theme = _theme(document, issues)
    if issues:
        raise ValueError(f"{path}: " + "; ".join(issues))
    return Config(
        remotes=remotes,
        odoo=odoo,
        mise=mise,
        owignore=owignore,
        editor=editor,
        theme=theme,
    )


def _load_global_v1(path: Path, original: bytes, document: Mapping[str, object]) -> Config:
    repo_issues: list[str] = []
    remotes = _remotes(document, repo_issues, strict=False)
    if repo_issues:
        raise ValueError(f"{path}: " + "; ".join(repo_issues))

    translation = _legacy_options(document)
    issues = list(translation.issues)
    problem = _unknown_key_problem(document, _V1_GLOBAL_KEYS, scope="global")
    if problem is not None:
        issues.append(problem)

    return Config(
        remotes=remotes,
        odoo=translation.odoo,
        mise=translation.mise,
        owignore=_owignore(document, issues),
        editor=_editor(document, issues),
        theme=_theme(document, issues),
        version=1,
        legacy=LegacyConfig(source=path, original=original, issues=tuple(issues)),
    )


def load_config(path: Path) -> Config:
    """Read a global config file: schema 2 strictly, schema 1 by translation."""
    original, document = _document(path)
    version = _schema_version(document, path)
    if version == 1:
        return _load_global_v1(path, original, document)
    return _load_global_v2(path, document)


def _default_config() -> Config:
    """The in-memory schema-2 default, parsed from the bootstrap text itself.

    Parsing our own bootstrap is what keeps the two from drifting: the file
    an explicit persist writes and the model a read-only command uses are
    the same document.
    """
    return _load_global_v2(Path("<ow default>"), tomlkit.parse(_DEFAULT_CONFIG))


def load_global_config() -> Config:
    """The user's configuration, or the in-memory default when the file is absent.

    Creating the file is not a read's business: a `ow status`, a completion
    callback or a dashboard launch must leave no config behind, and
    `check_legacy_layout()` depends on "no global config yet" meaning what
    it says. `ow init`/`ow render` persist explicitly.
    """
    path = paths.config_file()
    if not path.exists():
        return _default_config()
    return load_config(path)


def _sparse_odoo(odoo: OdooOverrides) -> dict[str, object]:
    return {name: value for name, value in asdict(odoo).items() if value is not None}


def _sparse_mise(mise: MiseOverrides) -> dict[str, object]:
    return {name: value for name, value in asdict(mise).items() if value is not None}


def dumps_workspace_config(ws: WorkspaceConfig) -> bytes:
    """The bytes this record's own schema calls for. Pure: reads no file.

    A schema-1 record keeps its file: only `[repos]` is updated, in the
    retained document, so comments and unknown data survive. A schema-2
    record is serialized whole and sparse — an unset option is absent, never
    written as a copy of a default it does not own.
    """
    if ws.version == 1 and ws.legacy is not None:
        document = _legacy_tree(ws.legacy)
        _set_repos(document, ws.repos)
        return tomlkit.dumps(document).encode("utf-8")

    data: dict[str, Any] = {
        "repos": {alias: spec.to_spec_str() for alias, spec in ws.repos.items()},
    }
    odoo = _sparse_odoo(ws.odoo)
    if odoo:
        data["odoo"] = odoo
    mise = _sparse_mise(ws.mise)
    if mise:
        data["mise"] = mise

    # `version` is emitted by hand, ahead of the serialised body: tomli_w
    # cannot write comments, and this one has to sit next to the key it
    # warns about. Keeping it out of `data` also means a bare top-level key
    # can never land after the [repos] table, which would not be valid TOML.
    return f"{_WS_HEADER}version = {ws.version}\n\n{tomli_w.dumps(data)}".encode("utf-8")


def dumps_global_config(config: Config) -> bytes:
    """The bytes this record's own schema calls for. Pure: reads no file.

    A schema-2 record is written as the documented bootstrap with the
    record's values applied, so the comments that explain the keys survive
    every save. A schema-1 record keeps its own document and only its
    remotes, editor and theme are updated.
    """
    if config.version == 1 and config.legacy is not None:
        document = _legacy_tree(config.legacy)
        _set_global_legacy_fields(document, config)
        return tomlkit.dumps(document).encode("utf-8")

    document = tomlkit.parse(_DEFAULT_CONFIG)
    _set_global_v2_fields(document, config)
    return tomlkit.dumps(document).encode("utf-8")


def _set_repos(document: Mapping[str, Any], repos: Mapping[str, BranchSpec]) -> None:
    if "repos" not in document:
        document["repos"] = tomlkit.table()
    table = document["repos"]
    for alias in list(table.keys()):
        if alias not in repos:
            del table[alias]
    for alias, spec in repos.items():
        table[alias] = spec.to_spec_str()


def _set_remotes(
    document: Mapping[str, Any], remotes: Mapping[str, Mapping[str, RemoteConfig]]
) -> None:
    """Update `[remotes]` in place, entry by entry.

    Entry tables are updated rather than replaced so an unrelated subkey or
    comment inside one survives — the same reason the whole document is
    updated in place instead of being rebuilt.
    """
    if "remotes" not in document:
        if not remotes:
            return
        document["remotes"] = tomlkit.table(is_super_table=True)
    table = document["remotes"]
    for alias in list(table.keys()):
        if alias not in remotes:
            del table[alias]
    for alias, remote_map in remotes.items():
        if alias not in table:
            table[alias] = tomlkit.table()
        alias_table = table[alias]
        for remote_name in list(alias_table.keys()):
            if remote_name not in remote_map:
                del alias_table[remote_name]
        for remote_name, remote in remote_map.items():
            if remote_name not in alias_table:
                alias_table[remote_name] = tomlkit.table()
            entry = alias_table[remote_name]
            entry["url"] = remote.url
            if remote.pushurl is not None:
                entry["pushurl"] = remote.pushurl
            elif "pushurl" in entry:
                del entry["pushurl"]
            if remote.fetch is not None:
                entry["fetch"] = remote.fetch
            elif "fetch" in entry:
                del entry["fetch"]


def _set_optional_scalar(document: Mapping[str, Any], key: str, value: str, default: str) -> None:
    """Set a scalar when it is explicit or already present; never add a default.

    Writing `editor = "code"` into every config that merely inherited the
    default would turn a live default into a pinned value: a later change of
    the built-in default could never reach those files again.
    """
    if key in document or value != default:
        document[key] = value


def _set_optional_list(document: Mapping[str, Any], key: str, values: tuple[str, ...]) -> None:
    if not values:
        if key in document:
            del document[key]
        return
    document[key] = list(values)


def _set_sparse_table(
    document: Mapping[str, Any], key: str, values: Mapping[str, object]
) -> None:
    """Update one option table to exactly `values`, or leave it absent."""
    if key not in document and not values:
        return
    if key not in document:
        document[key] = tomlkit.table()
    table = document[key]
    for name in list(table.keys()):
        if name not in values:
            del table[name]
    for name, value in values.items():
        table[name] = list(value) if isinstance(value, tuple) else value


def _set_global_legacy_fields(document: Mapping[str, Any], config: Config) -> None:
    """The only global fields a schema-1 save may touch: remotes, editor, theme."""
    _set_remotes(document, config.remotes)
    _set_optional_scalar(document, "editor", config.editor, "code")
    _set_optional_scalar(document, "theme", config.theme, "textual-dark")


def _set_global_v2_fields(document: Mapping[str, Any], config: Config) -> None:
    _set_global_legacy_fields(document, config)
    _set_optional_list(document, "owignore", config.owignore)
    _set_sparse_table(document, "odoo", _sparse_odoo(config.odoo))
    _set_sparse_table(document, "mise", _sparse_mise(config.mise))


def _reject_typed_change(
    legacy: LegacyConfig,
    *,
    odoo: OdooOverrides,
    mise: MiseOverrides,
    owignore: tuple[str, ...] | None = None,
) -> None:
    """Refuse typed data a schema-1 save has nowhere to put.

    Loading translates `vars` into the typed model, so a schema-1 record's
    options normally *are* what its file says. A caller that changed them is
    asking for a conversion — and converting implicitly, or dropping the
    change, are both worse than naming the one command that converts.
    """
    document = _legacy_document(legacy)
    translated = _legacy_options(document)
    representable = odoo == translated.odoo and mise == translated.mise
    if owignore is not None:
        representable = representable and owignore == _owignore(document, [])
    if not representable:
        raise ValueError(
            f"{legacy.source} is schema 1: typed options cannot be saved to it; "
            "run `ow render` to migrate it to schema 2"
        )


def _reload_legacy_document(path: Path) -> Mapping[str, Any]:
    """The document on disk, once its schema is confirmed to still be schema 1.

    The record's copy is a memory of an earlier file. A migration — or
    another ow — may have replaced it since, and updating a schema-2 file
    with schema-1 fields, or resurrecting a migrated file from stale bytes,
    is exactly what a save must refuse instead of doing silently.
    """
    try:
        original = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError(f"{path} no longer exists; reload it before saving") from exc
    document = tomlkit.parse(original.decode("utf-8"))
    version = document.get("version", 1)
    if version != 1:
        raise ValueError(f"{path} is schema {version}, not schema 1; reload it before saving")
    return document


def _atomic_write(path: Path, data: bytes) -> None:
    """A same-directory temp file at mode 0600, flushed, then `os.replace`d.

    Same directory, so the rename cannot cross a filesystem; mode 0600 on
    the temp file, so the replacement is private from the first instant and
    never briefly inherits the old file's permissions. A crash leaves a temp
    file at worst, never a truncated config.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def write_workspace_config(path: Path, ws: WorkspaceConfig) -> WorkspaceConfig:
    """Write `.ow/config.toml`, replacing it atomically at mode 0600.

    A schema-2 record is serialized whole. A schema-1 record is not: its
    destination is re-read first, its schema must still be 1, and only
    `[repos]` is updated in the document that is actually there — comments
    and unknown data survive, and a file migrated meanwhile is refused
    rather than reverted.

    Returns the record to keep using: for a schema-1 record this is `ws`
    with `legacy.original` refreshed to the bytes just written, so a caller
    holding the object — the TUI, a second save in the same command — saves
    from what is actually on disk rather than from what it was when this
    record was loaded. A caller that discards the return value and reloads
    instead gets the same document either way; one that keeps the *old*
    object and saves it again would silently reapply stale editor/theme/
    remotes over an edit that landed in between, which is exactly the
    revert this return value exists to prevent. A schema-2 record is
    returned unchanged — it carries no legacy evidence to refresh.
    """
    if ws.version == 1 and ws.legacy is not None:
        _reject_typed_change(ws.legacy, odoo=ws.odoo, mise=ws.mise)
        document = _reload_legacy_document(path)
        _set_repos(document, ws.repos)
        data = tomlkit.dumps(document).encode("utf-8")
    else:
        data = dumps_workspace_config(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, data)
    if ws.version == 1 and ws.legacy is not None:
        return replace(ws, legacy=LegacyConfig(source=path, original=data, issues=ws.legacy.issues))
    return ws


def write_global_config(config: Config) -> Config:
    """Rewrite `$XDG_CONFIG_HOME/ow/config.toml` to match `config`.

    Same contract as `write_workspace_config`, returned record included: a
    schema-1 config comes back with `legacy.original` refreshed to the
    bytes just written, so a repeated save — the TUI's theme picker is the
    caller this matters for — starts from the file as it now is rather than
    reapplying a save-time-stale document over an edit made in between.
    """
    path = paths.config_file()
    if config.version == 1 and config.legacy is not None:
        _reject_typed_change(
            config.legacy, odoo=config.odoo, mise=config.mise, owignore=config.owignore
        )
        document = _reload_legacy_document(path)
        _set_global_legacy_fields(document, config)
        data = tomlkit.dumps(document).encode("utf-8")
    else:
        data = dumps_global_config(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, data)
    if config.version == 1 and config.legacy is not None:
        return replace(
            config, legacy=LegacyConfig(source=path, original=data, issues=config.legacy.issues)
        )
    return config


def select_aliases(available: list[str], only: str | None) -> list[str]:
    """Filter repo aliases by --only, preserving the order of the config.

    Shared by every command whose --only narrows a workspace-wide operation
    down to specific repos (`ow rebase`, `ow switch`). typer.BadParameter is
    deliberate: Typer renders it as a usage error and exit code 2, where a
    bare SystemExit would look like the operation itself had failed.
    """
    if only is None:
        return list(available)
    wanted = [a.strip() for a in only.split(",") if a.strip()]
    if not wanted:
        # --only '' , --only ',' and --only ' ' all land here. Narrowing to
        # nothing is a mistake, not a request to do nothing: without this the
        # command materializes or rebases no repo at all and still reports
        # success.
        raise typer.BadParameter(
            f"--only names no repo (got {only!r}). "
            f"Available: {', '.join(available)}"
        )
    unknown = [a for a in wanted if a not in available]
    if unknown:
        raise typer.BadParameter(
            f"unknown repo alias(es): {', '.join(unknown)}. "
            f"Available: {', '.join(available)}"
        )
    return [a for a in available if a in wanted]


def find_project_root(start: Path) -> Path | None:
    """Walk up from start to the nearest ow project root, or None."""
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "ow.toml").exists() or (candidate / "ow.toml.example").exists():
            return candidate
    return None


_reported_migrations: set[Path] = set()


def pending_migration(config: Config, ws: WorkspaceConfig, ws_dir: Path) -> tuple[str, ...]:
    """The message for configs still on schema 1, or () when none are.

    A report, never an action: only explicit `ow init`/`ow render` convert a
    file, so every other command says what to run instead of rewriting the
    user's manifest behind their back.
    """
    pending = tuple(
        record.legacy.source
        for record in (config, ws)
        if record.version == 1 and record.legacy is not None
    )
    if not pending:
        return ()
    sources = ", ".join(str(source) for source in pending)
    return (f"Pending migration: {sources} still use schema 1 — run `ow render -w {ws_dir}`",)


def report_pending_migration(config: Config, ws: WorkspaceConfig, ws_dir: Path) -> None:
    """Show the pending-migration message once per source, per invocation.

    Command orchestration calls this; loaders never print. Nothing is
    persisted and nothing is marked done: a command that only displays
    state may show the same condition again without suppressing the warning
    a mutation would print.
    """
    pending = tuple(
        record.legacy.source
        for record in (config, ws)
        if record.version == 1 and record.legacy is not None
    )
    fresh = tuple(source for source in pending if source not in _reported_migrations)
    if not fresh:
        return
    _reported_migrations.update(fresh)
    for line in pending_migration(config, ws, ws_dir):
        err_console.print(line, markup=False)
