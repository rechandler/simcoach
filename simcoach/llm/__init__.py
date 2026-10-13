"""LLM backends. Every backend streams text so speech can start early."""

from __future__ import annotations

from typing import Iterator, Protocol

from simcoach.config import LLMConfig


class LLM(Protocol):
    name: str

    def stream(self, system: str, messages: list[dict]) -> Iterator[str]:
        """Yield response text chunks. ``messages`` are {"role", "content"} dicts."""
        ...


class NoLLM:
    name = "none"

    def stream(self, system: str, messages: list[dict]) -> Iterator[str]:
        return iter(())


def make_llm(cfg: LLMConfig) -> LLM:
    provider = cfg.provider.lower()
    if provider in ("anthropic", "claude"):
        from simcoach.llm.claude import ClaudeLLM

        return ClaudeLLM(cfg)
    if provider == "ollama":
        from simcoach.llm.ollama import OllamaLLM

        return OllamaLLM(cfg)
    if provider == "none":
        return NoLLM()
    raise ValueError(f"unknown llm.provider {cfg.provider!r} (use anthropic, ollama or none)")
