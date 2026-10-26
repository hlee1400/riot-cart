import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from worker.models import CartRequest, Result, Status

ROOT = Path(__file__).resolve().parent.parent


def test_result_never_submits_checkout():
    with pytest.raises(ValidationError):
        Result(status="FAILED", message="x", checkout_submitted=True)


def test_cart_request_quantity_bounds():
    with pytest.raises(ValidationError):
        CartRequest(product_name="Poro Pin", quantity=0)
    with pytest.raises(ValidationError):
        CartRequest(product_name="Poro Pin", quantity=11)
    assert CartRequest(product_name="Poro Pin").options == {}


def test_cart_request_rejects_blank_name():
    with pytest.raises(ValidationError):
        CartRequest(product_name="   ")


def test_schema_file_matches_model():
    on_disk = json.loads((ROOT / "contracts" / "result.schema.json").read_text())
    assert on_disk == Result.model_json_schema()


def test_fail_helper():
    r = Result.fail(Status.PRODUCT_NOT_FOUND, "no", step="search", candidates=["A"])
    assert r.cart is None
    assert r.failed_step == "search"
    assert r.candidates == ["A"]
    assert r.status == "PRODUCT_NOT_FOUND"
