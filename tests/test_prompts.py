import json

from worker.browser import ElementInfo
from worker.prompts import MAX_STATE_CHARS, batch_questions, page_state


def el(i, tag="button", name="x" * 50):
    return ElementInfo(id=f"e{i}", tag=tag, role=None, name=name, text="")


def test_page_state_skips_options_and_caps():
    els = [el(0, "select", "Country")] + [el(i, "option", f"Country {i}") for i in range(1, 50)] + [el(i) for i in range(50, 2000)]
    s = page_state("https://x/", els)
    assert "Country 3" not in s
    assert len(s) <= MAX_STATE_CHARS + 200
    assert s.endswith("(truncated)")


def test_batch_questions_respects_budget():
    q = {"type": "choice", "instructions": "i", "criteria": {f"e{i}": "d" * 100 for i in range(100)}}
    questions = {f"q{i}": q for i in range(6)}
    batches = batch_questions("s" * 1000, questions, budget_chars=25_000)
    assert sum(len(b) for b in batches) == 6
    assert all(len(json.dumps(b)) + 1000 <= 25_000 for b in batches)
    assert len(batches) > 1
