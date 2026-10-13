"""Local LLM backend via Ollama (https://ollama.com). No extra Python deps."""

from __future__ import annotations

import json
import urllib.request
from typing import Iterator

from simcoach.config import LLMConfig


class OllamaLLM:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.name = f"ollama:{cfg.ollama_model}"

    def stream(self, system: str, messages: list[dict]) -> Iterator[str]:
        body = {
            "model": self.cfg.ollama_model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": True,
            # Keep the model loaded between questions so answers stay fast.
            "keep_alive": "30m",
            "options": {"num_predict": min(self.cfg.max_tokens, 300), "temperature": 0.3},
        }
        req = urllib.request.Request(
            self.cfg.ollama_url.rstrip("/") + "/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
            for line in resp:
                if not line.strip():
                    continue
                chunk = json.loads(line)
                text = chunk.get("message", {}).get("content", "")
                if text:
                    yield text
                if chunk.get("done"):
                    break

    def warm_up(self) -> None:
        """Load the model into memory before the driver asks anything."""
        list(self.stream("Reply with OK.", [{"role": "user", "content": "ping"}]))
