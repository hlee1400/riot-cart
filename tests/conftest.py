from __future__ import annotations

from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import pytest

from worker.browser import install_guard, launch, new_context, set_policy
from worker.llm import SystemOneReply
from worker.recipe import load_recipe


class StubLLM:
    """System One stand-in. Each reply is an answers dict or a callable(state, questions) -> answers dict."""

    def __init__(self, replies: list[dict | Callable[[object, dict], dict]]):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def ask(self, *, purpose, state, questions):
        self.calls.append({"purpose": purpose, "state": state, "questions": questions})
        reply = self.replies.pop(0)
        answers = reply(state, questions) if callable(reply) else reply
        return SystemOneReply(answers=answers, tokens_in=1, latency_ms=0)


def answer(pick, confidence=0.95):
    return {"type": "choice", "choice": pick, "confidence": confidence, "probabilities": {}}


def pick_option(qid: str, needle: str, confidence: float = 0.95):
    """Reply that picks, for question `qid`, the first option whose description contains `needle`."""
    def reply(state, questions):
        crit = questions[qid]["criteria"]
        key = next((k for k, d in crit.items() if needle in d), "none")
        return {qid: answer(key, confidence)}
    return reply


@pytest.fixture
def stub_llm():
    return StubLLM


FIXTURES = Path(__file__).parent / "fixtures"
STORE = FIXTURES / "store"
SLUG_FILES = {"pin2": "pin", "hoodie-kids": "hoodie"}


def serve_store(route):
    path = urlsplit(route.request.url).path
    if path in ("/en-us/", "/en-us/search"):
        name = "search.html"
    elif path == "/en-us/cart":
        name = "cart.html"
    elif path == "/en-us/store.js":
        name = "store.js"
    elif path.startswith("/en-us/p/"):
        slug = path.rsplit("/", 1)[-1]
        name = "lab.html" if slug.startswith("lab-") else f"{SLUG_FILES.get(slug, slug)}.html"
    else:
        name = None
    if name is None or not (STORE / name).exists():
        return route.fulfill(status=404, body="not found")
    ctype = "application/javascript" if name.endswith(".js") else "text/html"
    route.fulfill(status=200, body=(STORE / name).read_text(encoding="utf-8"), content_type=ctype)


@pytest.fixture(scope="session")
def store_recipe():
    return load_recipe(FIXTURES / "store_recipe.yaml")


@pytest.fixture(scope="session")
def browser():
    pw, b = launch(headed=False)
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def store_page(browser, store_recipe):
    """A page whose https://store.test/** traffic is served from tests/fixtures/store, with the guard on."""
    context = new_context(browser)
    context.route("https://store.test/**", serve_store)
    blocked = install_guard(context, store_recipe)
    set_policy(store_recipe)
    page = context.new_page()
    page.blocked = blocked
    yield page
    context.close()
