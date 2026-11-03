"""Provider interface shared by all LLM backends."""
from __future__ import annotations


class LLMUnavailable(Exception):
    """Raised when a provider is requested but not configured (no key, etc.)."""


class LLM:
    name = "base"

    def is_available(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def complete(self, system: str, user: str, temperature: float = 0.2,
                 timeout: float = 90.0) -> str:  # pragma: no cover - interface
        raise NotImplementedError
