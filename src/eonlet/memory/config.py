"""Pydantic schema for the ``memory:`` block in ``agent.yaml``.

Implements MEMORY_SPEC §8. Legacy fields (``notes_files``,
``recent_messages_in_context``) are rejected at load time per MEMORY_SPEC §5.7
— there is no deprecation window.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..errors import ConfigError

# Legacy field names that v0.0.x agents shipped. Refused outright by the
# loader; the migration tool (`eonlet memory migrate`) handles conversion.
_LEGACY_FIELDS: frozenset[str] = frozenset({"notes_files", "recent_messages_in_context"})


class ConversationMemoryConfig(BaseModel):
    """Working/STM/LTM token budgets and compaction thresholds."""

    model_config = ConfigDict(extra="forbid")

    working_memory_tokens: int = Field(default=10_000, ge=64)
    keep_recent_messages_min: int = Field(default=4, ge=1)
    short_term_tokens: int = Field(default=4_000, ge=64)
    long_term_tokens: int = Field(default=8_000, ge=64)
    auto_compact: bool = True


class NotesMemoryConfig(BaseModel):
    """Notes (user-curated, never auto-deleted) budget."""

    model_config = ConfigDict(extra="forbid")

    max_tokens: int = Field(default=4_000, ge=128)
    inject: bool = True


class TodosMemoryConfig(BaseModel):
    """TODOs injection and archival policy."""

    model_config = ConfigDict(extra="forbid")

    inject_active: bool = True
    # 0 disables archival; otherwise done items older than N days are moved
    # to todos.archive.jsonl on the periodic sweep (MEMORY_SPEC §10).
    archive_done_after_days: int = Field(default=30, ge=0)


class MemoryConfig(BaseModel):
    """Top-level ``memory:`` block.

    See MEMORY_SPEC §8 for the full schema. When ``enabled`` is False, the
    runtime skips preamble injection and compaction entirely (§9).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    compaction_model: str = "claude-haiku-4-5@anthropic"

    conversation: ConversationMemoryConfig = Field(default_factory=ConversationMemoryConfig)
    notes: NotesMemoryConfig = Field(default_factory=NotesMemoryConfig)
    todos: TodosMemoryConfig = Field(default_factory=TodosMemoryConfig)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy(cls, data: Any) -> Any:
        if isinstance(data, dict):
            offenders = sorted(_LEGACY_FIELDS & data.keys())
            if offenders:
                raise ConfigError(
                    "agent.yaml: memory."
                    f"{offenders[0]} is no longer supported "
                    "(MEMORY_SPEC §5.7). Remove it and migrate notes/todos via "
                    "`eonlet memory migrate`. Offending fields: " + ", ".join(offenders)
                )
        return data

    def recent_message_count(self) -> int:
        """Transitional count used by the runtime until token-driven injection
        (P4) lands. Returns a budget-derived message count clamped above the
        configured floor. Roughly 250 tokens per message is a generous heuristic
        for mixed user/assistant/tool turns.
        """
        budget_estimate = max(self.conversation.working_memory_tokens // 250, 1)
        return max(self.conversation.keep_recent_messages_min, min(budget_estimate, 200))
