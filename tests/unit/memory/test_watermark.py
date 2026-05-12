"""Compaction watermark read/write (MEMORY_SPEC §4.2 / M-I2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from eonlet.memory.watermark import read_watermark, write_watermark


def test_read_missing_returns_zero(tmp_path: Path) -> None:
    assert read_watermark(tmp_path) == 0


def test_round_trip(tmp_path: Path) -> None:
    write_watermark(tmp_path, 12345)
    assert read_watermark(tmp_path) == 12345


def test_round_trip_zero(tmp_path: Path) -> None:
    write_watermark(tmp_path, 0)
    assert read_watermark(tmp_path) == 0


def test_read_corrupt_returns_zero(tmp_path: Path) -> None:
    (tmp_path / "watermark").write_text("not-a-number")
    assert read_watermark(tmp_path) == 0


def test_read_empty_returns_zero(tmp_path: Path) -> None:
    (tmp_path / "watermark").write_text("")
    assert read_watermark(tmp_path) == 0


def test_write_rejects_negative(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_watermark(tmp_path, -1)


def test_overwrite_advances_value(tmp_path: Path) -> None:
    write_watermark(tmp_path, 100)
    write_watermark(tmp_path, 200)
    assert read_watermark(tmp_path) == 200
