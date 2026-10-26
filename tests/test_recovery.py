import pytest

from conftest import answer, pick_option
from worker.browser import ElementInfo
from worker.recovery import Recovery, RecoveryBudget, RecoveryFailed, action_options, validate_action

MUG = "https://store.test/en-us/p/mug"


def test_recovery_selects_unknown_edition(store_page, stub_llm):
    store_page.goto(MUG)
    llm = stub_llm([pick_option("action", "Collector's")])
    budget = RecoveryBudget()
    Recovery(llm, budget).attempt(store_page, goal='select option "edition" = "Collector\'s"', failed_step="options")
    assert store_page.get_by_role("radio", name="Collector's").get_attribute("aria-checked") == "true"
    assert budget.calls == 1 and budget.actions == 1
    call = llm.calls[0]
    assert call["questions"]["action"]["type"] == "choice"
    assert "untrusted" in call["questions"]["action"]["instructions"].lower()
    assert "Collector" in str(call["state"])  # state is the parsed page, not a screenshot


def test_forbidden_elements_never_offered(store_page):
    store_page.goto(MUG)
    from worker.browser import snapshot
    opts = action_options(snapshot(store_page))
    descriptions = " ".join(opts.values()).lower()
    assert "checkout" not in descriptions
    assert {"scroll", "wait", "give_up"} <= set(opts)


def test_pick_outside_offered_options_refused(store_page, stub_llm):
    store_page.goto(MUG)
    from worker.browser import snapshot
    checkout_id = next(e.id for e in snapshot(store_page) if e.name == "Checkout")
    llm = stub_llm([{"action": answer(checkout_id)}] * 3)
    budget = RecoveryBudget()
    with pytest.raises(RecoveryFailed):
        Recovery(llm, budget).attempt(store_page, goal="add the mug", failed_step="add")
    assert budget.calls == 3 and budget.actions == 0
    assert store_page.url == MUG
    assert any("refused" in t for t in budget.trace)


def test_low_confidence_gives_up(store_page, stub_llm):
    store_page.goto(MUG)
    llm = stub_llm([pick_option("action", "Standard", confidence=0.3)])
    with pytest.raises(RecoveryFailed, match="confidence"):
        Recovery(llm, RecoveryBudget()).attempt(store_page, goal="x", failed_step="add")


def test_validate_action():
    els = {"e1": ElementInfo(id="e1", tag="button", role="button", name="M", text="M")}
    assert "unknown element" in validate_action({"action": "click", "element_id": "e9"}, els)
    assert validate_action({"action": "click", "element_id": "e1"}, els) is None
    assert "select" in validate_action({"action": "select_option", "element_id": "e1", "value": "x"}, els)
    assert "unknown action" in validate_action({"action": "type", "element_id": "e1"}, els)


def test_give_up_raises(store_page, stub_llm):
    store_page.goto(MUG)
    llm = stub_llm([{"action": answer("give_up")}])
    with pytest.raises(RecoveryFailed, match="gave up"):
        Recovery(llm, RecoveryBudget()).attempt(store_page, goal="x", failed_step="add")


def test_no_llm(store_page):
    with pytest.raises(RecoveryFailed, match="LUNAROUTE_API_KEY not set"):
        Recovery(None, RecoveryBudget()).attempt(store_page, goal="x", failed_step="add")


def test_budget_shared_across_attempts(store_page, stub_llm):
    store_page.goto(MUG)
    llm = stub_llm([pick_option("action", "Standard")] * 5)
    budget = RecoveryBudget(max_calls=3, max_actions=2)
    rec = Recovery(llm, budget)
    rec.attempt(store_page, goal="a", failed_step="s")
    rec.attempt(store_page, goal="b", failed_step="s")
    with pytest.raises(RecoveryFailed, match="budget"):
        rec.attempt(store_page, goal="c", failed_step="s")


def test_select_option_offered_per_value(store_page, stub_llm):
    store_page.goto("https://store.test/en-us/p/hoodie")
    llm = stub_llm([pick_option("action", "select 'Blue'")])
    Recovery(llm, RecoveryBudget()).attempt(store_page, goal="select color Blue", failed_step="options")
    assert store_page.locator("#color").input_value() == "Blue"
