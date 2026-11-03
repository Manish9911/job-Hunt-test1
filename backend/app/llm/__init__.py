"""Swappable LLM provider interface.

Every caller goes through ``get_llm()`` and ``llm.complete(system, user)``.
Phase 3 ships the OpenAI provider (default ``gpt-4o-mini``, per user config);
``none`` (or a missing API key) returns a provider whose ``is_available()`` is
False so the rest of the app can fall back to deterministic behaviour.
"""
from __future__ import annotations

import httpx

from .base import LLM, LLMUnavailable


class OpenAIProvider(LLM):
    """OpenAI-compatible chat completions over httpx (no SDK dependency).

    Works with api.openai.com or any OpenAI-compatible server via
    ``openai_base_url``.
    """

    name = "openai"

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    def is_available(self) -> bool:
        return bool(self.api_key)

    def complete(self, system: str, user: str, temperature: float = 0.2,
                 timeout: float = 90.0) -> str:
        if not self.is_available():
            raise LLMUnavailable("OPENAI_API_KEY is not set — put it in .env to enable LLM tailoring")
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


def get_llm(settings) -> LLM:
    """Build the configured provider. Never raises for a missing key —
    callers check ``is_available()`` and fall back."""
    if settings.llm_provider == "openai":
        return OpenAIProvider(settings.openai_api_key, settings.llm_model,
                              settings.openai_base_url)
    # anthropic/ollama: same interface, not implemented yet (Phase 3 scope
    # is openai + deterministic fallback per the confirmed preference).
    return OpenAIProvider("", settings.llm_model)
