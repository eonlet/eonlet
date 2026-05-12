"""MemoryConfig schema, defaults, and legacy-field rejection (MEMORY_SPEC §8)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eonlet.errors import ConfigError
from eonlet.memory.config import (
    ConversationMemoryConfig,
    MemoryConfig,
    NotesMemoryConfig,
    TodosMemoryConfig,
)


def test_defaults_round_trip() -> None:
    cfg = MemoryConfig()
    assert cfg.enabled is True
    assert cfg.compaction_model.startswith("claude-haiku")
    assert isinstance(cfg.conversation, ConversationMemoryConfig)
    assert isinstance(cfg.notes, NotesMemoryConfig)
    assert isinstance(cfg.todos, TodosMemoryConfig)
    # Defaults from MEMORY_SPEC §8
    assert cfg.conversation.working_memory_tokens == 10_000
    assert cfg.conversation.keep_recent_messages_min == 4
    assert cfg.conversation.short_term_tokens == 4_000
    assert cfg.conversation.long_term_tokens == 8_000
    assert cfg.conversation.auto_compact is True
    assert cfg.notes.max_tokens == 4_000
    assert cfg.notes.inject is True
    assert cfg.todos.inject_active is True
    assert cfg.todos.archive_done_after_days == 30


def test_partial_override_keeps_defaults() -> None:
    cfg = MemoryConfig.model_validate({"conversation": {"working_memory_tokens": 20_000}})
    assert cfg.conversation.working_memory_tokens == 20_000
    # Other conversation fields keep defaults
    assert cfg.conversation.short_term_tokens == 4_000


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
    assert cfg.conversation.working_memory_tokens == 10_000


def test_recent_message_count_floors_at_keep_min() -> None:
    # tiny budget should not return less than keep_recent_messages_min
    cfg = MemoryConfig.model_validate(
        {
            "conversation": {
                "working_memory_tokens": 512,
                "keep_recent_messages_min": 10,
            }
        }
    )
    assert cfg.recent_message_count() == 10


def test_recent_message_count_scales_with_budget() -> None:
    cfg = MemoryConfig.model_validate({"conversation": {"working_memory_tokens": 10_000}})
    # ~10000/250 = 40
    assert cfg.recent_message_count() == 40


def test_validation_floors() -> None:
    with pytest.raises(ValidationError):
        ConversationMemoryConfig(working_memory_tokens=0)
    with pytest.raises(ValidationError):
        ConversationMemoryConfig(keep_recent_messages_min=0)
    with pytest.raises(ValidationError):
        NotesMemoryConfig(max_tokens=0)
    with pytest.raises(ValidationError):
        TodosMemoryConfig(archive_done_after_days=-1)
