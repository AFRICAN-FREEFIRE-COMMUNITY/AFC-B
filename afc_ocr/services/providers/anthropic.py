"""
afc_ocr/services/providers/anthropic.py - Anthropic's Messages API (Claude), which has its own
request shape: `x-api-key` header, `anthropic-version` header, an image block with base64 data,
the reply under `content[0].text`. Same contract as the others (see base.py): screenshot bytes in,
{"placements": [...]} out, the provider's own message on refusal, one JSON-only retry.
"""
import base64

import requests
from django.conf import settings

from .base import JSON_ONLY_NUDGE, ProviderError, parse_placements

URL = "https://api.anthropic.com/v1/messages"
VERSION = "2023-06-01"
TIMEOUT_DEFAULT = 30


def _message_from(resp) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])[:280]
    return f"HTTP {resp.status_code} from Anthropic"


def read(image_bytes: bytes, mime_type: str, prompt: str, *, api_key: str, model: str, base_url: str = "") -> dict:
    if not model:
        raise ProviderError("Pick a model id first.")
    headers = {"x-api-key": api_key, "anthropic-version": VERSION, "Content-Type": "application/json"}
    timeout = getattr(settings, "OCR_PROVIDER_HTTP_TIMEOUT", TIMEOUT_DEFAULT)
    b64 = base64.b64encode(image_bytes).decode("ascii")

    def ask(text_prompt: str) -> str:
        payload = {
            "model": model,
            "max_tokens": 2048,
            "temperature": 0.1,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                    {"type": "text", "text": text_prompt},
                ],
            }],
        }
        try:
            resp = requests.post(base_url or URL, json=payload, headers=headers, timeout=timeout)
        except requests.Timeout:
            raise ProviderError(f"Anthropic did not answer within {timeout} seconds. Try again.")
        except requests.ConnectionError:
            raise ProviderError("Could not reach Anthropic. Check your connection.")
        if resp.status_code >= 400:
            raise ProviderError(_message_from(resp), resp.status_code)
        try:
            body = resp.json()
            blocks = [b.get("text", "") for b in body.get("content", []) if b.get("type") == "text"]
            return "\n".join(blocks)
        except (ValueError, AttributeError, TypeError):
            raise ProviderError("Anthropic answered in a shape this reader does not understand.")

    text = ask(prompt)
    try:
        return parse_placements(text)
    except ValueError:
        pass
    text = ask(prompt + JSON_ONLY_NUDGE)
    try:
        return parse_placements(text)
    except ValueError:
        raise ProviderError(
            "The model did not return the standings as JSON, twice. Try the recommended model."
        ) from None
