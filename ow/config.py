from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    name: str
    repos: dict[str, BranchSpec]
    vars: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    vars: dict[str, Any]
    remotes: dict[str, dict[str, RemoteConfig]]  # alias -> remote_name -> cfg
    workspaces: list[WorkspaceConfig]
    root_dir: Path


def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    vars_ = data.get("vars", {})

    remotes: dict[str, dict[str, RemoteConfig]] = {}
    for alias, remote_dict in data.get("remotes", {}).items():
        remotes[alias] = {}
        for remote_name, remote_cfg in remote_dict.items():
            remotes[alias][remote_name] = RemoteConfig(
                url=remote_cfg["url"],
                pushurl=remote_cfg.get("pushurl"),
                fetch=remote_cfg.get("fetch"),
            )

    workspaces = []
    for ws_data in data.get("workspace", []):
        repos = {}
        for alias, spec_str in ws_data.get("repo", {}).items():
            repos[alias] = parse_branch_spec(spec_str)
        workspaces.append(WorkspaceConfig(
            name=ws_data["name"],
            repos=repos,
            vars=ws_data.get("vars", {}),
        ))

    return Config(
        vars=vars_,
        remotes=remotes,
        workspaces=workspaces,
        root_dir=path.parent,
    )


def format_workspace(ws: WorkspaceConfig) -> str:
    lines = ["[[workspace]]", f'name = "{ws.name}"']
    for alias, spec in ws.repos.items():
        lines.append(f'repo.{alias} = "{spec.to_spec_str()}"')
    for k, v in ws.vars.items():
        if isinstance(v, str):
            lines.append(f'vars.{k} = "{v}"')
        else:
            lines.append(f'vars.{k} = {v}')
    return "\n".join(lines) + "\n"


def update_config_workspaces(path: Path, workspaces: list[WorkspaceConfig]) -> None:
    """Rewrite workspace sections, preserving the preamble."""
    text = path.read_text()
    lines = text.splitlines(keepends=True)

    split_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "[[workspace]]":
            split_idx = i
            break

    preamble = "".join(lines[:split_idx]) if split_idx is not None else text
    ws_text = "\n".join(format_workspace(ws) for ws in workspaces)
    path.write_text(preamble + ws_text)


def archive_workspace(config_path: Path, ws: WorkspaceConfig) -> None:
    """Append workspace entry to the archived workspaces file."""
    archive_path = config_path.parent / ".ow.toml.archived-workspaces"
    with open(archive_path, "a") as f:
        f.write(format_workspace(ws))
