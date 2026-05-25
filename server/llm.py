"""
server/llm.py — Thin LLM wrapper for Friday Budgeting Pro.

Design
------
Calls are routed **primarily** through OpenClaw's local completions API
(an OpenAI-compatible HTTP endpoint running on localhost).  If that endpoint
is unreachable (connection refused, timeout, HTTP error), the wrapper falls
back to calling the Anthropic or OpenAI SDK directly — the same path that
existed before this change.

Primary path (OpenClaw local API)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
POST to OPENCLAW_API_URL (default "http://127.0.0.1:7531/v1/completions")
with body::

    {
        "messages": [...],
        "temperature": 0.0,
        "model": "<OPENCLAW_LLM_MODEL or claude-sonnet-4-6>"
    }

Uses stdlib ``urllib.request`` only (no extra dependencies, 3-second timeout).

Response parsing (tried in order):
    1. OpenAI-style:  {"choices": [{"message": {"content": "..."}}]}
    2. OpenAI delta:  {"choices": [{"delta":   {"content": "..."}}]}
    3. Flat text:     {"text": "..."}
    4. Flat content:  {"content": "..."}

If none of those match, or the response body is not valid JSON, the wrapper
logs a warning and falls back to the SDK path.

Fallback path (SDK)
~~~~~~~~~~~~~~~~~~~
When OpenClaw is unreachable *or* the response is unparse-able, the wrapper
tries Anthropic then OpenAI (same auto-detection logic as before).  A warning
is logged so operators know the fallback was used.

Patchability
~~~~~~~~~~~~
The public ``chat()`` function is fully mockable in tests::

    with unittest.mock.patch("server.llm.chat", return_value="..."):
        ...

Env vars
~~~~~~~~
``OPENCLAW_API_URL``   — full URL of the local completions endpoint.
``OPENCLAW_LLM_MODEL`` — model name forwarded to OpenClaw (default:
                          "claude-sonnet-4-6").
``OPENCLAW_LLM_PROVIDER`` — "anthropic" or "openai" (SDK fallback provider).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / env helpers
# ---------------------------------------------------------------------------

_DEFAULT_OPENCLAW_URL = "http://127.0.0.1:7531/v1/completions"
_OPENCLAW_TIMEOUT = 60  # seconds — agent turns can take time


def _openclaw_url() -> str:
    return os.environ.get("OPENCLAW_API_URL", _DEFAULT_OPENCLAW_URL)


def _openclaw_model() -> str:
    return os.environ.get("OPENCLAW_LLM_MODEL", "claude-sonnet-4-6")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chat(messages: list[dict], temperature: float = 0.0) -> str:
    """Send *messages* to the LLM and return the response text.

    Tries the OpenClaw local API first; falls back to the direct SDK path
    when OpenClaw is unreachable or returns an unparseable response.

    Args:
        messages: A list of ``{"role": ..., "content": ...}`` dicts.
        temperature: Sampling temperature (0.0 = deterministic).

    Returns:
        The assistant's reply as a plain string.

    Raises:
        RuntimeError: If OpenClaw is unreachable **and** no SDK provider is
            available.
        Exception: Any unhandled exception from the SDK is propagated.
    """
    # --- Primary path: OpenClaw local API ---
    try:
        return _chat_openclaw(messages, temperature)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        ConnectionError,
        OSError,
    ) as exc:
        logger.warning("OpenClaw local API unreachable (%s); falling back to direct SDK.", exc)
    except _UnparseableResponseError as exc:
        logger.warning(
            "OpenClaw response could not be parsed (%s); falling back to direct SDK.",
            exc,
        )

    # --- Fallback path: direct SDK ---
    return _chat_sdk_fallback(messages, temperature)


# ---------------------------------------------------------------------------
# OpenClaw primary path
# ---------------------------------------------------------------------------


class _UnparseableResponseError(Exception):
    """Raised when the OpenClaw response body cannot be parsed."""


def _chat_openclaw(messages: list[dict], temperature: float) -> str:
    """POST to OpenClaw local completions endpoint and parse the reply."""
    url = _openclaw_url()
    payload = json.dumps(
        {
            "messages": messages,
            "temperature": temperature,
            "model": _openclaw_model(),
        }
    ).encode()

    headers = {"Content-Type": "application/json"}
    _token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
    if _token:
        headers["Authorization"] = f"Bearer {_token}"

    req = urllib.request.Request(
        url,
        data=payload,
        headers=headers,
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=_OPENCLAW_TIMEOUT) as resp:
        raw = resp.read()

    # Parse JSON
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _UnparseableResponseError(f"Non-JSON response: {exc}") from exc

    # Try known response shapes in order of likelihood
    text = _extract_content(data)
    if text is None:
        raise _UnparseableResponseError(f"Unrecognised response shape: {list(data.keys())!r}")
    return text


def _extract_content(data: dict) -> str | None:
    """Try multiple plausible response shapes and return the content string.

    Shapes tried (in order):
        1. OpenAI-style:  {"choices": [{"message": {"content": "..."}}]}
        2. OpenAI delta:  {"choices": [{"delta":   {"content": "..."}}]}
        3. Flat text:     {"text": "..."}
        4. Flat content:  {"content": "..."}

    Returns ``None`` if none of the shapes matched.
    """
    # Shape 1 & 2: choices list
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            # message.content
            msg = first.get("message")
            if isinstance(msg, dict) and "content" in msg:
                return str(msg["content"])
            # delta.content (streaming residue)
            delta = first.get("delta")
            if isinstance(delta, dict) and "content" in delta:
                return str(delta["content"])
            # bare "text" inside choice
            if "text" in first:
                return str(first["text"])

    # Shape 3: flat {"text": "..."}
    if "text" in data:
        return str(data["text"])

    # Shape 4: flat {"content": "..."}
    if "content" in data:
        return str(data["content"])

    return None


# ---------------------------------------------------------------------------
# SDK fallback path (lazy imports — no top-level SDK dependency)
# ---------------------------------------------------------------------------


def _chat_sdk_fallback(messages: list[dict], temperature: float) -> str:
    """Call the Anthropic or OpenAI SDK directly."""
    provider = os.environ.get("OPENCLAW_LLM_PROVIDER", "").lower()
    model = os.environ.get("OPENCLAW_LLM_MODEL", "")

    # Auto-detect provider if not set
    if not provider:
        try:
            import anthropic as _  # noqa: F401

            provider = "anthropic"
        except ImportError:
            pass

    if not provider:
        try:
            import openai as _  # noqa: F401

            provider = "openai"
        except ImportError:
            pass

    if not provider:
        raise RuntimeError(
            "OpenClaw is unreachable and no LLM SDK is available. "
            "Set OPENCLAW_LLM_PROVIDER (e.g. 'anthropic') and OPENCLAW_LLM_MODEL."
        )

    if provider == "anthropic":
        return _chat_anthropic(messages, temperature, model)
    elif provider == "openai":
        return _chat_openai(messages, temperature, model)
    else:
        raise RuntimeError(
            f"Unknown OPENCLAW_LLM_PROVIDER={provider!r}. "
            "Supported values: 'anthropic', 'openai'."
        )


def _chat_anthropic(messages: list[dict], temperature: float, model: str) -> str:
    import anthropic  # type: ignore[import]

    if not model:
        model = "claude-3-5-haiku-20241022"

    system_parts = [m["content"] for m in messages if m["role"] == "system"]
    human_messages = [m for m in messages if m["role"] != "system"]

    client = anthropic.Anthropic()
    kwargs: dict = dict(
        model=model,
        max_tokens=1024,
        messages=human_messages,
        temperature=temperature,
    )
    if system_parts:
        kwargs["system"] = "\n\n".join(system_parts)

    response = client.messages.create(**kwargs)
    return response.content[0].text


def _chat_openai(messages: list[dict], temperature: float, model: str) -> str:
    import openai  # type: ignore[import]

    if not model:
        model = "gpt-4o-mini"

    client = openai.OpenAI()
    response = client.chat.completions.create(
        model=model,
        messages=messages,  # type: ignore[arg-type]
        temperature=temperature,
    )
    return response.choices[0].message.content or ""
