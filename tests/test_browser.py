import pytest

from worker.browser import UnsafeAction, detect_blocker, resolve, safe_click, snapshot
from worker.models import Status
from worker.recipe import CssLocator, RoleLocator

BASE = "https://store.test/en-us"


def test_guard_blocks_offsite_navigation(store_page):
    with pytest.raises(Exception):
        store_page.goto("https://pay.test/checkout")
    assert store_page.blocked == ["https://pay.test/checkout"]


def test_guard_allows_store(store_page):
    store_page.goto(f"{BASE}/p/hoodie")
    assert store_page.locator("h1").inner_text() == "Arcane Jinx Hoodie"


def test_safe_click_refuses_checkout_link(store_page):
    store_page.goto(f"{BASE}/p/hoodie")
    with pytest.raises(UnsafeAction):
        safe_click(store_page.locator("#checkout"))


def test_safe_click_refuses_cart_checkout_button(store_page):
    store_page.goto(f"{BASE}/cart")
    with pytest.raises(UnsafeAction):
        safe_click(store_page.get_by_role("button", name="Proceed to Checkout"))
    assert store_page.url.endswith("/en-us/cart")


def test_safe_click_allows_add(store_page):
    store_page.goto(f"{BASE}/p/pin")
    safe_click(store_page.locator("#add"))
    assert store_page.locator("#added").is_visible()


def test_safe_click_hidden_radio_clicks_label(store_page):
    store_page.goto(f"{BASE}/p/pin")
    store_page.set_content(
        '<fieldset><label for="r-m"><input id="r-m" type="radio" name="s" style="position:absolute;opacity:0;'
        'width:0;height:0;pointer-events:none"><span>M</span></label></fieldset>')
    safe_click(store_page.locator("#r-m"), timeout=2000)
    assert store_page.locator("#r-m").is_checked()


def test_snapshot_lists_sizes_without_values(store_page):
    store_page.goto(f"{BASE}/p/hoodie")
    els = snapshot(store_page)
    names = [e.name for e in els]
    assert "M" in names and "L - Sold Out" in names
    sold_out = next(e for e in els if e.name == "L - Sold Out")
    assert sold_out.disabled
    assert all(e.id.startswith("e") for e in els)
    qty = next(e for e in els if e.name == "Quantity")
    assert "value" not in qty.attrs
    assert store_page.locator(f"[data-rc-id={sold_out.id}]").inner_text() == "L - Sold Out"


def test_snapshot_skips_decorative(store_page):
    store_page.goto(f"{BASE}/p/pin")
    store_page.set_content('<svg role="presentation"></svg><img role="none" src="x.png"><button>Go</button>')
    assert [e.name for e in snapshot(store_page)] == ["Go"]


def test_resolve_uses_fallback(store_page):
    store_page.goto(f"{BASE}/p/hoodie")
    loc = resolve(store_page, [CssLocator(css="#nope"), RoleLocator(role="button", name="Add to Cart")])
    assert loc is not None and loc.first.get_attribute("id") == "add"
    assert resolve(store_page, [CssLocator(css="#nope")]) is None


def test_detect_blocker_login(store_page, store_recipe):
    store_page.goto(f"{BASE}/p/pin")
    store_page.set_content("<form><input type=password></form>")
    assert detect_blocker(store_page, store_recipe) == Status.AUTH_REQUIRED


def test_detect_blocker_captcha(store_page, store_recipe):
    store_page.goto(f"{BASE}/p/pin")
    store_page.set_content('<iframe src="https://hcaptcha.com/x"></iframe>')
    assert detect_blocker(store_page, store_recipe) == Status.VERIFICATION_REQUIRED


def test_detect_blocker_ignores_hidden_passive_captcha(store_page, store_recipe):
    store_page.goto(f"{BASE}/p/pin")
    store_page.set_content('<p>shop</p><iframe aria-hidden="true" style="display:none" src="https://newassets.hcaptcha.com/x"></iframe>'
                           '<iframe src="https://hcaptcha.com/c" style="visibility:hidden;width:0;height:0"></iframe>')
    assert detect_blocker(store_page, store_recipe) is None


def test_detect_blocker_none(store_page, store_recipe):
    store_page.goto(f"{BASE}/p/pin")
    assert detect_blocker(store_page, store_recipe) is None
