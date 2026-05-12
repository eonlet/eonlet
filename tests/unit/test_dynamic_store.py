"""Tests for triggers/dynamic_store.py."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from eonlet.config import CronTrigger, OnceTrigger
from eonlet.errors import ConfigError
from eonlet.triggers.dynamic_store import (
    DYNAMIC_ID_PREFIX,
    DynamicOnceRecord,
    DynamicTriggerRecord,
    DynamicTriggerStore,
    is_dynamic_id,
    mint_dynamic_id,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def test_is_dynamic_id_true() -> None:
    assert is_dynamic_id("dyn-2026-05-22-ab12") is True


def test_is_dynamic_id_false() -> None:
    assert is_dynamic_id("static-cron") is False
    assert is_dynamic_id("") is False


def test_mint_dynamic_id_format() -> None:
    tid = mint_dynamic_id()
    assert tid.startswith(DYNAMIC_ID_PREFIX)
    parts = tid[len(DYNAMIC_ID_PREFIX) :].split("-")
    # format: YYYY-MM-DD-xxxx
    assert len(parts) == 4
    assert len(parts[3]) == 4  # 4 hex chars


def test_mint_dynamic_id_unique() -> None:
    ids = {mint_dynamic_id() for _ in range(20)}
    assert len(ids) == 20


# ── DynamicTriggerRecord.to_json / from_json ──────────────────────────────────


def _make_cron_record(tid: str = "dyn-2026-05-22-ab01") -> DynamicTriggerRecord:
    trig = CronTrigger(
        id=tid,
        schedule="0 9 * * *",
        timezone="UTC",
        message="good morning",
        grace_period="1h",
        enabled=True,
    )
    return DynamicTriggerRecord(
        trig=trig, created_at="2026-05-22T09:00:00+00:00", created_by="agent"
    )


def _make_once_record(tid: str = "dyn-2026-05-22-ab02") -> DynamicOnceRecord:
    trig = OnceTrigger(
        id=tid,
        fire_at="2026-05-22T10:00:00+00:00",
        timezone="UTC",
        message="fire once",
        grace_period="30m",
        enabled=True,
    )
    return DynamicOnceRecord(trig=trig, created_at="2026-05-22T09:00:00+00:00", created_by="cli")


def test_cron_record_roundtrip() -> None:
    rec = _make_cron_record()
    d = rec.to_json()
    rec2 = DynamicTriggerRecord.from_json(d)
    assert rec2.trig.id == rec.trig.id
    assert rec2.trig.schedule == "0 9 * * *"
    assert rec2.created_by == "agent"


def test_cron_record_from_json_bad_prefix() -> None:
    rec = _make_cron_record()
    d = rec.to_json()
    d["id"] = "static-bad"
    with pytest.raises(ConfigError, match="prefix"):
        DynamicTriggerRecord.from_json(d)


def test_once_record_roundtrip() -> None:
    rec = _make_once_record()
    d = rec.to_json()
    rec2 = DynamicOnceRecord.from_json(d)
    assert rec2.trig.id == rec.trig.id
    assert rec2.trig.fire_at == "2026-05-22T10:00:00+00:00"
    assert rec2.created_by == "cli"


def test_once_record_from_json_bad_prefix() -> None:
    rec = _make_once_record()
    d = rec.to_json()
    d["id"] = "not-dyn-id"
    with pytest.raises(ConfigError, match="prefix"):
        DynamicOnceRecord.from_json(d)


# ── DynamicTriggerStore ────────────────────────────────────────────────────────


def test_store_load_missing_file(tmp_path: Path) -> None:
    store = DynamicTriggerStore(tmp_path)
    crons, once = store.load()
    assert crons == []
    assert once == []


def test_store_add_and_load(tmp_path: Path) -> None:
    store = DynamicTriggerStore(tmp_path)
    store.load()
    rec = _make_cron_record()
    anyio.run(store.add, rec)

    store2 = DynamicTriggerStore(tmp_path)
    crons, once = store2.load()
    assert len(crons) == 1
    assert crons[0].trig.id == rec.trig.id
    assert once == []


def test_store_add_once_and_load(tmp_path: Path) -> None:
    store = DynamicTriggerStore(tmp_path)
    store.load()
    rec = _make_once_record()
    anyio.run(store.add_once, rec)

    store2 = DynamicTriggerStore(tmp_path)
    _, once = store2.load()
    assert len(once) == 1
    assert once[0].trig.id == rec.trig.id


def test_store_add_duplicate_raises(tmp_path: Path) -> None:
    store = DynamicTriggerStore(tmp_path)
    store.load()
    rec = _make_cron_record()
    anyio.run(store.add, rec)
    with pytest.raises(ConfigError, match="duplicate"):
        anyio.run(store.add, rec)


def test_store_remove_cron(tmp_path: Path) -> None:
    store = DynamicTriggerStore(tmp_path)
    store.load()
    rec = _make_cron_record()
    anyio.run(store.add, rec)
    removed = anyio.run(store.remove, rec.trig.id)
    assert removed is True
    assert store.all() == []


def test_store_remove_once(tmp_path: Path) -> None:
    store = DynamicTriggerStore(tmp_path)
    store.load()
    rec = _make_once_record()
    anyio.run(store.add_once, rec)
    removed = anyio.run(store.remove, rec.trig.id)
    assert removed is True
    assert store.all_once() == []


def test_store_remove_missing_returns_false(tmp_path: Path) -> None:
    store = DynamicTriggerStore(tmp_path)
    store.load()
    removed = anyio.run(store.remove, "dyn-2026-05-22-0000")
    assert removed is False


def test_store_remove_non_dynamic_raises(tmp_path: Path) -> None:
    store = DynamicTriggerStore(tmp_path)
    store.load()
    with pytest.raises(ConfigError, match="refusing"):
        anyio.run(store.remove, "static-trigger")


def test_store_set_enabled_cron(tmp_path: Path) -> None:
    store = DynamicTriggerStore(tmp_path)
    store.load()
    rec = _make_cron_record()
    anyio.run(store.add, rec)
    ok = anyio.run(store.set_enabled, rec.trig.id, False)
    assert ok is True
    assert store.get(rec.trig.id).trig.enabled is False  # type: ignore[union-attr]


def test_store_set_enabled_once(tmp_path: Path) -> None:
    store = DynamicTriggerStore(tmp_path)
    store.load()
    rec = _make_once_record()
    anyio.run(store.add_once, rec)
    ok = anyio.run(store.set_enabled, rec.trig.id, False)
    assert ok is True
    assert store.get_once(rec.trig.id).trig.enabled is False  # type: ignore[union-attr]


def test_store_set_enabled_missing_returns_false(tmp_path: Path) -> None:
    store = DynamicTriggerStore(tmp_path)
    store.load()
    ok = anyio.run(store.set_enabled, "dyn-2026-01-01-0000", True)
    assert ok is False


def test_store_clear(tmp_path: Path) -> None:
    store = DynamicTriggerStore(tmp_path)
    store.load()
    anyio.run(store.add, _make_cron_record("dyn-2026-05-22-cc01"))
    anyio.run(store.add_once, _make_once_record("dyn-2026-05-22-cc02"))
    n = anyio.run(store.clear)
    assert n == 2
    assert store.all() == []
    assert store.all_once() == []


def test_store_clear_empty(tmp_path: Path) -> None:
    store = DynamicTriggerStore(tmp_path)
    store.load()
    n = anyio.run(store.clear)
    assert n == 0


def test_store_get_returns_none_for_missing(tmp_path: Path) -> None:
    store = DynamicTriggerStore(tmp_path)
    store.load()
    assert store.get("dyn-missing") is None
    assert store.get_once("dyn-missing") is None


def test_store_load_invalid_version(tmp_path: Path) -> None:
    import json

    (tmp_path / "dynamic_triggers.json").write_text(
        json.dumps({"version": 99, "triggers": [], "once": []}), encoding="utf-8"
    )
    store = DynamicTriggerStore(tmp_path)
    with pytest.raises(ConfigError, match="version"):
        store.load()
