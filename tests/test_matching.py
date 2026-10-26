from worker.matching import (
    match_option,
    match_title,
    normalize,
    parse_price,
    parse_quantity,
    strip_availability,
)
from worker.models import Price


def test_normalize_symbols_dashes_space():
    assert normalize("  Arcane  Jinx Hoodie™ — Black® ") == "arcane jinx hoodie - black"


def test_match_title_trademark():
    assert match_title("Arcane Jinx Hoodie", ["Arcane Jinx Hoodie™", "Jinx Tee"]) == ("one", [0])


def test_match_title_dash_variants():
    assert match_title("Arcane Jinx Hoodie — Black", ["arcane jinx hoodie - black"]) == ("one", [0])


def test_match_title_many():
    assert match_title("Poro Pin", ["Poro Pin", "Poro  Pin"]) == ("many", [0, 1])


def test_match_title_none_ranks_by_overlap():
    kind, idx = match_title("Jinx Hoodie", ["Teemo Mug", "Arcane Jinx Hoodie", "Jinx Tee"])
    assert kind == "none"
    assert idx[:2] == [1, 2]
    assert 0 not in idx


def test_strip_availability():
    assert strip_availability("M - Sold Out") == ("M", True)
    assert strip_availability("Black (Out of stock)") == ("Black", True)
    assert strip_availability("Blue") == ("Blue", False)


def test_match_option_alias_and_suffix():
    assert match_option("medium", ["S", "M - Sold Out", "L"]) == 1
    assert match_option("Black", ["Black (Out of stock)"]) == 0
    assert match_option("xl", ["S", "Extra Large"]) == 1
    assert match_option("XL", ["S", "M"]) is None


def test_parse_price():
    assert parse_price("$1,064.99") == Price(amount="1064.99", currency="USD")
    assert parse_price("64.99 USD") == Price(amount="64.99", currency="USD")
    assert parse_price("free") is None


def test_parse_quantity():
    assert parse_quantity("Qty: 2") == 2
    assert parse_quantity("3") == 3
    assert parse_quantity("") is None
