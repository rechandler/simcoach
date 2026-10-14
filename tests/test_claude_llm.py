import types

import pytest

from simcoach.config import LLMConfig
from simcoach.llm import claude as claude_mod


class FakeAnthropic:
    instances = []

    def __init__(self, **kw):
        self.kw = kw
        FakeAnthropic.instances.append(self)


def make(monkeypatch):
    fake = types.SimpleNamespace(Anthropic=FakeAnthropic, AuthenticationError=type("AE", (Exception,), {}))
    llm = claude_mod.ClaudeLLM.__new__(claude_mod.ClaudeLLM)
    llm._anthropic, llm.cfg, llm.client, llm._key = fake, LLMConfig(), None, None
    return llm


def test_key_saved_after_startup_is_picked_up(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm = make(monkeypatch)
    first = llm._get_client()
    assert "api_key" not in first.kw
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-new")
    second = llm._get_client()
    assert second is not first and second.kw["api_key"] == "sk-ant-new"
    assert llm._get_client() is second  # cached while the key is unchanged


def test_missing_credentials_become_a_clear_error(monkeypatch):
    llm = make(monkeypatch)

    def boom(system, messages):
        raise TypeError('"Could not resolve authentication method. Expected one of api_key, auth_token"')
        yield  # pragma: no cover

    llm._stream = boom
    with pytest.raises(claude_mod.MissingCredentials, match="simcoach setup"):
        list(llm.stream("s", []))


def test_out_of_credit_is_explained(monkeypatch):
    llm = make(monkeypatch)

    class BadRequest(Exception):
        pass

    llm._anthropic.BadRequestError = BadRequest
    for name in ("RateLimitError", "APIConnectionError", "InternalServerError"):
        setattr(llm._anthropic, name, type(name, (Exception,), {}))

    def boom(system, messages):
        raise BadRequest("Error code: 400 - Your credit balance is too low to access the Anthropic API.")
        yield  # pragma: no cover

    llm._stream = boom
    with pytest.raises(claude_mod.CoachUnavailable) as exc:
        list(llm.stream("s", []))
    assert "out of credit" in exc.value.spoken and "billing" in str(exc.value)
