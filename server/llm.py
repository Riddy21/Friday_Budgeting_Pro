"""
server/llm.py — Thin LLM wrapper for Friday Budgeting Pro.

Provides a single function, chat(), that sends messages to the configured
LLM and returns the response text.  The implementation is intentionally
kept small and fully mockable in tests via:

    unittest.mock.patch("server.llm.chat", ...)

Configuration is environment-driven:
    OPENCLAW_LLM_PROVIDER  — "anthropic" (default if anthropic SDK present)
    OPENCLAW_LLM_MODEL     — e.g. "claude-3-5-haiku-20241022"
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chat(messages: list[dict], temperature: float = 0.0) -> str:
    """Send *messages* to the configured LLM and return the response text.

    Args:
        messages: A list of ``{"role": ..., "content": ...}`` dicts.
            The first message whose role is "system" is treated as the
            system prompt by providers that handle it separately.
        temperature: Sampling temperature (0.0 = deterministic).

    Returns:
        The assistant's reply as a plain string.

    Raises:
        RuntimeError: If OPENCLAW_LLM_PROVIDER / OPENCLAW_LLM_MODEL are
            absent and no provider can be auto-detected.
        Exception: Any exception raised by the underlying SDK is propagated.

    Note:
        This function is designed to be patched in tests::

            with unittest.mock.patch("server.llm.chat") as mock_chat:
                mock_chat.return_value = '{"line_item_id": "..."}'
                ...
    """
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
            "No LLM provider configured. Set OPENCLAW_LLM_PROVIDER "
            "(e.g. 'anthropic') and OPENCLAW_LLM_MODEL environment variables."
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


# ---------------------------------------------------------------------------
# Provider implementations (lazy imports — no top-level SDK dependency)
# ---------------------------------------------------------------------------


def _chat_anthropic(messages: list[dict], temperature: float, model: str) -> str:
    import anthropic  # type: ignore[import]

    if not model:
        model = "claude-3-5-haiku-20241022"

    # Anthropic API separates system from human/assistant turns
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
