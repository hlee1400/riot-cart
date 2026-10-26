"""The fixed cart flow: search -> product -> options -> add -> verify cart. Recipe supplies store specifics."""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import quote_plus, urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from worker.browser import UnsafeAction, detect_blocker, resolve, safe_click
from worker.matching import match_option, match_title, normalize, parse_price, parse_quantity, strip_availability
from worker.models import CartLine, CartRequest, Price, Result, Status
from worker.recipe import OptionGroup, Recipe
from worker.recovery import Recovery, RecoveryFailed
from worker.safety import is_allowed_url

log = logging.getLogger("worker.flow")

OPTION_NAME_ALIASES = {"colour": "color", "colors": "color", "colours": "color", "sizes": "size"}
WAIT_MS = 15_000


class StepFailure(Exception):
    def __init__(self, status: Status, message: str, step: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.status, self.message, self.step, self.candidates = status, message, step, candidates or []


def _option_key(name: str) -> str:
    n = normalize(name)
    return OPTION_NAME_ALIASES.get(n, n)


class CartFlow:
    def __init__(self, page: Page, recipe: Recipe, request: CartRequest, recovery: Recovery | None):
        self.page, self.recipe, self.req, self.recovery = page, recipe, request, recovery
        self.title = ""
        self.product_url = ""
        self.product_price: Price | None = None
        self.chosen: dict[str, str] = {}
        self.defaulted: list[str] = []
        self.picked: list[tuple[OptionGroup, Locator]] = []
        self.qty_on_page = False

    # ---- helpers -------------------------------------------------------------------------
    def _goto(self, url: str, step: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightError:
            pass
        self._check(step)

    def _check(self, step: str) -> None:
        blocker = detect_blocker(self.page, self.recipe)
        if blocker:
            raise StepFailure(blocker, f"store showed a {'login wall' if blocker == Status.AUTH_REQUIRED else 'verification challenge'}; stopped", step)
        if not is_allowed_url(self.page.url, self.recipe):
            raise StepFailure(Status.FAILED, f"store redirected to {self.page.url} (outside the allowed storefront)", step)

    def _find(self, scope, target, wait_ms: int = WAIT_MS) -> Locator | None:
        page = scope if isinstance(scope, Page) else scope.page
        deadline = time.monotonic() + wait_ms / 1000
        while True:
            loc = resolve(scope, target)
            if loc is not None:
                return loc
            if time.monotonic() >= deadline:
                return None
            page.wait_for_timeout(250)

    def _recover(self, goal: str, step: str) -> None:
        self._check(step)  # a login wall or challenge ends the run; it never goes to recovery (spec 6.4)
        if self.recovery is None:
            raise StepFailure(Status.RECOVERY_FAILED, f"recovery needed ({goal}) but recovery is disabled", step)
        try:
            self.recovery.attempt(self.page, goal=goal, failed_step=step)
        except RecoveryFailed as e:
            raise StepFailure(Status.RECOVERY_FAILED, f"could not {goal}: {e}", step) from None

    def _require(self, target, what: str, step: str, goal: str) -> Locator:
        loc = self._find(self.page, target)
        if loc is None:
            self._recover(goal, step)
            loc = self._find(self.page, target, wait_ms=3_000)
        if loc is None:
            raise StepFailure(Status.FAILED, f"recipe target {what} not found on {self.page.url}", step)
        return loc

    # ---- steps ---------------------------------------------------------------------------
    def open(self) -> None:
        self._goto(self.recipe.base_url, "open")
        self._dismiss_cookies()

    def _dismiss_cookies(self) -> None:
        if not self.recipe.cookie_banner_accept:
            return
        loc = resolve(self.page, self.recipe.cookie_banner_accept)
        if loc is not None and loc.first.is_visible():
            try:
                safe_click(loc)
            except (UnsafeAction, PlaywrightError) as e:
                log.info("cookie banner not dismissed: %s", e)

    def search(self) -> None:
        s = self.recipe.search
        name = self.req.product_name
        if s.url_template:
            self._goto(s.url_template.replace("{query}", quote_plus(name)), "search")
        else:
            self._require(s.input, "search.input", "search", "open the store search box").first.fill(name)
            if s.submit and (btn := resolve(self.page, s.submit)) is not None:
                safe_click(btn)
            else:
                self.page.keyboard.press("Enter")
            self.page.wait_for_load_state("domcontentloaded")
            self._check("search")
        self._dismiss_cookies()
        items = self._find(self.page, s.result_item)
        rows: list[tuple[str, str]] = []
        for item in items.all() if items is not None else []:
            t = resolve(item, s.result_title)
            a = resolve(item, s.result_link)
            if t is None or a is None:
                continue
            href = a.first.get_attribute("href") or ""
            rows.append((t.first.inner_text().strip(), urljoin(self.page.url, href)))
        titles = [r[0] for r in rows]
        kind, idx = match_title(name, titles)
        if kind == "one":
            self.product_url = rows[idx[0]][1]
            return
        if kind == "many":
            raise StepFailure(Status.AMBIGUOUS_MATCH, f"{len(idx)} products are titled {name!r}", "search",
                              [titles[i] for i in idx])
        msg = (f"no product titled {name!r} in search results" if rows
               else f"search for {name!r} returned no results")
        raise StepFailure(Status.PRODUCT_NOT_FOUND, msg, "search", [titles[i] for i in idx])

    def product(self) -> None:
        self._goto(self.product_url, "product")
        self._dismiss_cookies()
        p = self.recipe.product
        title = " ".join((self._require(p.title, "product.title", "product", "find the product title").first.text_content() or "").split())
        if match_title(self.req.product_name, [title])[0] != "one":
            raise StepFailure(Status.FAILED, f"product page title {title!r} does not match {self.req.product_name!r}",
                              "product")
        self.title = title
        price = resolve(self.page, p.price)
        self.product_price = parse_price(price.first.inner_text()) if price is not None else None

    def options(self) -> None:
        groups = {_option_key(g.name): g for g in self.recipe.product.option_groups}
        alias = {_option_key(a): key for key, g in groups.items() for a in g.aliases}
        requested = {alias.get(_option_key(k), _option_key(k)): v for k, v in self.req.options.items()}
        for key, group in groups.items():
            loc = resolve(self.page, group.group)
            if loc is None:
                if key in requested:
                    raise StepFailure(Status.VARIANT_UNAVAILABLE, f"this product has no {group.name} choice",
                                      "options")
                continue
            self._choose(group, loc.first, requested.get(key))
        for key, value in requested.items():
            if key in groups:
                continue
            self._recover(f'select option "{key}" = "{value}"', "options")
            self.chosen[key] = value

    def _choose(self, group: OptionGroup, container: Locator, wanted: str | None) -> None:
        opts = resolve(container, group.option)
        elements = opts.all() if opts is not None else []
        labels, available = [], []
        for el in elements:
            raw = self._label(el, group)
            clean, flagged = strip_availability(raw)
            unavailable = flagged or el.evaluate("e => !!(e.disabled || e.getAttribute('aria-disabled') === 'true')")
            if group.unavailable_css:
                unavailable = unavailable or el.evaluate("(e, css) => e.matches(css)", group.unavailable_css)
            labels.append(raw)
            available.append(not unavailable)
        avail_labels = [strip_availability(lab)[0] for lab, ok in zip(labels, available) if ok]
        if wanted is not None:
            idx = match_option(wanted, labels)
            if idx is None or not available[idx]:
                state = "is sold out" if idx is not None else "is not offered"
                raise StepFailure(Status.VARIANT_UNAVAILABLE, f"{group.name} {wanted!r} {state}", "options",
                                  avail_labels)
        else:
            if not any(available):
                if not group.required:
                    return
                raise StepFailure(Status.VARIANT_UNAVAILABLE, f"no {group.name} is available", "options")
            idx = available.index(True)
        el = elements[idx]
        if group.kind == "select":
            container.select_option(label=labels[idx])
        else:
            safe_click(el)
        if not self._selected(el, group):
            self._recover(f'select option "{group.name}" = "{strip_availability(labels[idx])[0]}"', "options")
            if not self._selected(el, group):
                raise StepFailure(Status.FAILED, f"could not confirm {group.name} selection", "options")
        self.chosen[group.name] = strip_availability(labels[idx])[0]
        self.picked.append((group, el))
        if wanted is None:
            self.defaulted.append(group.name)

    @staticmethod
    def _label(el: Locator, group: OptionGroup) -> str:
        if group.value_label == "text":
            return el.inner_text().strip()
        return (el.get_attribute(group.value_label) or el.inner_text()).strip()

    def _selected(self, el: Locator, group: OptionGroup) -> bool:
        deadline = time.monotonic() + 2
        while True:
            if el.evaluate("(e, css) => e.matches(css)", group.selected_css):
                return True
            if time.monotonic() >= deadline:
                return False
            self.page.wait_for_timeout(200)

    def quantity(self) -> None:
        q = self.recipe.product.quantity_input
        loc = resolve(self.page, q) if q else None
        self.qty_on_page = loc is not None
        if loc is not None:
            loc.first.fill(str(self.req.quantity))

    def _click_add(self) -> None:
        btn = self._require(self.recipe.product.add_to_cart, "product.add_to_cart", "add", "find the add to cart button")
        if btn.first.is_disabled():
            label = btn.first.inner_text().strip() or "disabled"
            raise StepFailure(Status.VARIANT_UNAVAILABLE,
                              f"add to cart is disabled ({label!r}): this item/choice is out of stock", "add")
        safe_click(btn)
        self._check("add")

    def _added_signal(self, wait_ms: int) -> bool:
        sig = self.recipe.product.added_signal
        loc = self._find(self.page, sig, wait_ms=wait_ms)
        return loc is not None and loc.first.is_visible()

    def add(self) -> None:
        """Click add once. Re-click only after the cart was checked and does not hold the item.

        A missing "added" signal does not prove the add failed (it may be slow, or recovery may have clicked add
        itself), so the cart is reconciled before any re-click. Recovery's budget bounds the loop.
        """
        self._click_add()
        if not self.recipe.product.added_signal:
            self.page.wait_for_timeout(1_500)
            return
        if self._added_signal(5_000) or self._line_in_cart():
            return
        while True:
            self._recover("the item was not added to the cart; select the first available value for any "
                          "required option that is not selected yet (do not click add to cart)", "add")
            self._confirm_choices()
            if self._added_signal(1_000) or self._line_in_cart():
                return
            self._click_add()
            if self._added_signal(5_000) or self._line_in_cart():
                return

    def _confirm_choices(self) -> None:
        """After recovery, every option the flow chose must still be selected: recovery may not swap one."""
        for group, el in self.picked:
            if not self._selected(el, group):
                raise StepFailure(Status.FAILED, f"{group.name} {self.chosen[group.name]!r} is no longer selected "
                                  "after recovery; not adding a different variant", "add")

    def _line_in_cart(self) -> bool:
        """Does the cart already hold this product? Checked without leaving the product page."""
        c = self.recipe.cart
        try:
            if c.url:
                page = self.page.context.new_page()
                try:
                    page.goto(c.url, wait_until="domcontentloaded")
                    return any(self._is_mine(t) for _, t in self._lines(page, wait_ms=3_000))
                finally:
                    page.close()
            self._open_cart()
            if any(self._is_mine(t) for _, t in self._lines(self.page, wait_ms=3_000)):
                return True
            self.page.keyboard.press("Escape")  # close the drawer again so recovery sees the product page
            return False
        except PlaywrightError as e:
            raise StepFailure(Status.FAILED, "could not check the cart after adding, so not adding again "
                              f"({str(e).splitlines()[0][:120]})", "add") from None

    def _open_cart(self) -> None:
        c = self.recipe.cart
        if c.url:
            self._goto(c.url, "cart")
            return
        lines = resolve(self.page, c.line_item)
        if lines is not None and lines.first.is_visible():
            return  # the drawer is already open (many stores open it after adding)
        safe_click(self._require(c.open_button, "cart.open_button", "cart", "open the shopping cart"))
        self.page.wait_for_timeout(800)
        self._check("cart")

    def cart(self) -> CartLine:
        c = self.recipe.cart
        self._open_cart()
        line = self._cart_line()
        if line is None:
            raise StepFailure(Status.FAILED, f"{self.title!r} was not found in the cart after adding it", "cart")
        observed_qty = self._line_qty(line)
        if observed_qty != self.req.quantity and not self.qty_on_page:
            line, observed_qty = self._adjust_qty(line, observed_qty)
        if observed_qty != self.req.quantity:
            raise StepFailure(Status.FAILED, f"cart shows quantity {observed_qty}, requested {self.req.quantity}"
                              + ("" if self.qty_on_page or c.quantity_control or c.quantity_increase else
                                 " (quantity > 1 not supported for this item)"), "cart")
        text = self._line_options_text(line)
        missing = [k for k, v in self.chosen.items() if not _value_in(v, text)]
        if missing:
            raise StepFailure(Status.FAILED, f"cart line does not show the chosen {', '.join(missing)} "
                              f"(line says {text[:120]!r})", "cart")
        price = None
        if c.line_price and (pl := resolve(line, c.line_price)) is not None:
            price = parse_price(pl.first.inner_text())
        return CartLine(product_name=self.title, product_url=self.product_url, options=dict(self.chosen),
                        defaulted=list(self.defaulted), quantity=observed_qty, observed_price=price or self.product_price,
                        verified=True)

    def _adjust_qty(self, line: Locator, observed: int | None) -> tuple[Locator, int | None]:
        c = self.recipe.cart
        if c.quantity_control and (ctl := resolve(line, c.quantity_control)) is not None:
            ctl.first.fill(str(self.req.quantity))
            ctl.first.press("Tab")
            self.page.wait_for_timeout(1_000)
            self._open_cart()
        elif c.quantity_increase and observed is not None and observed < self.req.quantity:
            for _ in range(self.req.quantity - observed):
                before = self._line_qty(line)
                plus = resolve(line, c.quantity_increase)
                if plus is None:
                    break
                safe_click(plus)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    self.page.wait_for_timeout(250)
                    line = self._cart_line() or line
                    if self._line_qty(line) != before:
                        break
        else:
            return line, observed
        line = self._cart_line()
        return line, (self._line_qty(line) if line is not None else None)

    def _lines(self, page: Page, wait_ms: int) -> list[tuple[Locator, str]]:
        c = self.recipe.cart
        lines = self._find(page, c.line_item, wait_ms=wait_ms)
        rows = []
        for line in lines.all() if lines is not None else []:
            t = resolve(line, c.line_title)
            rows.append((line, t.first.inner_text().strip() if t is not None else ""))
        return rows

    def _is_mine(self, title: str) -> bool:
        return normalize(title) == normalize(self.title)

    def _cart_line(self) -> Locator | None:
        """The one line of this product. A fresh guest cart must hold nothing else."""
        rows = self._lines(self.page, wait_ms=8_000)
        mine = [line for line, t in rows if self._is_mine(t)]
        if not mine:
            return None
        if len(rows) > 1:
            others = [t or "(untitled line)" for _, t in rows if not self._is_mine(t)]
            raise StepFailure(Status.FAILED, f"the fresh cart holds {len(rows)} lines ({len(mine)} of {self.title!r}"
                              + (f"; also {', '.join(repr(o) for o in others[:3])}" if others else "")
                              + "), expected exactly one", "cart")
        return mine[0]

    def _line_qty(self, line: Locator) -> int | None:
        q = resolve(line, self.recipe.cart.line_quantity)
        if q is None:
            return None
        el = q.first
        is_input = el.evaluate("e => ['INPUT', 'SELECT'].includes(e.tagName)")
        return parse_quantity(el.input_value() if is_input else el.inner_text())

    def _line_options_text(self, line: Locator) -> str:
        """Each leaf text on its own line, so adjacent tag spans (Riot: <span>M</span><span>Black</span>) stay apart."""
        lo = self.recipe.cart.line_options
        loc = resolve(line, lo) if lo else None
        return "\n".join((loc if loc is not None else line.first).evaluate_all(_LEAF_TEXTS_JS))

    # ---- driver --------------------------------------------------------------------------
    def run(self) -> CartLine:
        self.open()
        self.search()
        self.product()
        self.options()
        self.quantity()
        self.add()
        return self.cart()


_LEAF_TEXTS_JS = """
els => els.flatMap(e => {
  const leaves = [...e.querySelectorAll('*')].filter(c => !c.children.length && (c.textContent || '').trim());
  return leaves.length ? leaves.map(c => c.textContent.trim()) : [(e.innerText || e.textContent || '').trim()];
})
"""


def _value_in(value: str, text: str) -> bool:
    """True only if one separated part of the cart text IS the value (sizes canonicalised); never a substring."""
    parts = [p.split(":", 1)[-1] for p in re.split(r"[/|,;\n]", text)]
    return match_option(value, parts) is not None


def _message(line: CartLine, model_calls: int) -> str:
    opts = ", ".join(f"{k} {v}" + (" — default" if k in line.defaulted else "") for k, v in line.options.items())
    price = f" — ${line.observed_price.amount} {line.observed_price.currency}" if line.observed_price else ""
    msg = f"Added {line.product_name}{f' ({opts})' if opts else ''} x{line.quantity} to cart{price}, verified on cart page."
    if model_calls:
        msg += f" (recovered with {model_calls} model call{'s' if model_calls != 1 else ''})"
    return msg


def run_cart(page: Page, recipe: Recipe, request: CartRequest, recovery: Recovery | None) -> Result:
    if not request.recovery:
        recovery = None
    flow = CartFlow(page, recipe, request, recovery)
    budget = recovery.budget if recovery else None
    calls = lambda: budget.calls if budget else 0  # noqa: E731
    try:
        line = flow.run()
        result = Result(status=Status.CART_READY, message=_message(line, calls()), cart=line)
    except StepFailure as e:
        result = Result.fail(e.status, e.message, step=e.step, candidates=e.candidates)
    except UnsafeAction as e:
        result = Result.fail(Status.FAILED, f"stopped: {e}")
    except PlaywrightError as e:
        result = Result.fail(Status.FAILED, f"browser error: {str(e).splitlines()[0][:200]}")
    result.recipe_version = recipe.recipe_version
    if budget:
        result.model_calls = budget.calls
        result.recovery_used = budget.calls > 0
        result.recovery_trace = list(budget.trace)
    return result
