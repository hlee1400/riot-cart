"""riot-cart MCP server (stdio). stdout is the protocol channel: log to stderr only."""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from mcp_server.runners import ROOT, RUNS_DIR, Runner, runner_from_env
from worker.models import CartRequest, Result, Status
from worker.safety import redact

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("mcp_server")

RECIPE_PATH = ROOT / "retailer-recipes" / "riot_games" / "recipe.yaml"
LOG_PATH = RUNS_DIR / "runs.jsonl"

mcp = FastMCP("riot-cart")


def _append_log(log_path: Path, request: dict, result: dict, runner: str) -> None:
    line = json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "request": request,
                       "result": result, "runner": runner}, default=str)
    line = redact(line, [os.environ.get("LUNAROUTE_API_KEY")])
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        log.warning("could not write run log: %s", e)


def handle_add_to_cart(req: CartRequest, runner: Runner, recipe_path: Path, log_path: Path) -> dict:
    if not Path(recipe_path).exists():
        result = Result.fail(Status.FAILED,
                             "no promoted recipe; run the riot-discovery skill and promote the draft")
    else:
        try:
            result = runner.run(recipe_path, req)
        except Exception as e:  # a crashed runner is still a run: return FAILED and log it
            log.exception("runner crashed")
            result = Result.fail(Status.FAILED, redact(f"internal error: {e}", [os.environ.get("LUNAROUTE_API_KEY")]))
    out = result.model_dump(mode="json") | {"summary": result.message}
    _append_log(log_path, req.model_dump(mode="json"), out, runner.name)
    return out


@mcp.tool()
def add_to_cart(product_name: str, options: dict[str, str] | None = None, quantity: int = 1) -> dict:
    """Add an exact Riot Games merch product to a fresh guest cart and verify it. Pass the exact product name from the user's message; pass only options the user actually mentioned (e.g. {'size': 'medium', 'color': 'black'}) — unmentioned ones default to the first available choice. Never checks out. Usually takes 20–150 s; at most ~200 s with the local/Docker runners and ~320 s with the Sprite runner (restore + run). On AMBIGUOUS_MATCH, PRODUCT_NOT_FOUND or VARIANT_UNAVAILABLE, show the user the candidates and ask before retrying."""
    try:
        req = CartRequest(product_name=product_name, options=options or {}, quantity=quantity)
    except ValidationError as e:
        msg = redact(f"invalid request: {e}", [os.environ.get("LUNAROUTE_API_KEY")])
        return Result.fail(Status.FAILED, msg).model_dump(mode="json") | {"summary": msg}
    try:
        return handle_add_to_cart(req, runner_from_env(), RECIPE_PATH, LOG_PATH)
    except Exception as e:  # never let an exception escape the tool
        log.exception("add_to_cart crashed")
        msg = redact(f"internal error: {e}", [os.environ.get("LUNAROUTE_API_KEY")])
        return Result.fail(Status.FAILED, msg).model_dump(mode="json") | {"summary": msg}


if __name__ == "__main__":
    mcp.run(transport="stdio")
