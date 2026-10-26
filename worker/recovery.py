"""Bounded LunaRoute recovery with djev: code lists the safe actions, the model picks one, code re-checks it."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from playwright.sync_api import Page

from worker import browser
from worker.browser import ElementInfo, UnsafeAction, by_rc_id, safe_click, snapshot
from worker.llm import LLM, LLMError, choice
from worker.prompts import page_state, recovery_question
from worker.safety import is_allowed_url, is_forbidden_label

log = logging.getLogger("worker.recovery")

ACTIONS = {"click", "select_option", "scroll", "wait", "give_up"}
MIN_CONFIDENCE = 0.5
MAX_ELEMENT_OPTIONS = 240  # System One allows 255 options per choice question
_CLICKABLE_TAGS = {"a", "button", "label", "summary"}
_CLICKABLE_ROLES = {"button", "link", "radio", "checkbox", "tab", "option", "menuitem", "switch", "menuitemradio"}


class RecoveryFailed(Exception):
    pass


@dataclass
class RecoveryBudget:
    max_calls: int = 3
    max_actions: int = 4
    calls: int = 0
    actions: int = 0
    trace: list[str] = field(default_factory=list)

    def exhausted(self) -> bool:
        return self.calls >= self.max_calls or self.actions >= self.max_actions


def _offerable(e: ElementInfo) -> bool:
    if e.disabled or is_forbidden_label(f"{e.name} {e.text} {e.attrs.get('aria-label', '')}"):
        return False
    href = e.attrs.get("href")
    if href and browser._POLICY is not None and not is_allowed_url(href, browser._POLICY):
        return False
    is_choice_input = e.tag == "input" and e.attrs.get("type") in ("radio", "checkbox")
    return e.tag in _CLICKABLE_TAGS or e.role in _CLICKABLE_ROLES or is_choice_input


def action_options(elements: list[ElementInfo]) -> dict[str, str]:
    """The only actions the model may pick: vetted clicks, per-value dropdown selections, scroll, wait, give up."""
    opts: dict[str, str] = {}
    current_select: ElementInfo | None = None
    for e in elements:
        if len(opts) >= MAX_ELEMENT_OPTIONS:
            break
        if e.tag == "select":
            current_select = None if e.disabled else e
            continue
        if e.tag == "option":
            if current_select is not None and not e.disabled and e.name:
                opts[f"{current_select.id}|{e.name}"] = f"select {e.name!r} in dropdown {current_select.name!r}"
            continue
        current_select = None
        if _offerable(e):
            opts[e.id] = f"click {e.brief()[:140]}"
    opts["scroll"] = "scroll down to reveal more of the page"
    opts["wait"] = "wait for the page to finish loading"
    opts["give_up"] = "none of these actions clearly moves toward the goal"
    return opts


def validate_action(action: dict, elements: dict[str, ElementInfo]) -> str | None:
    """Return a refusal reason, or None if the action may run."""
    kind = action.get("action")
    if kind not in ACTIONS:
        return f"unknown action {kind!r}"
    if kind in ("scroll", "wait", "give_up"):
        return None
    el = elements.get(action.get("element_id") or "")
    if el is None:
        return f"unknown element {action.get('element_id')!r}"
    if kind == "select_option" and (el.tag != "select" or not action.get("value")):
        return "select_option needs a <select> element and a value"
    if is_forbidden_label(f"{el.name} {el.text} {el.attrs.get('aria-label', '')}"):
        return f"forbidden element {el.name!r}"
    if el.disabled:
        return f"element {el.name!r} is disabled"
    return None


def _to_action(pick: str) -> dict:
    if pick in ("scroll", "wait", "give_up"):
        return {"action": pick}
    if "|" in pick:
        el, value = pick.split("|", 1)
        return {"action": "select_option", "element_id": el, "value": value}
    return {"action": "click", "element_id": pick}


class Recovery:
    def __init__(self, llm: LLM | None, budget: RecoveryBudget):
        self.llm = llm
        self.budget = budget

    def attempt(self, page: Page, goal: str, failed_step: str) -> None:
        """Run until one valid action has executed; raise RecoveryFailed otherwise."""
        b = self.budget
        if self.llm is None:
            raise RecoveryFailed("recovery needed but LUNAROUTE_API_KEY not set")
        while True:
            if b.exhausted():
                raise RecoveryFailed(f"recovery budget exhausted ({b.calls} model calls, {b.actions} actions)")
            elements = snapshot(page)
            by_id = {e.id: e for e in elements}
            options = action_options(elements)
            b.calls += 1
            try:
                reply = self.llm.ask(purpose="recovery", state=page_state(page.url, elements),
                                     questions={"action": recovery_question(goal, failed_step, options)})
            except LLMError as e:
                raise RecoveryFailed(str(e)) from None
            pick, confidence = choice(reply, "action")
            if pick is None or pick == "give_up":
                b.trace.append(f"{failed_step}: model gave up")
                raise RecoveryFailed("model gave up: no listed action moves toward the goal")
            if confidence < MIN_CONFIDENCE:
                b.trace.append(f"{failed_step}: low confidence {confidence:.2f} for {options.get(pick, pick)!r}")
                raise RecoveryFailed(f"model confidence {confidence:.2f} too low to act")
            action = _to_action(pick)
            el = by_id.get(action.get("element_id") or "")
            label = el.name if el else pick
            refusal = None if pick in options else "not one of the offered actions"
            refusal = refusal or validate_action(action, by_id)
            if refusal is None:
                try:
                    self._execute(page, action)
                except UnsafeAction as e:
                    refusal = str(e)
            if refusal:
                b.trace.append(f"{failed_step}: refused {action['action']} {label!r}: {refusal}")
                log.warning("recovery refused: %s", refusal)
                continue
            b.actions += 1
            b.trace.append(f"{failed_step}: {action['action']} {label!r}"
                           + (f" = {action['value']!r}" if action.get("value") else "") + f" (confidence {confidence:.2f})")
            return

    @staticmethod
    def _execute(page: Page, action: dict) -> None:
        kind = action["action"]
        if kind == "click":
            safe_click(by_rc_id(page, action["element_id"]))
        elif kind == "select_option":
            by_rc_id(page, action["element_id"]).select_option(label=action["value"])
        elif kind == "scroll":
            page.mouse.wheel(0, 600)
        page.wait_for_timeout(500 if kind != "wait" else 1500)
