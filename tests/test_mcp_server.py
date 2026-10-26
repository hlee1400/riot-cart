import json
from pathlib import Path

from mcp_server.__main__ import add_to_cart, handle_add_to_cart, mcp
from worker.models import CartRequest, Result, Status


class FakeRunner:
    name = "fake"

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def run(self, recipe_path, request):
        self.calls += 1
        return self.result


def _ok():
    r = Result(status=Status.CART_READY, message="added it")
    r.runner = "fake"
    return r


def test_cart_ready_logs_and_returns_summary(tmp_path):
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("x")
    log = tmp_path / "runs" / "runs.jsonl"
    out = handle_add_to_cart(CartRequest(product_name="Hat"), FakeRunner(_ok()), recipe, log)
    assert out["checkout_submitted"] is False
    assert out["summary"] == "added it"
    assert out["status"] == "CART_READY"
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert {"ts", "request", "result", "runner"} <= entry.keys()


def test_log_does_not_contain_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNAROUTE_API_KEY", "sekrit-key-123")
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("x")
    log = tmp_path / "runs.jsonl"
    res = Result(status=Status.CART_READY, message="leak sekrit-key-123")
    handle_add_to_cart(CartRequest(product_name="Hat"), FakeRunner(res), recipe, log)
    assert "sekrit-key-123" not in log.read_text()


def test_missing_recipe_fails(tmp_path):
    runner = FakeRunner(_ok())
    out = handle_add_to_cart(CartRequest(product_name="Hat"), runner, tmp_path / "nope.yaml", tmp_path / "l.jsonl")
    assert out["status"] == "FAILED"
    assert "no promoted recipe" in out["message"]
    assert runner.calls == 0


def test_invalid_quantity_returns_failed(monkeypatch):
    out = add_to_cart("Hat", None, 0)
    assert out["status"] == "FAILED"
    assert out["message"].startswith("invalid request")


def test_tool_registered():
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    # run in a worker thread: earlier playwright tests can leave a loop running on the main thread
    with ThreadPoolExecutor(1) as ex:
        tools = ex.submit(lambda: asyncio.run(mcp.list_tools())).result()
    assert [t.name for t in tools] == ["add_to_cart"]


class CrashingRunner:
    name = "crashy"

    def run(self, recipe_path, request):
        raise RuntimeError("runner exploded with sekrit-key-123")


def test_runner_crash_is_returned_and_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("LUNAROUTE_API_KEY", "sekrit-key-123")
    recipe = tmp_path / "recipe.yaml"
    recipe.write_text("x")
    log = tmp_path / "runs.jsonl"
    out = handle_add_to_cart(CartRequest(product_name="Hat"), CrashingRunner(), recipe, log)
    assert out["status"] == "FAILED" and "runner exploded" in out["summary"]
    lines = log.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["result"]["status"] == "FAILED" and entry["runner"] == "crashy"
    assert "sekrit-key-123" not in log.read_text() and "sekrit-key-123" not in out["summary"]
