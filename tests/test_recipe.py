from pathlib import Path

import pytest
import yaml

from worker.recipe import CssLocator, RecipeError, RoleLocator, load_recipe

FIXTURE = Path(__file__).parent / "fixtures" / "store_recipe.yaml"


def _write(tmp_path, data) -> Path:
    p = tmp_path / "r.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _raw():
    return yaml.safe_load(FIXTURE.read_text())


def test_loads_fixture_recipe():
    r = load_recipe(FIXTURE)
    assert [g.name for g in r.product.option_groups] == ["size", "color"]
    assert r.allowed_path_prefixes == ["/en-us/"]
    assert isinstance(r.product.add_to_cart[0], RoleLocator)
    assert any(isinstance(loc, CssLocator) for loc in r.product.add_to_cart)


def test_rejects_unknown_field(tmp_path):
    data = _raw()
    data["product"]["surprise"] = 1
    with pytest.raises(RecipeError):
        load_recipe(_write(tmp_path, data))


def test_search_requires_exactly_one_mode(tmp_path):
    both = _raw()
    both["search"]["input"] = [{"css": "#q"}]
    with pytest.raises(RecipeError):
        load_recipe(_write(tmp_path, both))
    neither = _raw()
    neither["search"]["url_template"] = None
    with pytest.raises(RecipeError):
        load_recipe(_write(tmp_path, neither))


def test_url_template_needs_query_placeholder(tmp_path):
    data = _raw()
    data["search"]["url_template"] = "https://store.test/en-us/search"
    with pytest.raises(RecipeError):
        load_recipe(_write(tmp_path, data))


def test_empty_target_rejected(tmp_path):
    data = _raw()
    data["product"]["title"] = []
    with pytest.raises(RecipeError):
        load_recipe(_write(tmp_path, data))


def test_cart_needs_url_or_open_button(tmp_path):
    data = _raw()
    del data["cart"]["url"]
    with pytest.raises(RecipeError):
        load_recipe(_write(tmp_path, data))
    data["cart"]["open_button"] = [{"css": ".cart"}]
    assert load_recipe(_write(tmp_path, data)).cart.url is None
