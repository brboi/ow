import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w
import typer

from ow.utils import paths
from ow.utils.display import err_console


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
        base = self.branch if self.remote == "origin" else self.base_ref
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
    if not base or not local or ".." in local or " " in base or " " in local:
        raise ValueError(f"invalid branch spec: {spec!r}")
    if "/" not in base:
        base = f"origin/{base}"
    return BranchSpec(base, local)


@dataclass
class RemoteConfig:
    url: str
    pushurl: str | None = None
    fetch: str | None = None


@dataclass
class WorkspaceConfig:
    repos: dict[str, BranchSpec]
    templates: list[str]
    vars: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    vars: dict[str, Any]
    remotes: dict[str, dict[str, RemoteConfig]]  # alias -> remote_name -> cfg


def load_workspace_config(path: Path) -> WorkspaceConfig:
    """Read the .ow/config.toml file from an individual workspace."""
    with open(path, "rb") as f:
        data = tomllib.load(f)

    repos = {}
    for alias, spec_str in data.get("repos", {}).items():
        repos[alias] = parse_branch_spec(spec_str)

    templates = data.get("templates")
    if templates is None:
        raise ValueError(f"Workspace config '{path}' missing required 'templates' field")
    if not isinstance(templates, list):
        raise ValueError(f"Workspace config '{path}' 'templates' must be a list")

    return WorkspaceConfig(
        repos=repos,
        templates=templates,
        vars=data.get("vars", {}),
    )


def write_workspace_config(path: Path, ws: WorkspaceConfig) -> None:
    """Write the .ow/config.toml file for an individual workspace."""
    data: dict[str, Any] = {
        "templates": ws.templates,
        "repos": {alias: spec.to_spec_str() for alias, spec in ws.repos.items()},
    }
    if ws.vars:
        data["vars"] = ws.vars

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)


def select_aliases(available: list[str], only: str | None) -> list[str]:
    """Filter repo aliases by --only, preserving the order of the config.

    Shared by every command whose --only narrows a workspace-wide operation
    down to specific repos (`ow rebase`, `ow apply`). typer.BadParameter is
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


def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    vars = data.get("vars", {})

    remotes: dict[str, dict[str, RemoteConfig]] = {}
    for alias, remote_dict in data.get("remotes", {}).items():
        if not isinstance(remote_dict, dict):
            raise ValueError(
                f"[remotes.{alias}] must be a table of remote entries"
            )
        remotes[alias] = {}
        for remote_name, remote_cfg in remote_dict.items():
            if not isinstance(remote_cfg, dict) or "url" not in remote_cfg:
                raise ValueError(
                    f"[remotes.{alias}.{remote_name}] must have a 'url' key"
                )
            remotes[alias][remote_name] = RemoteConfig(
                url=remote_cfg["url"],
                pushurl=remote_cfg.get("pushurl"),
                fetch=remote_cfg.get("fetch"),
            )

    return Config(
        vars=vars,
        remotes=remotes,
    )


_DEFAULT_CONFIG = '''\
# ow configuration. Everything here is optional except at least one remote.

[vars]
http_port = 8069
db_host = "localhost"
db_port = 5432
db_user = "odoo"
db_password = "odoo"
admin_passwd = "Password"
# smtp_server = "mailpit"
# smtp_port = 1025

[remotes.community]
origin.url = "git@github.com:odoo/odoo.git"
# dev.url = "git@github.com:odoo-dev/odoo.git"
# dev.pushurl = "git@github.com:odoo-dev/odoo.git"
# dev.fetch = "+refs/heads/*:refs/remotes/dev/*"

# [remotes.enterprise]
# origin.url = "git@github.com:odoo/enterprise.git"
'''


def load_global_config() -> Config:
    """Read the user's config, creating a commented default on first use.

    Written on first run rather than at install time: pip must not create
    files outside site-packages, post-install hooks are skipped for wheels
    and isolated by pipx, and a package that writes into someone's home when
    installed is a behaviour people rightly complain about.
    """
    path = paths.config_file()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_DEFAULT_CONFIG, encoding="utf-8")
        err_console.print(f"Created {path} — edit it to add your remotes.")
    return load_config(path)
