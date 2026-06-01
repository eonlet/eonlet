"""Pydantic schema for the ``memory:`` block in ``agent.yaml``.

Implements MEMORY_SPEC §8. Legacy fields (``notes_files``,
``recent_messages_in_context``) are rejected at load time per MEMORY_SPEC §5.7
— there is no deprecation window.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..errors import ConfigError

# Legacy field names that early v0.0.x agents shipped. Refused outright by the
# loader — pre-alpha has no migration path, just rewrite the agent.yaml.
_LEGACY_FIELDS: frozenset[str] = frozenset({"notes_files", "recent_messages_in_context"})


class EpisodicMemoryConfig(BaseModel):
    """Working/STM/LTM token budgets and compaction thresholds."""

    model_config = ConfigDict(extra="forbid")

    working_memory_tokens: int = Field(default=10_000, ge=64)
    keep_recent_messages_min: int = Field(default=4, ge=1)
    short_term_tokens: int = Field(default=4_000, ge=64)
    long_term_tokens: int = Field(default=8_000, ge=64)
    auto_compact: bool = True

    # Agent-proposed semantic compaction (ADR-0006). These are validated and
    # defaulted now (v0.0.9 M1) but only consumed once the propose_compact
    # action + consent round-trip land (M3).
    propose_semantic: bool = True
    propose_floor_tokens: int = Field(default=5_000, ge=64)
    propose_min_interval_seconds: int = Field(default=1_800, ge=0)
    propose_min_turns: int = Field(default=3, ge=0)


class KnowledgeMemoryConfig(BaseModel):
    """Curated-knowledge axis (ADR-0005, axis 2).

    The knowledge tree's bodies stay on disk; only ``index.md`` is injected
    into context. ``index_max_tokens`` / ``warn_file_tokens`` drive non-fatal
    warnings when the map or a single file grows past a sane size.
    """

    model_config = ConfigDict(extra="forbid")

    inject_index: bool = True
    index_max_tokens: int = Field(default=2_000, ge=128)
    warn_file_tokens: int = Field(default=4_000, ge=128)


class MemoryConfig(BaseModel):
    """Top-level ``memory:`` block.

    See MEMORY_SPEC §8 for the full schema. When ``enabled`` is False, the
    runtime skips preamble injection and compaction entirely (§9).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    compaction_model: str = "claude-haiku-4-5@anthropic"

    # Prefix every user message rendered into the working window with its local
    # datetime (ADR-0006), giving the model temporal awareness of when episodes
    # happened. Render-time only — never written into event payloads.
    inject_turn_timestamps: bool = True

    episodic: EpisodicMemoryConfig = Field(default_factory=EpisodicMemoryConfig)
    knowledge: KnowledgeMemoryConfig = Field(default_factory=KnowledgeMemoryConfig)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy(cls, data: Any) -> Any:
        if isinstance(data, dict):
            offenders = sorted(_LEGACY_FIELDS & data.keys())
            if offenders:
                raise ConfigError(
                    "agent.yaml: memory."
                    f"{offenders[0]} is no longer supported "
                    "(MEMORY_SPEC §5.7). Remove it. Offending fields: " + ", ".join(offenders)
                )
        return data

    def recent_message_count(self) -> int:
        """Transitional count used by the runtime until token-driven injection
        (P4) lands. Returns a budget-derived message count clamped above the
        configured floor. Roughly 250 tokens per message is a generous heuristic
        for mixed user/assistant/tool turns.
        """
        budget_estimate = max(self.episodic.working_memory_tokens // 250, 1)
        return max(self.episodic.keep_recent_messages_min, min(budget_estimate, 200))
