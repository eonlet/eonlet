"""Eonlet — A local-first runtime for stateful AI agents.

Top-level package. The public surface for *consumers* is intentionally narrow;
most code lives inside subpackages (``runtime``, ``tools``, ``llm``, ``worker``,
``cli``). Custom tool authors should import from ``eonlet.tools``.
"""

from __future__ import annotations

__version__ = "0.0.1"
__spec_version__ = "eonlet/v1"
