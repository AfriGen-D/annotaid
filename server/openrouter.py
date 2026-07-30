"""OpenRouter client — stdlib (urllib) port of scripts/pipeline.py.

The backend base64-encodes the PDF and sends it (brief §9: never through the
browser). Structured output is requested via response_format (only when the model
config says it is supported) and the PDF parse engine is selected via the
file-parser plugin.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_TIMEOUT = 300  # seconds per request


class OpenRouterError(RuntimeError):
    pass


def pdf_data_url(path: str) -> str:
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    return f"data:application/pdf;base64,{b64}"


def build_messages(prompt: str, pdf_path: str, filename: str) -> list:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "file",
                    "file": {
                        "filename": filename,
                        "file_data": pdf_data_url(pdf_path),
                    },
                },
            ],
        }
    ]


def build_payload(
    model: str,
    messages: list,
    response_schema: dict | None,
    parse_engine: str,
    send_response_format: bool,
) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        # Select OpenRouter's PDF parse engine (pdf-text | mistral-ocr | native).
        "plugins": [{"id": "file-parser", "pdf": {"engine": parse_engine}}],
    }
    if send_response_format and response_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "annotaid_extraction",
                "strict": True,
                "schema": response_schema,
            },
        }
    return payload


def call(payload: dict, api_key: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Send one request, return the model's text content. Raises OpenRouterError."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            pass
        raise OpenRouterError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise OpenRouterError(f"network error: {exc.reason}") from exc

    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise OpenRouterError(f"non-JSON response: {body[:300]}") from exc

    if isinstance(obj, dict) and obj.get("error"):
        err = obj["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        raise OpenRouterError(msg)
    try:
        return obj["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(f"unexpected response shape: {body[:300]}") from exc
