"""Claude (cloud) backend using the official Anthropic SDK.

Credentials come from the ANTHROPIC_API_KEY environment variable (on
Windows also read from the saved user environment, so a key saved with
`setx` works without restarting), or an `ant auth login` profile. Never put
keys in the config file.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Iterator

from simcoach.config import LLMConfig

log = logging.getLogger(__name__)


class CoachUnavailable(RuntimeError):
    """Claude can't answer right now; ``spoken`` is what to tell the driver."""

    def __init__(self, message: str, spoken: str):
        super().__init__(message)
        self.spoken = spoken


class MissingCredentials(CoachUnavailable):
    """No (valid) Anthropic API key."""

    def __init__(self, message: str):
        super().__init__(message, "I don't have a working Claude API key, so here's what the telemetry says.")


HOW_TO_FIX = ("No Claude API key found. Run `simcoach setup`, or run "
              "setx ANTHROPIC_API_KEY \"sk-ant-...\" (get a key at https://console.anthropic.com/settings/keys).")


def saved_user_env(name: str) -> str | None:
    """Read a user environment variable straight from the Windows registry.

    `setx` (used by `simcoach setup`) only affects programs started afterwards,
    so a window that was already open won't see a freshly saved key. Reading
    the registry picks it up without a restart.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            value, _ = winreg.QueryValueEx(k, name)
            return str(value) or None
    except OSError:
        return None


def find_api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or saved_user_env("ANTHROPIC_API_KEY")


class ClaudeLLM:
    def __init__(self, cfg: LLMConfig):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("pip install 'simcoach[claude]' to use Claude") from e
        self._anthropic = anthropic
        self.cfg = cfg
        self.name = f"claude:{cfg.model}"
        self.client = None
        self._key: str | None = None
        if not (find_api_key() or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            log.warning(HOW_TO_FIX)

    def _get_client(self):
        """Create the client lazily so a key saved while simcoach is running works."""
        key = find_api_key()
        if self.client is None or (key and key != self._key):
            kwargs = {"timeout": self.cfg.timeout_s, "max_retries": 1}
            if key:
                kwargs["api_key"] = key
            # Otherwise the SDK falls back to ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile.
            self.client = self._anthropic.Anthropic(**kwargs)
            self._key = key
        return self.client

    def stream(self, system: str, messages: list[dict]) -> Iterator[str]:
        try:
            yield from self._stream(system, messages)
        except self._anthropic.AuthenticationError as e:
            raise MissingCredentials("Your Claude API key was rejected. " + HOW_TO_FIX) from e
        except TypeError as e:
            # The SDK raises TypeError when it can't find any credentials.
            if "authentication" in str(e).lower() or "api_key" in str(e):
                raise MissingCredentials(HOW_TO_FIX) from e
            raise
        except self._anthropic.BadRequestError as e:
            if "credit balance" in str(e).lower():
                raise CoachUnavailable(
                    "Your Anthropic account is out of credit. Add credit at "
                    "https://console.anthropic.com/settings/billing",
                    "My Claude account is out of credit, so here's what the telemetry says.") from e
            raise
        except self._anthropic.RateLimitError as e:
            raise CoachUnavailable(f"Claude rate limit: {e}", "Claude is busy right now, here's the telemetry.") from e
        except self._anthropic.APIConnectionError as e:
            raise CoachUnavailable(f"Can't reach the Claude API: {e}",
                                   "I can't reach Claude right now, here's the telemetry.") from e
        except self._anthropic.InternalServerError as e:
            raise CoachUnavailable(f"Claude API error: {e}", "Claude is having trouble, here's the telemetry.") from e

    def _stream(self, system: str, messages: list[dict]) -> Iterator[str]:
        # The system prompt is identical on every question, so cache it.
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        # Server-side fallbacks: if a request is ever declined by a safety
        # classifier, the API retries it on a recommended fallback model.
        with self._get_client().beta.messages.stream(
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
