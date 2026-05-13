"""Pydantic models for agent.yaml and the global config.yaml.

Implements AGENT_CONFIG_SPEC §2–§17 for what the MVP cares about.
Unknown top-level fields are warned-and-accepted per §17 (forward-compat).
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ConfigError

# ── Duration parsing ──────────────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^\s*(\d+)\s*(s|m|h|d)\s*$", re.IGNORECASE)


def parse_duration(value: str | int | float) -> float:
    """Parse `30s`, `5m`, `2h`, `1d` → seconds. Plain numbers treated as seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    m = _DURATION_RE.match(value)
    if not m:
        raise ConfigError(f"Invalid duration: {value!r} (expected like '30s', '5m', '2h', '1d')")
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


# ── Sub-models ────────────────────────────────────────────────────────────────


class Metadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str
    version: str
    authors: list[str] = []
    tags: list[str] = []
    specialty: str | None = None
    capabilities: list[str] = []
    homepage: str | None = None

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        if not re.match(r"^\d+\.\d+\.\d+([+-].*)?$", v):
            raise ConfigError(f"metadata.version must be SemVer, got {v!r}")
        return v


class Budget(BaseModel):
    daily_usd: float | None = None
    monthly_usd: float | None = None
    on_exceed: Literal["warn", "kill", "pause"] = "warn"


class Runtime(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    fallback_model: str | None = None
    max_context_tokens: int = 180_000
    max_steps_per_run: int = 200
    max_wall_clock_per_run: str = "1h"
    budget: Budget = Field(default_factory=Budget)

    @property
    def max_wall_clock_seconds(self) -> float:
        return parse_duration(self.max_wall_clock_per_run)


class CronTrigger(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: Literal["cron"] = "cron"
    schedule: str
    timezone: str
    message: str
    grace_period: str = "1h"
    enabled: bool = True
    max_concurrent_runs: int = 1
    max_queued: int = 1

    @property
    def grace_period_seconds(self) -> float:
        return parse_duration(self.grace_period)


class Tools(BaseModel):
    builtin: list[str] = []
    custom: list[str] = []


class Permissions(BaseModel):
    mode: Literal["ask", "yolo"] = "ask"
    extra_deny: list[str] = []


class Memory(BaseModel):
    recent_messages_in_context: int = 30
    notes_files: list[str] = Field(default_factory=lambda: ["notes.md"])


class Env(BaseModel):
    required: list[str] = []
    optional: list[str] = []
    defaults: dict[str, str] = {}


class Lifecycle(BaseModel):
    model_config = ConfigDict(extra="allow")

    idle_timeout: str | None = None
    on_crash: Literal["warn", "exit"] = "exit"


# ── Top-level ─────────────────────────────────────────────────────────────────


class AgentConfig(BaseModel):
    """Parsed `agent.yaml`. Forward-compat: unknown sections accepted with warning."""

    model_config = ConfigDict(extra="allow")

    apiVersion: str = "eonlet/v1"
    kind: str = "Agent"
    metadata: Metadata
    runtime: Runtime
    triggers: list[CronTrigger] = []
    tools: Tools
    permissions: Permissions = Field(default_factory=Permissions)
    memory: Memory = Field(default_factory=Memory)
    env: Env = Field(default_factory=Env)
    lifecycle: Lifecycle = Field(default_factory=Lifecycle)

    @model_validator(mode="after")
    def _post(self) -> AgentConfig:
        if self.apiVersion != "eonlet/v1":
            raise ConfigError(f"apiVersion must be 'eonlet/v1', got {self.apiVersion!r}")
        if self.kind != "Agent":
            raise ConfigError(f"kind must be 'Agent', got {self.kind!r}")
        # Unique trigger ids
        seen: set[str] = set()
        for t in self.triggers:
            if t.id in seen:
                raise ConfigError(f"Duplicate trigger id: {t.id!r}")
            seen.add(t.id)
        return self


def load_agent_config(path: Path) -> AgentConfig:
    """Load `agent.yaml` from a file or definition directory."""
    p = path / "agent.yaml" if path.is_dir() else path
    if not p.exists():
        raise ConfigError(f"agent.yaml not found at {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {p}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: top-level must be a mapping")
    # Forward-compat: warn about unknown sections we won't read.
    _known = {
        "apiVersion",
        "kind",
        "metadata",
        "runtime",
        "triggers",
        "tools",
        "permissions",
        "memory",
        "env",
        "lifecycle",
        "outputs",
        "hooks",
        "observability",  # v0.2+ — silently ignored
    }
    for key in data:
        if key not in _known:
            warnings.warn(f"Unknown top-level field in {p}: {key!r}", stacklevel=2)
    try:
        cfg = AgentConfig.model_validate(data)
    except Exception as e:
        raise ConfigError(f"Validation failed for {p}: {e}") from e
    # Directory-name == metadata.name (only when loading from a dir)
    if path.is_dir() and path.name != cfg.metadata.name:
        raise ConfigError(
            f"{p}: metadata.name ({cfg.metadata.name!r}) must match directory name ({path.name!r})"
        )
    return cfg


# ── Global config ─────────────────────────────────────────────────────────────


class GlobalConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    defaults: dict[str, Any] = {}
    providers: dict[str, dict[str, Any]] = Field(
        default_factory=lambda: {
            "anthropic": {"api_key_env": "ANTHROPIC_API_KEY"},
            "openai": {
                "api_key_env": "OPENAI_API_KEY",
                "base_url_env": "OPENAI_BASE_URL",
            },
        }
    )
    editor: str = "vim"
    logging: dict[str, Any] = Field(default_factory=lambda: {"level": "info"})


def load_global_config(path: Path | None = None) -> GlobalConfig:
    from .paths import config_path

    p = path or config_path()
    if not p.exists():
        return GlobalConfig()
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {p}: {e}") from e
    return GlobalConfig.model_validate(data)
