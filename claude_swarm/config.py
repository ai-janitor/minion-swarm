from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Literal, Optional

import yaml

ProviderName = Literal["claude", "codex", "opencode", "gemini"]


@dataclass(frozen=True)
class AgentConfig:
    name: str
    role: str
    zone: str
    provider: ProviderName
    system: str
    allowed_tools: Optional[str]
    permission_mode: Optional[str]
    model: Optional[str]
    max_history_tokens: int
    no_output_timeout_sec: int
    retry_backoff_sec: int
    retry_backoff_max_sec: int


@dataclass(frozen=True)
class SwarmConfig:
    config_path: Path
    project_dir: Path
    dead_drop_dir: Path
    dead_drop_db: Path
    agents: Dict[str, AgentConfig]

    @property
    def runtime_dir(self) -> Path:
        return self.project_dir / ".claude-swarm"

    @property
    def logs_dir(self) -> Path:
        return self.runtime_dir / "logs"

    @property
    def pids_dir(self) -> Path:
        return self.runtime_dir / "pids"

    @property
    def state_dir(self) -> Path:
        return self.runtime_dir / "state"

    def ensure_runtime_dirs(self) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.pids_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)


def _resolve_path(raw_value: str, base: Path) -> Path:
    path = Path(raw_value).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def load_config(config_path: str | Path) -> SwarmConfig:
    cfg_path = Path(config_path).expanduser().resolve()
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config not found: {cfg_path}")

    raw = yaml.safe_load(cfg_path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError("Top-level config must be a YAML mapping")

    project_dir = _resolve_path(str(raw.get("project_dir", cfg_path.parent)), cfg_path.parent)
    dead_drop_dir = _resolve_path(str(raw.get("dead_drop_dir", ".dead-drop")), project_dir)

    db_default = os.environ.get("DEAD_DROP_DB_PATH", "~/.dead-drop/messages.db")
    dead_drop_db = _resolve_path(str(raw.get("dead_drop_db", db_default)), cfg_path.parent)

    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, dict) or not agents_raw:
        raise ValueError("Config must define a non-empty 'agents' mapping")

    agents: Dict[str, AgentConfig] = {}
    for name, item in agents_raw.items():
        if not isinstance(item, dict):
            raise ValueError(f"Agent '{name}' config must be a mapping")

        provider = str(item.get("provider", "claude")).strip().lower()
        if provider not in {"claude", "codex", "opencode", "gemini"}:
            raise ValueError(
                f"Agent '{name}' has invalid provider '{provider}'. "
                "Expected one of: claude, codex, opencode, gemini."
            )

        role = str(item.get("role", "coder"))
        zone = str(item.get("zone", ""))
        system = str(item.get("system", "")).strip()
        if not system:
            system = (
                f"You are {name} ({role}) running under claude-swarm. "
                "Check dead-drop inbox, execute tasks, and report via dead-drop."
            )

        allowed_tools = item.get("allowed_tools")
        if allowed_tools is not None:
            allowed_tools = str(allowed_tools)

        permission_mode = item.get("permission_mode")
        if permission_mode is not None:
            permission_mode = str(permission_mode).strip()
            if not permission_mode:
                permission_mode = None

        model = item.get("model")
        if model is not None:
            model = str(model)

        max_history_tokens = int(item.get("max_history_tokens", 100_000))
        no_output_timeout_sec = int(item.get("no_output_timeout_sec", 600))
        retry_backoff_sec = int(item.get("retry_backoff_sec", 30))
        retry_backoff_max_sec = int(item.get("retry_backoff_max_sec", 300))

        agents[str(name)] = AgentConfig(
            name=str(name),
            role=role,
            zone=zone,
            provider=provider,  # type: ignore[arg-type]
            system=system,
            allowed_tools=allowed_tools,
            permission_mode=permission_mode,
            model=model,
            max_history_tokens=max_history_tokens,
            no_output_timeout_sec=no_output_timeout_sec,
            retry_backoff_sec=retry_backoff_sec,
            retry_backoff_max_sec=retry_backoff_max_sec,
        )

    return SwarmConfig(
        config_path=cfg_path,
        project_dir=project_dir,
        dead_drop_dir=dead_drop_dir,
        dead_drop_db=dead_drop_db,
        agents=agents,
    )
