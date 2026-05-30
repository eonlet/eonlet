"""MemoryConfig schema, defaults, and legacy-field rejection (MEMORY_SPEC §8)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eonlet.errors import ConfigError
from eonlet.memory.config import (
    EpisodicMemoryConfig,
    KnowledgeMemoryConfig,
    MemoryConfig,
)


def test_defaults_round_trip() -> None:
    cfg = MemoryConfig()
    assert cfg.enabled is True
    assert cfg.compaction_model.startswith("claude-haiku")
    assert isinstance(cfg.episodic, EpisodicMemoryConfig)
    assert isinstance(cfg.knowledge, KnowledgeMemoryConfig)
    # Defaults from MEMORY_SPEC §8
    assert cfg.episodic.working_memory_tokens == 10_000
    assert cfg.episodic.keep_recent_messages_min == 4
    assert cfg.episodic.short_term_tokens == 4_000
    assert cfg.episodic.long_term_tokens == 8_000
    assert cfg.episodic.auto_compact is True
    assert cfg.knowledge.inject_index is True
    assert cfg.knowledge.index_max_tokens == 2_000
    assert cfg.knowledge.warn_file_tokens == 4_000
    # ADR-0006 trigger-model fields.
    assert cfg.inject_turn_timestamps is True
    assert cfg.episodic.propose_semantic is True
    assert cfg.episodic.propose_floor_tokens == 5_000
    assert cfg.episodic.propose_min_interval_seconds == 1_800
    assert cfg.episodic.propose_min_turns == 3


def test_propose_fields_validate_bounds() -> None:
    # Zero cooldowns are allowed; negative is rejected; floor honors ge=64.
    EpisodicMemoryConfig(propose_min_interval_seconds=0, propose_min_turns=0)
    with pytest.raises(ValidationError):
        EpisodicMemoryConfig(propose_min_interval_seconds=-1)
    with pytest.raises(ValidationError):
        EpisodicMemoryConfig(propose_floor_tokens=10)


def test_legacy_notes_block_rejected() -> None:
    # The whole `memory.notes` block is gone (ADR-0005); extra='forbid' rejects it.
    with pytest.raises(ValidationError):
        MemoryConfig.model_validate({"notes": {"max_tokens": 1000}})


def test_legacy_todos_block_rejected() -> None:
    # `memory.todos` moved to the top-level `tasks:` block (ADR-0005).
    with pytest.raises(ValidationError):
        MemoryConfig.model_validate({"todos": {"inject_active": False}})


def test_partial_override_keeps_defaults() -> None:
    cfg = MemoryConfig.model_validate({"episodic": {"working_memory_tokens": 20_000}})
    assert cfg.episodic.working_memory_tokens == 20_000
    # Other conversation fields keep defaults
    assert cfg.episodic.short_term_tokens == 4_000


def test_legacy_notes_files_rejected() -> None:
    with pytest.raises(ConfigError, match="notes_files"):
        MemoryConfig.model_validate({"notes_files": ["notes.md"]})


def test_legacy_recent_messages_in_context_rejected() -> None:
    with pytest.raises(ConfigError, match="recent_messages_in_context"):
        MemoryConfig.model_validate({"recent_messages_in_context": 30})


def test_unknown_field_rejected() -> None:
    # extra='forbid' on MemoryConfig itself
    with pytest.raises(ValidationError):
        MemoryConfig.model_validate({"made_up_field": True})


def test_disabled_construction() -> None:
    cfg = MemoryConfig(enabled=False)
    assert cfg.enabled is False
    # Sub-configs still present with defaults; the runtime is responsible for
    # ignoring them when enabled is False.
    assert cfg.episodic.working_memory_tokens == 10_000


def test_recent_message_count_floors_at_keep_min() -> None:
    # tiny budget should not return less than keep_recent_messages_min
    cfg = MemoryConfig.model_validate(
        {
            "episodic": {
                "working_memory_tokens": 512,
                "keep_recent_messages_min": 10,
            }
        }
    )
    assert cfg.recent_message_count() == 10


def test_recent_message_count_scales_with_budget() -> None:
    cfg = MemoryConfig.model_validate({"episodic": {"working_memory_tokens": 10_000}})
    # ~10000/250 = 40
    assert cfg.recent_message_count() == 40


def test_validation_floors() -> None:
    with pytest.raises(ValidationError):
        EpisodicMemoryConfig(working_memory_tokens=0)
    with pytest.raises(ValidationError):
        EpisodicMemoryConfig(keep_recent_messages_min=0)
    with pytest.raises(ValidationError):
        KnowledgeMemoryConfig(index_max_tokens=0)
