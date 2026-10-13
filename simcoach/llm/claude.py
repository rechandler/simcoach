"""Claude (cloud) backend using the official Anthropic SDK.

Credentials come from the environment (ANTHROPIC_API_KEY, or an
`ant auth login` profile) - never put keys in the config file.
"""

from __future__ import annotations

import logging
from typing import Iterator

from simcoach.config import LLMConfig

log = logging.getLogger(__name__)


class ClaudeLLM:
    def __init__(self, cfg: LLMConfig):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("pip install 'simcoach[claude]' to use Claude") from e
        self._anthropic = anthropic
        self.cfg = cfg
        self.name = f"claude:{cfg.model}"
        self.client = anthropic.Anthropic(timeout=cfg.timeout_s, max_retries=1)

    def stream(self, system: str, messages: list[dict]) -> Iterator[str]:
        # The system prompt is identical on every question, so cache it.
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        # Server-side fallbacks: if a request is ever declined by a safety
        # classifier, the API retries it on a recommended fallback model.
        with self.client.beta.messages.stream(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            system=system_blocks,
            messages=messages,
            output_config={"effort": self.cfg.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            yield from stream.text_stream
            final = stream.get_final_message()
        if final.stop_reason == "refusal":
            log.warning("Claude declined the request")
            yield " Sorry, I can't help with that one."
        elif final.stop_reason == "max_tokens":
            log.warning("Claude answer was cut off at max_tokens")
