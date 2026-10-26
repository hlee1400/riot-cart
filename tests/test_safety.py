from pathlib import Path

import pytest

from worker.recipe import load_recipe
from worker.safety import is_allowed_url, is_forbidden_label, redact

RECIPE = load_recipe(Path(__file__).parent / "fixtures" / "store_recipe.yaml")


@pytest.mark.parametrize("url", ["https://store.test/en-us/p/1", "https://store.test/en-us/", "about:blank"])
def test_allowed_urls(url):
    assert is_allowed_url(url, RECIPE)


@pytest.mark.parametrize("url", [
    "https://store.test/fr-fr/p/1",
    "https://evil.test/en-us/",
    "http://store.test/en-us/",
    "https://store.test.evil.test/en-us/",
    "javascript:alert(1)",
])
def test_rejected_urls(url):
    assert not is_allowed_url(url, RECIPE)


@pytest.mark.parametrize("label", ["Proceed to Checkout", "CHECKOUT", "Buy Now", "Sign In", "Place order", "Pay with PayPal"])
def test_forbidden_labels(label):
    assert is_forbidden_label(label)


@pytest.mark.parametrize("label", ["Add to Cart", "Add to Bag", "Size M", "Quantity"])
def test_allowed_labels(label):
    assert not is_forbidden_label(label)


def test_redact():
    assert redact("key=abc Bearer xyz", ["abc"]) == "key=*** Bearer ***"
    assert redact("nothing", ["", None]) == "nothing"
