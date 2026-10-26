"""LunaRoute System One client for the `djev` decision model.

djev never writes text: it answers typed questions about a `state` (here: parsed page HTML) —
`choice` (pick one of up to 255 caller-supplied options), `noul` (probability a statement is true)
or `score` — each with calibrated confidence. Code supplies the candidates, so the model can only
pick things code has already found and vetted.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Protocol

import requests

from worker.safety import redact

log = logging.getLogger("worker.llm")

DEFAULT_BASE_URL = "https://gw.lunaroute.com/v1"
DEFAULT_MODEL = "djev"


class LLMError(Exception):
    pass


@dataclass
class SystemOneReply:
    answers: dict
    tokens_in: int | None
    latency_ms: int


class LLM(Protocol):
    def ask(self, *, purpose: str, state: str | dict | list, questions: dict) -> SystemOneReply: ...


class SystemOneClient:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL,
                 timeout_s: int = 90):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def ask(self, *, purpose: str, state: str | dict | list, questions: dict) -> SystemOneReply:
        body = {"model": self.model, "state": state, "questions": questions}
        start = time.monotonic()
        try:
            res = self._post(body)
        except requests.RequestException as e:
            raise LLMError(redact(f"LunaRoute {purpose} failed: {e}", [self.api_key])) from None
        answers = res.get("answers") or {}
        missing = [q for q in questions if q not in answers]
        if missing:
            raise LLMError(f"LunaRoute {purpose}: no answer for {', '.join(missing)}")
        usage = res.get("usage") or {}
        reply = SystemOneReply(answers, usage.get("input_tokens"), int((time.monotonic() - start) * 1000))
        log.info("djev call purpose=%s questions=%d latency_ms=%s tokens_in=%s",
                 purpose, len(questions), reply.latency_ms, reply.tokens_in)
        return reply

    def _post(self, body: dict) -> dict:
        for attempt in range(3):
            r = requests.post(f"{self.base_url}/systemone", json=body, timeout=self.timeout_s,
                              headers={"Authorization": f"Bearer {self.api_key}"})
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                try:
                    detail = r.json().get("error", {}).get("message", "")
                except ValueError:
                    detail = r.text[:200]
                raise requests.HTTPError(f"HTTP {r.status_code}: {detail}")
            return r.json()
        raise requests.HTTPError("retries exhausted")


def llm_from_env() -> SystemOneClient | None:
    key = os.environ.get("LUNAROUTE_API_KEY")
    if not key:
        return None
    return SystemOneClient(key, os.environ.get("LUNAROUTE_BASE_URL", DEFAULT_BASE_URL),
                           os.environ.get("LUNAROUTE_DECISION_MODEL", DEFAULT_MODEL))


def choice(reply: SystemOneReply, qid: str) -> tuple[str | None, float]:
    """The picked option and its confidence for a `choice` question; 'none' becomes None."""
    a = reply.answers.get(qid) or {}
    pick = a.get("choice")
    return (None if pick in (None, "none") else pick), float(a.get("confidence") or 0.0)


def noul(reply: SystemOneReply, qid: str) -> float:
    return float((reply.answers.get(qid) or {}).get("noul") or 0.0)
