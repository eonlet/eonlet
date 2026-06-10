"""Configuration for the context-trace subsystem (ADR-0010)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class TraceConfig(BaseModel):
    """Top-level ``trace`` block in ``agent.yaml``.

    Off by default: the trace file grows without bound (deltas per call, a
    full snapshot per fork) and is pure observability — nothing in the
    runtime reads it back.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
