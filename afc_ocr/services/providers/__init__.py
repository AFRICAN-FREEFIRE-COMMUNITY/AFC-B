"""
afc_ocr/services/providers - the AI providers an organization can bring its own key from.

    registry.py       the list the connect page shows, in the owner's order, with models + guides
    base.py           the contract, ProviderError, the shared JSON parsing
    gemini.py         Google Gemini (wraps services.gemini)
    openai_compat.py  OpenAI, OpenRouter, Groq, Mistral, xAI, custom base URLs
    anthropic.py      Anthropic Claude

read_with(credentials, image_bytes, mime_type, prompt, **gemini_extras) picks the adapter from the
registry entry's `adapter` field, so services.extract never imports a provider module by name.
"""
from importlib import import_module

from . import registry
from .base import Credentials, ProviderError, parse_placements  # noqa: F401  (re-exported)


def read_with(creds: Credentials, image_bytes: bytes, mime_type: str, prompt: str, **extras) -> dict:
    entry = registry.get(creds.provider)
    module = import_module(f".{entry['adapter']}", __name__)
    kwargs = dict(api_key=creds.api_key, model=creds.model, base_url=creds.base_url or entry.get("base_url", ""))
    if entry["adapter"] == "gemini":
        kwargs.update({k: v for k, v in extras.items() if k in ("aliases", "team_notes", "prompt_kind")})
    return module.read(image_bytes, mime_type, prompt, **kwargs)
