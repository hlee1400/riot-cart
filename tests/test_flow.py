import json

from worker.flow import run_cart
from worker.models import CartRequest, Status
from conftest import pick_option
from worker.recovery import Recovery, RecoveryBudget


def req(name, **kw):
    return CartRequest(product_name=name, **kw)


def test_named_and_defaulted(store_page, store_recipe):
    r = run_cart(store_page, store_recipe, req("Arcane Jinx Hoodie", options={"size": "medium"}), None)
    assert r.status == Status.CART_READY, r.message
    assert r.cart.options == {"size": "M", "color": "Black"}
    assert r.cart.defaulted == ["color"]
    assert r.cart.verified and r.cart.quantity == 1
    assert r.cart.observed_price.amount == "64.99"
    assert r.model_calls == 0
    assert "color Black — default" in r.message


def test_named_color(store_page, store_recipe):
    r = run_cart(store_page, store_recipe, req("arcane jinx hoodie", options={"size": "S", "colour": "blue"}), None)
    assert r.status == Status.CART_READY, r.message
    assert r.cart.options == {"size": "S", "color": "Blue"}
    assert r.cart.defaulted == []


def test_sold_out_named_size(store_page, store_recipe):
    r = run_cart(store_page, store_recipe, req("Arcane Jinx Hoodie", options={"size": "L"}), None)
    assert r.status == Status.VARIANT_UNAVAILABLE
    assert r.candidates == ["S", "M"]


def test_option_not_on_product(store_page, store_recipe):
    r = run_cart(store_page, store_recipe, req("Teemo Mug", options={"size": "M"}), None)
    assert r.status == Status.VARIANT_UNAVAILABLE


def test_product_not_found(store_page, store_recipe):
    r = run_cart(store_page, store_recipe, req("Jinx Hoodie Deluxe"), None)
    assert r.status == Status.PRODUCT_NOT_FOUND
    assert "Arcane Jinx Hoodie" in r.candidates


def test_kids_variant_not_confused(store_page, store_recipe):
    r = run_cart(store_page, store_recipe, req("Arcane Jinx Hoodie", options={"size": "M"}), None)
    assert r.status == Status.CART_READY


def test_ambiguous(store_page, store_recipe):
    r = run_cart(store_page, store_recipe, req("Poro Pin"), None)
    assert r.status == Status.AMBIGUOUS_MATCH
    assert r.candidates == ["Poro Pin", "Poro Pin"]


def test_quantity_two(store_page, store_recipe):
    r = run_cart(store_page, store_recipe, req("Teemo Mug", options={}, quantity=2),
                 Recovery(None, RecoveryBudget()))
    # The mug needs an edition the recipe doesn't know; without a model this must fail cleanly.
    assert r.status == Status.RECOVERY_FAILED
    r = run_cart(store_page, store_recipe, req("Arcane Jinx Hoodie", options={"size": "M"}, quantity=2), None)
    assert r.status == Status.CART_READY and r.cart.quantity == 2


def test_unknown_option_uses_recovery(store_page, store_recipe, stub_llm):
    llm = stub_llm([pick_option("action", "Collector's")])
    budget = RecoveryBudget()
    r = run_cart(store_page, store_recipe, req("Teemo Mug", options={"edition": "Collector's"}), Recovery(llm, budget))
    assert r.status == Status.CART_READY, r.message
    assert r.cart.options == {"edition": "Collector's"}
    assert r.recovery_used and r.model_calls == 1
    assert "recovered" in r.message


def test_unmentioned_unknown_option_recovers_after_failed_add(store_page, store_recipe, stub_llm):
    llm = stub_llm([pick_option("action", "Standard")])
    r = run_cart(store_page, store_recipe, req("Teemo Mug"), Recovery(llm, RecoveryBudget()))
    assert r.status == Status.CART_READY, r.message
    assert r.model_calls == 1


def test_recovery_disabled_fails_cleanly(store_page, store_recipe):
    r = run_cart(store_page, store_recipe, req("Teemo Mug", options={"edition": "Standard"}, recovery=False), None)
    assert r.status == Status.RECOVERY_FAILED
    assert r.cart is None


def test_cli_last_line_is_json_on_crash(monkeypatch, capsys, tmp_path):
    import worker.__main__ as cli

    def boom(args):
        raise RuntimeError("kaboom with key sk-123")

    monkeypatch.setenv("LUNAROUTE_API_KEY", "sk-123")
    monkeypatch.setattr(cli, "run_once", boom)
    (tmp_path / "req.json").write_text(json.dumps({"product_name": "x"}))
    code = cli.main(["run", "--recipe", "r.yaml", "--request", str(tmp_path / "req.json")])
    last = capsys.readouterr().out.strip().splitlines()[-1]
    data = json.loads(last)
    assert code == 0
    assert data["status"] == "FAILED" and "kaboom" in data["message"]
    assert "sk-123" not in last


def test_drawer_cart_with_plus_button(store_page):
    from pathlib import Path
    from worker.recipe import load_recipe
    recipe = load_recipe(Path(__file__).parent / "fixtures" / "drawer_recipe.yaml")
    r = run_cart(store_page, recipe, req("Drawer Tee", options={"size": "M"}, quantity=2), None)
    assert r.status == Status.CART_READY, r.message
    assert r.cart.options == {"size": "M"} and r.cart.quantity == 2


def test_disabled_add_button_is_unavailable(store_page, store_recipe):
    r = run_cart(store_page, store_recipe, req("Sold Out Cap"), None)
    assert r.status == Status.VARIANT_UNAVAILABLE
    assert "out of stock" in r.message.lower()


def test_option_group_alias(store_page, store_recipe):
    r = run_cart(store_page, store_recipe, req("Arcane Jinx Hoodie", options={"size": "M", "shade": "Blue"}), None)
    assert r.status == Status.CART_READY, r.message
    assert r.cart.options == {"size": "M", "color": "Blue"}


def test_reports_title_as_written_not_css_uppercased(store_page):
    from pathlib import Path
    from worker.recipe import load_recipe
    recipe = load_recipe(Path(__file__).parent / "fixtures" / "drawer_recipe.yaml")
    r = run_cart(store_page, recipe, req("Drawer Tee", options={"size": "S"}), None)
    assert r.status == Status.CART_READY, r.message
    assert r.cart.product_name == "Drawer Tee"


# ---- finding 1: cart option verification is whole-value, not substring ----------------------
import pytest

from worker.flow import _value_in


@pytest.mark.parametrize("value,text", [
    ("XL", "Size: XXL"),
    ("S", "Size: M / Color: Black"),
    ("L", "Black / M"),
    ("M", "Size: Medium Tall"),
    ("Black", "Black Gold"),
    ("Framed", "Unframed\n12x18"),
])
def test_value_in_rejects_other_variants(value, text):
    assert not _value_in(value, text)


@pytest.mark.parametrize("value,text", [
    ("XL", "Size: XL"),
    ("M", "Size: M / Color: Black"),
    ("medium", "Size: M"),
    ("M", "Size: Medium"),
    ("Black", "Black / M"),
    ("Collector's", "Edition: Collector's"),
    ("Snowdown", "Snowdown\nFramed\n12x18"),
    ("12x18", "Snowdown\nFramed\n12x18"),
    ("Framed", "Snowdown\nFramed\n12x18"),
    ("S", "S"),
])
def test_value_in_accepts_shown_value(value, text):
    assert _value_in(value, text)


def test_riot_tag_spans_read_as_separate_values(store_page):
    """Riot renders each chosen option as an adjacent tag span with no separator between them."""
    from pathlib import Path
    from worker.flow import CartFlow
    from worker.recipe import load_recipe
    riot = load_recipe(Path(__file__).parents[1] / "retailer-recipes" / "riot_games" / "recipe.yaml")
    store_page.set_content(
        '<div class="cart_cart-item__x"><div class="cart-line-item_cart-line-item-tags__y">'
        '<span class="tag_tag__a">Snowdown</span><span class="tag_tag__a">Framed</span>'
        '<span class="tag_tag__a">12x18</span></div></div>')
    flow = CartFlow(store_page, riot, req("Poster"), None)
    line = store_page.locator('[class*="cart_cart-item__"]')
    text = flow._line_options_text(line)
    for v in ("Snowdown", "Framed", "12x18"):
        assert _value_in(v, text), text
    assert not _value_in("Unframed", text)
    assert not _value_in("Snow", text)


def test_riot_cart_drawer_fixture_options_text(store_page):
    from pathlib import Path
    from worker.browser import resolve
    from worker.flow import CartFlow
    from worker.recipe import load_recipe
    root = Path(__file__).parents[1] / "retailer-recipes" / "riot_games"
    riot = load_recipe(root / "recipe.yaml")
    store_page.set_content((root / "fixtures" / "cart_drawer.html").read_text(encoding="utf-8"))
    line = resolve(store_page, riot.cart.line_item)
    text = CartFlow(store_page, riot, req("VALORANT Jett Knives Hoodie"), None)._line_options_text(line.first)
    assert _value_in("S", text) and _value_in("small", text)
    assert not _value_in("M", text) and not _value_in("XS", text)


# ---- findings 2 and 4: never re-add blindly ----------------------------------------------------
def _lab(store_page, store_recipe, llm, name, **kw):
    return run_cart(store_page, store_recipe, req(name, options={"size": "M"}, **kw), Recovery(llm, RecoveryBudget()))


def test_slow_added_signal_is_reconciled_not_re_added(store_page, store_recipe, stub_llm):
    llm = stub_llm([pick_option("action", "'Add to Cart'")] * 3)
    r = _lab(store_page, store_recipe, llm, "Lab Slow Scarf")
    assert r.status == Status.CART_READY, r.message
    assert r.cart.quantity == 1
    assert llm.calls == [] and r.model_calls == 0


def test_recovery_that_adds_is_not_followed_by_another_click(store_page, store_recipe, stub_llm):
    llm = stub_llm([pick_option("action", "'Add to Cart'")] * 3)
    r = _lab(store_page, store_recipe, llm, "Lab Lazy Scarf")
    assert r.status == Status.CART_READY, r.message
    assert r.cart.quantity == 1 and r.cart.options == {"size": "M"}
    assert r.model_calls == 1


def test_recovery_changing_a_chosen_option_fails(store_page, store_recipe, stub_llm):
    llm = stub_llm([pick_option("action", "'L'")] * 3)
    r = _lab(store_page, store_recipe, llm, "Lab Lazy Scarf")
    assert r.status == Status.FAILED, r.message
    assert "size" in r.message and r.failed_step == "add"


def test_extra_unrelated_cart_line_fails(store_page, store_recipe):
    r = run_cart(store_page, store_recipe, req("Lab Gift Scarf", options={"size": "M"}), None)
    assert r.status == Status.FAILED, r.message
    assert "Free Sticker" in r.message and r.failed_step == "cart"


def test_two_lines_of_the_product_fail(store_page, store_recipe):
    r = run_cart(store_page, store_recipe, req("Lab Twin Scarf", options={"size": "M"}), None)
    assert r.status == Status.FAILED, r.message
    assert r.failed_step == "cart"


def test_add_disabled_after_recovery_is_unavailable(store_page, store_recipe, stub_llm):
    llm = stub_llm([pick_option("action", "limited run")] * 3)
    r = _lab(store_page, store_recipe, llm, "Lab Limit Scarf")
    assert r.status == Status.VARIANT_UNAVAILABLE, r.message
    assert "out of stock" in r.message


# ---- finding 3: a login wall or challenge ends the run before any recovery ----------------------
def test_login_after_add_click_stops_without_recovery(store_page, store_recipe, stub_llm):
    llm = stub_llm([])
    r = _lab(store_page, store_recipe, llm, "Lab Gate Scarf")
    assert r.status == Status.AUTH_REQUIRED, r.message
    assert llm.calls == [] and not r.recovery_used


def test_login_appearing_later_stops_before_recovery(store_page, store_recipe, stub_llm):
    llm = stub_llm([])
    r = _lab(store_page, store_recipe, llm, "Lab Lategate Scarf")
    assert r.status == Status.AUTH_REQUIRED, r.message
    assert llm.calls == [] and not r.recovery_used
