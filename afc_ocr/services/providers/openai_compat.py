"""
afc_ocr/services/providers/openai_compat.py - one adapter for every service that speaks the
OpenAI chat-completions shape: OpenAI itself, OpenRouter, Groq, Mistral, xAI, and whatever the
organizer pastes as a custom base URL.

The request: POST {base_url}/chat/completions with one user message holding the prompt text and
the screenshot as a data URL; `response_format: json_object` where the service honours it (the
ones that do not simply ignore the field). The reply's first choice text goes through
base.parse_placements; a malformed reply is asked once more with the JSON-only nudge.

Errors: the service's own message (`error.message` in the body) is what the organizer reads, with
the status code, and never the Authorization header or the URL. A timeout says so in words.
"""
import base64
import time

import requests
from django.conf import settings

from .base import JSON_ONLY_NUDGE, ProviderError, parse_placements

TIMEOUT_DEFAULT = 30


def _message_from(resp) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])[:280]
    if isinstance(err, str):
        return err[:280]
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])[:280]
    return f"HTTP {resp.status_code} from the provider"


def read(image_bytes: bytes, mime_type: str, prompt: str, *, api_key: str, model: str, base_url: str) -> dict:
    if not base_url:
        raise ProviderError("This provider needs a base URL.")
    if not model:
        raise ProviderError("Pick a model id first.")
    url = base_url.rstrip("/") + "/chat/completions"
    data_url = f"data:{mime_type};base64," + base64.b64encode(image_bytes).decode("ascii")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = getattr(settings, "OCR_PROVIDER_HTTP_TIMEOUT", TIMEOUT_DEFAULT)

    def ask(text_prompt: str) -> str:
        payload = {
            "model": model,
            "temperature": 0.1,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": text_prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
            "response_format": {"type": "json_object"},
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.Timeout:
            raise ProviderError(f"The provider did not answer within {timeout} seconds. Try again.")
        except requests.ConnectionError:
            raise ProviderError("Could not reach the provider. Check the base URL and your connection.")
        if resp.status_code >= 400:
            raise ProviderError(_message_from(resp), resp.status_code)
        try:
            body = resp.json()
            return body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("The provider answered in a shape this reader does not understand.")

    started = time.monotonic()
    text = ask(prompt)
    try:
        return parse_placements(text)
    except ValueError:
        pass
    # One more time, sterner. Some models chat before the JSON despite response_format.
    text = ask(prompt + JSON_ONLY_NUDGE)
    try:
        return parse_placements(text)
    except ValueError:
        raise ProviderError(
            "The model did not return the standings as JSON, twice. Try the recommended model for this provider."
        ) from None
    finally:
        _ = time.monotonic() - started
