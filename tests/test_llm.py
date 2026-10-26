import pytest

from worker import llm as llm_mod
from worker.llm import LLMError, SystemOneClient, llm_from_env

QUESTIONS = {"add": {"type": "choice", "instructions": "Which is add to cart?", "criteria": {"e1": "b", "none": "n"}}}


class FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise llm_mod.requests.HTTPError(f"HTTP {self.status_code}")


OK = {"answers": {"add": {"type": "choice", "choice": "e1", "confidence": 0.97, "probabilities": {"e1": 0.97}}},
      "model": "djev", "usage": {"input_tokens": 40, "output_tokens": 2}}


def _patch(monkeypatch, responses):
    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw))
        return responses.pop(0)

    monkeypatch.setattr(llm_mod.requests, "post", fake_post)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    return calls


def test_request_shape(monkeypatch):
    calls = _patch(monkeypatch, [FakeResp(200, OK)])
    reply = SystemOneClient(api_key="k").ask(purpose="t", state="<b>x</b>", questions=QUESTIONS)
    url, kw = calls[0]
    assert url == "https://gw.lunaroute.com/v1/systemone"
    assert kw["headers"]["Authorization"] == "Bearer k"
    assert kw["json"] == {"model": "djev", "state": "<b>x</b>", "questions": QUESTIONS}
    assert reply.answers["add"]["choice"] == "e1"
    assert reply.tokens_in == 40


def test_retries_5xx(monkeypatch):
    calls = _patch(monkeypatch, [FakeResp(503), FakeResp(502), FakeResp(200, OK)])
    assert SystemOneClient(api_key="k").ask(purpose="t", state="s", questions=QUESTIONS).answers
    assert len(calls) == 3


def test_400_raises_with_message_without_key(monkeypatch):
    _patch(monkeypatch, [FakeResp(400, {"error": {"code": "x", "message": "state too large secret-key-123"}})])
    with pytest.raises(LLMError) as e:
        SystemOneClient(api_key="secret-key-123").ask(purpose="t", state="s", questions=QUESTIONS)
    assert "state too large" in str(e.value) and "secret-key-123" not in str(e.value)


def test_missing_answer_raises(monkeypatch):
    _patch(monkeypatch, [FakeResp(200, {"answers": {}})])
    with pytest.raises(LLMError, match="add"):
        SystemOneClient(api_key="k").ask(purpose="t", state="s", questions=QUESTIONS)


def test_llm_from_env(monkeypatch):
    monkeypatch.delenv("LUNAROUTE_API_KEY", raising=False)
    assert llm_from_env() is None
    monkeypatch.setenv("LUNAROUTE_API_KEY", "k")
    monkeypatch.setenv("LUNAROUTE_DECISION_MODEL", "djev-preview")
    c = llm_from_env()
    assert c is not None and c.model == "djev-preview"
