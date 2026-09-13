"""
afc_ocr/services/providers/registry.py - the providers an organization may connect, in the
order the owner chose (2026-09-12): Gemini, OpenAI, Anthropic, OpenRouter, Groq, Mistral, xAI,
then "any other OpenAI-compatible service".

Each entry is what the connect page needs to explain itself: the recommended vision-capable
model, where the key comes from, what a key looks like (the hint), whether a free tier exists,
and a per-image cost line with the date it was checked. The frontend renders this list
(views_ai_key.providers) so the page and the backend can never disagree about what is offered.

`adapter` names the module in this package that talks to the provider. Three adapters cover the
list because nearly every hosted model speaks the OpenAI request shape; "custom" is the escape
hatch for the rest (the organizer pastes the base URL and a model id).

Prices are APPROXIMATE, quoted from each provider's public pricing page on the date shown, and
the page says so. A screenshot is roughly 1,000 to 2,000 input tokens plus a few hundred output
tokens; the numbers below are that arithmetic on the recommended model, rounded up.
"""

CHECKED_ON = "2026-09-12"

PROVIDERS = [
    {
        "id": "gemini",
        "name": "Google Gemini",
        "adapter": "gemini",
        "recommended_model": "gemini-2.5-flash",
        "keys_url": "https://aistudio.google.com/app/apikey",
        "key_hint": "starts with AIza",
        "free_tier": "Yes, with daily limits on an AI Studio key.",
        "cost_per_image_usd": 0.001,
        "steps": [
            "Open aistudio.google.com and sign in with any Google account.",
            "Press Get API key in the left menu.",
            "Press Create API key. Choose an existing Google Cloud project or let it create one.",
            "Copy the key. It starts with AIza. Treat it like a password.",
            "Optional but wise: in Google Cloud, restrict this key to the Generative Language API only.",
        ],
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "adapter": "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "recommended_model": "gpt-4.1-mini",
        "keys_url": "https://platform.openai.com/api-keys",
        "key_hint": "starts with sk-",
        "free_tier": "No. Pay as you go; add a few dollars of credit under Billing first.",
        "cost_per_image_usd": 0.002,
        "steps": [
            "Open platform.openai.com and sign in.",
            "Under Settings, open Billing and add credit (a few dollars lasts a whole season).",
            "Open API keys in the left menu and press Create new secret key.",
            "Name it AFC OCR, choose Restricted and allow only Model capabilities.",
            "Copy the key. It starts with sk-. It is shown once.",
        ],
    },
    {
        "id": "anthropic",
        "name": "Anthropic Claude",
        "adapter": "anthropic",
        "recommended_model": "claude-haiku-4-5",
        "keys_url": "https://console.anthropic.com/settings/keys",
        "key_hint": "starts with sk-ant-",
        "free_tier": "No. Pay as you go; add credit under Plans and billing first.",
        "cost_per_image_usd": 0.003,
        "steps": [
            "Open console.anthropic.com and sign in.",
            "Under Plans and billing, add credit.",
            "Open Settings, then API keys, and press Create key.",
            "Name it AFC OCR and choose the workspace it belongs to.",
            "Copy the key. It starts with sk-ant-. It is shown once.",
        ],
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "adapter": "openai_compat",
        "base_url": "https://openrouter.ai/api/v1",
        "recommended_model": "google/gemini-2.5-flash",
        "keys_url": "https://openrouter.ai/settings/keys",
        "key_hint": "starts with sk-or-",
        "free_tier": "Some models are free with limits; the recommended one is pay as you go.",
        "cost_per_image_usd": 0.001,
        "steps": [
            "Open openrouter.ai and sign in (Google or GitHub works).",
            "Open Credits and add a few dollars.",
            "Open Keys and press Create key.",
            "Name it AFC OCR. A credit limit on the key is a good idea.",
            "Copy the key. It starts with sk-or-.",
        ],
    },
    {
        "id": "groq",
        "name": "Groq",
        "adapter": "openai_compat",
        "base_url": "https://api.groq.com/openai/v1",
        "recommended_model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "keys_url": "https://console.groq.com/keys",
        "key_hint": "starts with gsk_",
        "free_tier": "Yes, rate limited.",
        "cost_per_image_usd": 0.001,
        "steps": [
            "Open console.groq.com and sign in.",
            "Open API Keys in the left menu and press Create API Key.",
            "Name it AFC OCR.",
            "Copy the key. It starts with gsk_. It is shown once.",
        ],
    },
    {
        "id": "mistral",
        "name": "Mistral",
        "adapter": "openai_compat",
        "base_url": "https://api.mistral.ai/v1",
        "recommended_model": "pixtral-12b-2409",
        "keys_url": "https://console.mistral.ai/api-keys",
        "key_hint": "a 32-character string",
        "free_tier": "Yes, a limited free plan.",
        "cost_per_image_usd": 0.001,
        "steps": [
            "Open console.mistral.ai and sign in.",
            "Choose a plan under Billing (the free experiment plan works for OCR).",
            "Open API Keys and press Create new key.",
            "Name it AFC OCR and copy it. It is shown once.",
        ],
    },
    {
        "id": "xai",
        "name": "xAI Grok",
        "adapter": "openai_compat",
        "base_url": "https://api.x.ai/v1",
        "recommended_model": "grok-2-vision-1212",
        "keys_url": "https://console.x.ai",
        "key_hint": "starts with xai-",
        "free_tier": "No. Pay as you go.",
        "cost_per_image_usd": 0.004,
        "steps": [
            "Open console.x.ai and sign in with your X account.",
            "Add credit under Billing.",
            "Open API Keys and press Create API key.",
            "Name it AFC OCR and copy it. It starts with xai-.",
        ],
    },
    {
        "id": "custom",
        "name": "Any other OpenAI-compatible service",
        "adapter": "openai_compat",
        "recommended_model": "",
        "keys_url": "",
        "key_hint": "whatever your provider gave you",
        "free_tier": "Depends on the provider.",
        "cost_per_image_usd": 0.002,
        "steps": [
            "Find your provider's OpenAI-compatible base URL (it usually ends in /v1).",
            "Find the id of a model that can read images.",
            "Create a key in your provider's console.",
            "Paste the base URL, the model id and the key below, then Test.",
        ],
    },
]

BY_ID = {p["id"]: p for p in PROVIDERS}


def get(provider_id: str) -> dict:
    p = BY_ID.get(provider_id)
    if not p:
        raise KeyError(provider_id)
    return p


def public() -> list:
    """What the connect page renders: everything, in order, with the date the prices were checked."""
    return [{**p, "checked_on": CHECKED_ON} for p in PROVIDERS]
