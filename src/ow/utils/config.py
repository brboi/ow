import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w


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
    if ".." in spec:
        base, local = spec.split("..", 1)
        if "/" not in base:
            base = f"origin/{base}"
        return BranchSpec(base, local)
    if "/" in spec:
        return BranchSpec(spec)
    return BranchSpec(f"origin/{spec}")


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
    root_dir: Path


def load_workspace_config(path: Path) -> WorkspaceConfig:
    """Read a .ow/config TOML file from an individual workspace."""
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
    """Write a .ow/config TOML file for an individual workspace."""
    data: dict[str, Any] = {
        "templates": ws.templates,
        "repos": {alias: spec.to_spec_str() for alias, spec in ws.repos.items()},
    }
    if ws.vars:
        data["vars"] = ws.vars

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)


def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    vars = data.get("vars", {})

    remotes: dict[str, dict[str, RemoteConfig]] = {}
    for alias, remote_dict in data.get("remotes", {}).items():
        remotes[alias] = {}
        for remote_name, remote_cfg in remote_dict.items():
            remotes[alias][remote_name] = RemoteConfig(
                url=remote_cfg["url"],
                pushurl=remote_cfg.get("pushurl"),
                fetch=remote_cfg.get("fetch"),
            )

    return Config(
        vars=vars,
        remotes=remotes,
        root_dir=path.parent,
    )
