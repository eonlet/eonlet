"""Provider selection from a model string.

Per AGENT_CONFIG_SPEC §4: ``claude-*`` → Anthropic, everything else → OpenAI-compatible.
"""

from __future__ import annotations

from ..errors import ConfigError
from .protocol import LLMProvider


def build_provider(model: str) -> LLMProvider:
    if not model:
        raise ConfigError("runtime.model is empty")
    if model.startswith("fake-"):
        # In-process deterministic provider — testing only, no network.
        from .fake_provider import FakeProvider

        return FakeProvider(model)
    if model.startswith("claude-"):
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(model)
    # Everything else: OpenAI / OpenAI-compatible.
    from .openai_provider import OpenAIProvider

    return OpenAIProvider(model)
