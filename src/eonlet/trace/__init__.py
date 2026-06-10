"""Context trace — lineage-aware recording of LLM requests (ADR-0010)."""

from .config import TraceConfig
from .html import render_html
from .recorder import TRACE_FILENAME, ContextTracer, fold_line, read_trace

__all__ = [
    "TRACE_FILENAME",
    "ContextTracer",
    "TraceConfig",
    "fold_line",
    "read_trace",
    "render_html",
]
