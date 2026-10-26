"""Worker CLI. `run` prints exactly one JSON Result as the last stdout line; logs go to stderr."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from worker.models import CartRequest, Result, Status
from worker.safety import redact

RUN_TIMEOUT_S = 150
_emit_lock = threading.Lock()
_emitted = False


def emit(result: Result) -> None:
    global _emitted
    with _emit_lock:
        if _emitted:
            return
        _emitted = True
        line = redact(result.model_dump_json(), [os.environ.get("LUNAROUTE_API_KEY")])
        sys.stdout.write("\n" + line + "\n")
        sys.stdout.flush()


def run_once(args) -> Result:
    from worker.browser import install_guard, launch, new_context, set_policy
    from worker.flow import run_cart
    from worker.llm import llm_from_env
    from worker.recipe import load_recipe
    from worker.recovery import Recovery, RecoveryBudget

    recipe = load_recipe(args.recipe)
    request = CartRequest.model_validate_json(Path(args.request).read_text(encoding="utf-8"))
    recovery = Recovery(llm_from_env(), RecoveryBudget()) if request.recovery else None
    pw, browser = launch(headed=os.environ.get("HEADED") == "1")
    try:
        context = new_context(browser)
        install_guard(context, recipe)
        set_policy(recipe)
        page = context.new_page()
        result = run_cart(page, recipe, request, recovery)
        if result.status != Status.CART_READY and args.artifacts:
            try:
                out = Path(args.artifacts)
                out.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out / f"failed-{int(time.time())}.png"), full_page=True)
            except Exception as e:  # evidence is best-effort
                logging.warning("screenshot failed: %s", e)
        return result
    finally:
        browser.close()
        pw.stop()


def _cmd_run(args) -> int:
    start = time.monotonic()

    def finish(result: Result) -> None:
        result.runner = args.runner
        result.duration_ms = int((time.monotonic() - start) * 1000)
        emit(result)

    def on_timeout() -> None:
        finish(Result.fail(Status.FAILED, f"worker timeout after {RUN_TIMEOUT_S}s"))
        os._exit(0)

    timer = threading.Timer(RUN_TIMEOUT_S, on_timeout)
    timer.daemon = True
    timer.start()
    try:
        result = run_once(args)
    except Exception as e:  # never let a traceback replace the result line
        logging.exception("worker crashed")
        result = Result.fail(Status.FAILED, f"worker error: {type(e).__name__}: {e}")
    finally:
        timer.cancel()
    finish(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="worker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="add one product to a fresh cart and verify it")
    run.add_argument("--recipe", required=True)
    run.add_argument("--request", required=True)
    run.add_argument("--artifacts")
    run.add_argument("--runner", default="local")
    disc = sub.add_parser("discover", help="have LunaRoute label a store page and propose recipe locators")
    disc.add_argument("--url", required=True)
    disc.add_argument("--kind", required=True, choices=["landing", "search", "product", "cart"])
    disc.add_argument("--out", required=True)
    disc.add_argument("--search-term")
    disc.add_argument("--explore-cart", action="store_true")
    disc.add_argument("--origin", default="https://merch.riotgames.com")
    disc.add_argument("--prefix", default="/en-us/")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        return _cmd_run(args)
    if args.cmd == "discover":
        return _cmd_discover(args)
    return 2


def _cmd_discover(args) -> int:
    from worker.browser import install_guard, launch, new_context, set_policy
    from worker.discover import riot_policy, run_discovery
    from worker.llm import llm_from_env

    llm = llm_from_env()
    if llm is None:
        print(json.dumps({"error": "LUNAROUTE_API_KEY not set"}))
        return 1
    policy = riot_policy(args.origin, args.prefix)
    pw, browser = launch(headed=os.environ.get("HEADED") == "1")
    try:
        context = new_context(browser)
        blocked = install_guard(context, policy)
        set_policy(policy)
        summary = run_discovery(context.new_page(), llm, policy, args.url, args.kind, Path(args.out),
                                search_term=args.search_term, explore_cart=args.explore_cart)
        summary["blocked_navigations"] = blocked
    except Exception as e:
        logging.exception("discovery failed")
        summary = {"error": redact(f"{type(e).__name__}: {e}", [os.environ.get("LUNAROUTE_API_KEY")])[:500]}
    finally:
        browser.close()
        pw.stop()
    print(json.dumps(summary, indent=2))
    return 0 if "error" not in summary else 1


if __name__ == "__main__":
    sys.exit(main())
