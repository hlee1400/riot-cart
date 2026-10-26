import json

from conftest import answer
from worker.browser import snapshot
from worker.discover import candidate_locators, discover_page, find_option_groups, sanitize_html

BASE = "https://store.test/en-us"
HOODIE = f"{BASE}/p/hoodie"


def choose(**needles):
    """Reply answering each choice question by the option whose description contains the needle;
    every noul question gets 0.9, unlisted choice questions get 'none'."""
    def reply(state, questions):
        out = {}
        for qid, q in questions.items():
            if q["type"] == "noul":
                out[qid] = {"type": "noul", "noul": 0.9}
                continue
            needle = needles.get(qid)
            key = next((k for k, d in q["criteria"].items() if needle and needle in d), "none")
            out[qid] = answer(key, 0.93)
        return out
    return reply


def test_find_option_groups(store_page):
    store_page.goto(HOODIE)
    groups = {g["name"].lower(): g for g in find_option_groups(store_page)}
    size, color = groups["size"], groups["color"]
    assert size["kind"] == "buttons" and [o["label"] for o in size["options"]] == ["S", "M", "L - Sold Out"]
    assert [o["disabled"] for o in size["options"]] == [False, False, True]
    assert size["selected_css"] == "[aria-pressed=true]"
    assert color["kind"] == "select" and [o["label"] for o in color["options"]] == ["Black", "Blue"]
    assert {"css": "div.sizes", "matches": 1} in size["group_candidates"]
    assert {"css": "button.size", "matches": 3} in size["option_candidates"]


def test_find_option_groups_hidden_radios(store_page):
    store_page.goto(f"{BASE}/p/drawer")
    (g,) = find_option_groups(store_page)
    assert g["kind"] == "radio" and [o["label"] for o in g["options"]] == ["S", "M"]
    assert g["selected_css"] == ":has(input:checked)" and g["unavailable_css"] == ":has(input:disabled)"
    assert {"css": "label.opt", "matches": 2} in g["option_candidates"]


def test_product_discovery(store_page, stub_llm):
    store_page.goto(HOODIE)
    llm = stub_llm([choose(title="Arcane Jinx Hoodie", price="$64.99", add_to_cart="Add to Cart",
                           quantity_input="Quantity", cart_link="'Cart'")])
    p = discover_page(store_page, llm, "product")
    assert p["add_to_cart"]["label"] == "Add to Cart" and p["add_to_cart"]["confidence"] == 0.93
    assert p["add_to_cart"]["candidates"][0]["role"] == "button"
    assert p["title"]["label"] == "Arcane Jinx Hoodie"
    assert p["added_signal"] is None
    assert sorted(g["name"].lower() for g in p["option_groups"]) == ["color", "size"]
    assert all(g["is_main_product"] == 0.9 for g in p["option_groups"])
    call = llm.calls[0]
    assert isinstance(call["state"], str) and "Add to Cart" in call["state"]
    assert all(q["type"] in ("choice", "noul") for q in call["questions"].values())
    assert all(len(q.get("criteria", {})) <= 255 for q in call["questions"].values())


def test_none_answer_is_null(store_page, stub_llm):
    store_page.goto(HOODIE)
    p = discover_page(store_page, stub_llm([choose()]), "product")
    assert p["add_to_cart"] is None and p["title"] is None


def test_search_discovery_finds_repeating_card(store_page, stub_llm):
    store_page.goto(f"{BASE}/search?q=hoodie")
    store_page.wait_for_selector("li.result")
    llm = stub_llm([choose(first_result_link="Arcane Jinx Hoodie"), choose(result_title="Arcane Jinx Hoodie")])
    p = discover_page(store_page, llm, "search")
    assert {"css": "li.result", "matches": 2} in p["result_item"]["candidates"]
    assert p["result_link"]["candidates"][0]["matches"] >= 1
    assert any(c.get("css") == "span.result-title" for c in p["result_title"]["candidates"])


def test_cart_discovery_line_parts(store_page, stub_llm):
    store_page.goto(HOODIE)
    store_page.get_by_role("button", name="M", exact=True).click()
    store_page.locator("#add").click()
    store_page.goto(f"{BASE}/cart")
    store_page.wait_for_selector(".line")
    llm = stub_llm([choose(line_title="Arcane Jinx Hoodie"),
                    choose(line_price="$64.99", line_quantity="Line quantity", line_options="Size: M")])
    p = discover_page(store_page, llm, "cart")
    assert {"css": "div.line", "matches": 1} in p["line_item"]["candidates"]
    assert p["line_price"]["candidates"][0] == {"css": "span.line-price", "matches": 1}
    assert p["line_quantity"]["label"] == "Line quantity"
    assert p["line_options"]["label"].startswith("Size: M")


def test_candidate_locators_role_first(store_page):
    store_page.goto(HOODIE)
    eid = next(e.id for e in snapshot(store_page) if e.name == "Add to Cart")
    cands = candidate_locators(store_page, eid)
    assert cands[0]["role"] == "button" and cands[0]["name"] == "Add to Cart" and cands[0]["matches"] == 1
    assert {"css": "#add", "matches": 1} in cands
    assert not any("data-rc" in json.dumps(c) for c in cands)


def test_sanitize_html():
    html = '<html><script>var token="x"</script><input name=email value="a@b.com"><p>hi</p></html>'
    out = sanitize_html(html)
    assert "<script" not in out and "a@b.com" not in out and "<p>hi</p>" in out


def test_groups_carry_location_context(store_page):
    store_page.goto(HOODIE)
    store_page.evaluate("""() => {
      const wrap = document.createElement('section'); wrap.className = 'shop-carousel_item__AbC12';
      wrap.innerHTML = '<div class="sizes" role="group" aria-label="Size"><button class="size">S</button><button class="size">M</button></div>';
      document.body.appendChild(wrap);
    }""")
    groups = [g for g in find_option_groups(store_page) if g["name"] == "Size"]
    assert len(groups) == 2
    assert "shop-carousel_item" in groups[1]["context"] and "shop-carousel" not in groups[0]["context"]


def test_duplicate_group_names_keep_first(store_page, stub_llm):
    store_page.goto(HOODIE)
    store_page.evaluate("""() => {
      const wrap = document.createElement('section'); wrap.className = 'carousel';
      wrap.innerHTML = '<div role="group" aria-label="Size"><button class="size">S</button><button class="size">M</button></div>';
      document.body.appendChild(wrap);
    }""")
    p = discover_page(store_page, stub_llm([choose(), choose(), choose()]), "product")
    names = [g["name"] for g in p["option_groups"]]
    assert names.count("Size") == 1


def test_cart_line_container_contains_price(store_page, stub_llm):
    store_page.goto(HOODIE)
    store_page.get_by_role("button", name="M", exact=True).click()
    store_page.locator("#add").click()
    store_page.goto(f"{BASE}/cart")
    store_page.wait_for_selector(".line")
    store_page.evaluate("""() => { const t = document.querySelector('.line-title');
      const w = document.createElement('div'); w.className = 'cart-line-item-title'; t.replaceWith(w); w.appendChild(t); }""")
    llm = stub_llm([choose(line_title="Arcane Jinx Hoodie"), choose(line_price="$64.99")])
    p = discover_page(store_page, llm, "cart")
    assert {"css": "div.line", "matches": 1} in p["line_item"]["candidates"]
