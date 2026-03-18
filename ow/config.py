from __future__ import annotations

import json
import re
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
    templates: list[str]
    vars: dict[str, Any] = field(default_factory=dict)
    _source_text: str | None = field(default=None, repr=False, compare=False)


@dataclass
class Config:
    vars: dict[str, Any]
    remotes: dict[str, dict[str, RemoteConfig]]  # alias -> remote_name -> cfg
    workspaces: list[WorkspaceConfig]
    root_dir: Path


def _split_workspace_blocks(text: str) -> tuple[str, list[str]]:
    parts = re.split(r'(?=^\[\[workspace\]\])', text, flags=re.MULTILINE)
    return parts[0], parts[1:]


def load_config(path: Path) -> Config:
    text = path.read_text()
    with open(path, "rb") as f:
        data = tomllib.load(f)

    _, raw_blocks = _split_workspace_blocks(text)

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
    for i, ws_data in enumerate(data.get("workspace", [])):
        repos = {}
        for alias, spec_str in ws_data.get("repo", {}).items():
            repos[alias] = parse_branch_spec(spec_str)
        templates = ws_data.get("templates")
        if templates is None:
            raise ValueError(f"Workspace '{ws_data.get('name', '<unknown>')}' missing required 'templates' field")
        if not isinstance(templates, list) or not templates:
            raise ValueError(f"Workspace '{ws_data.get('name', '<unknown>')}' 'templates' must be a non-empty list")
        workspaces.append(WorkspaceConfig(
            name=ws_data["name"],
            repos=repos,
            templates=templates,
            vars=ws_data.get("vars", {}),
            _source_text=raw_blocks[i] if i < len(raw_blocks) else None,
        ))

    return Config(
        vars=vars_,
        remotes=remotes,
        workspaces=workspaces,
        root_dir=path.parent,
    )


def format_workspace(ws: WorkspaceConfig) -> str:
    lines = ["[[workspace]]", f'name = "{ws.name}"', f'templates = {json.dumps(ws.templates)}']
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
    preamble, _ = _split_workspace_blocks(text)
    parts = [ws._source_text if ws._source_text is not None else format_workspace(ws)
             for ws in workspaces]
    path.write_text(preamble + "".join(parts))


def archive_workspace(config_path: Path, ws: WorkspaceConfig) -> None:
    """Append workspace entry to the archived workspaces file."""
    raw = ws._source_text or format_workspace(ws)
    archive_path = config_path.parent / ".ow.toml.archived-workspaces"
    with open(archive_path, "a") as f:
        f.write(raw)
