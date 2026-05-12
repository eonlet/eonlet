"""Provider selection from a model string.

``runtime.model`` accepts two forms:

1. **Explicit:** ``<model-name>@<provider>`` — e.g. ``gpt-4o@openai``,
   ``claude-sonnet-4-6@anthropic``, ``deepseek-chat@deepseek``.
   Built-in providers (``anthropic``, ``openai``, ``fake``) are handled
   directly.  Custom providers are looked up in ``config.yaml §providers``
   and resolved via :func:`resolve_model`.
2. **Prefix-inferred (legacy):** ``claude-*`` → Anthropic, ``fake-*`` → fake
   in-process provider, everything else → OpenAI-compatible.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from ..errors import ConfigError
from .protocol import LLMProvider

if TYPE_CHECKING:
    from ..config import GlobalConfig, ProviderConfig

# Providers handled without any config.yaml entry.
BUILTIN_PROVIDERS = ("anthropic", "openai", "fake")


def parse_model_string(model: str) -> tuple[str, str]:
    """Return ``(bare_model, provider)``.

    Validates format only — provider existence is checked at build time so
    that custom providers defined in ``config.yaml §providers`` are accepted.
    """
    if not model or not model.strip():
        raise ConfigError("runtime.model is empty")
    model = model.strip()
    if "@" in model:
        bare, _, provider = model.rpartition("@")
        bare = bare.strip()
        provider = provider.strip().lower()
        if not bare:
            raise ConfigError(
                f"runtime.model {model!r}: missing model name before '@' "
                f"(expected '<model>@<provider>')"
            )
        if not provider:
            raise ConfigError(f"runtime.model {model!r}: missing provider after '@'")
        return bare, provider
    # Prefix-based inference (legacy / convenience).
    if model.startswith("fake-"):
        return model, "fake"
    if model.startswith("claude-"):
        return model, "anthropic"
    return model, "openai"


def build_provider(model: str) -> LLMProvider:
    """Build a provider for a built-in provider name."""
    bare, provider = parse_model_string(model)
    if provider == "fake":
        from .fake_provider import FakeProvider

        return FakeProvider(bare)
    if provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(bare)
    if provider == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(bare)
    raise ConfigError(
        f"runtime.model {model!r}: unknown provider {provider!r}. "
        f"Built-in providers: {', '.join(BUILTIN_PROVIDERS)}. "
        f"For custom providers add an entry under 'providers:' in config.yaml."
    )


def _build_from_provider_config(
    bare_model: str, provider_name: str, cfg: ProviderConfig
) -> LLMProvider:
    """Build a provider from a ``ProviderConfig`` entry (config.yaml §providers)."""
    api_key_env = cfg.api_key_env or f"{provider_name.upper()}_API_KEY"
    api_key: str | None = os.environ.get(api_key_env)
    if cfg.api == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(bare_model, api_key=api_key, base_url=cfg.base_url)
    # api == "openai"
    from .openai_provider import OpenAIProvider

    return OpenAIProvider(bare_model, api_key=api_key, base_url=cfg.base_url)


def resolve_model(model_ref: str, global_cfg: GlobalConfig | None = None) -> LLMProvider:
    """Resolve *model_ref* to a provider.

    Parses ``model_ref`` as ``<model>@<provider>``.  If *provider* matches a
    key in ``global_cfg.providers``, the :class:`ProviderConfig` is used.
    Otherwise delegates to :func:`build_provider` (built-ins + prefix inference).
    """
    bare, provider = parse_model_string(model_ref)
    if global_cfg is not None:
        cfg: Any = global_cfg.providers.get(provider)
        if cfg is not None:
            return _build_from_provider_config(bare, provider, cfg)
    return build_provider(model_ref)
