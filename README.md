# riot-cart (prototype)

Ask Claude Code to "add the MSI 2026 Jacket in medium to my Riot cart" and a Python Playwright worker adds that
exact product to a fresh **guest** cart on https://merch.riotgames.com/en-us/, verifies it on the cart page, and
returns one JSON `Result` (e.g. `CART_READY`). It never logs in, checks out, pays, or solves CAPTCHAs.

The worker follows a recipe (`retailer-recipes/riot_games/recipe.yaml`) and only calls LunaRoute (System One)
for recovery when the recipe misses. It runs in one of three places: locally, in Docker, or on a Fly.io Sprite.

## Setup

```bash
uv sync                                   # Python deps (incl. dev/pytest)
uv run playwright install chromium        # local runner browser
docker build -t riot-cart-worker -f worker/Dockerfile .   # docker runner image
uv run pytest -q                          # offline test suite
```

Set `LUNAROUTE_API_KEY` in your environment (never commit it; `.env` is git-ignored).

### Sprite (optional third runner)

1. Install the `sprite` CLI and authenticate (`sprite login`). The CLI is found via `$SPRITE_CMD`, then `PATH`, then
   `%LOCALAPPDATA%\Programs\Sprite\sprite.exe`.
2. Create the Sprite yourself once (`sprite create riot-store`) — the setup script never creates Sprites.
3. Commit your changes (the setup uploads `git archive HEAD`), then run:

   ```bash
   uv run python deploy/sprite_setup.py [--name riot-store]
   ```

   It uploads `pyproject.toml`, `worker/`, `mcp_server/`, `deploy/install_worker.sh` and the promoted recipe to
   `/home/sprite/app`, runs `install_worker.sh` (venv, Playwright Chromium, xvfb), smoke-tests Chromium against the
   Riot store headless and headed, creates a `clean-<UTC timestamp>` checkpoint and writes `deploy/sprite.json`
   (`name`, `checkpoint`, `home`, `created`, `smoke`). The API key is never sent during setup.

## Runners

Pick with `RUNNER=local|docker|sprite` (`HEADED=1` shows/uses a real headed browser; in Docker/Sprite it runs
under `xvfb-run`). The worker stops itself after 150 s: it prints a FAILED result and exits with `os._exit`, which
can leave a local Chromium process orphaned. Each runner parses the last JSON line of the worker's stdout and, as a
backstop, kills the process it started after 200 s. That process is the worker itself only for `local`; for `docker`
it is the `docker run` client, and for `sprite` it is the local `sprite exec` client, so a hung remote worker lingers
on the Sprite until the next run's `sprite restore` resets it.

| Runner | What it does |
|---|---|
| `local` | `python -m worker run ...` with the local venv. |
| `docker` | `docker run --rm riot-cart-worker ...`; the key is passed by name (`-e LUNAROUTE_API_KEY`). Failure screenshots land in `runs/`. |
| `sprite` | `sprite restore <checkpoint>` (clean state), then `sprite exec` the worker in `/home/sprite/app`, uploading the recipe and request to `/home/sprite/run/`. |

## Using it from Claude Code

`.mcp.json` registers the `riot-cart` MCP server (`uv run python -m mcp_server`) with `LUNAROUTE_API_KEY` and
`RUNNER` (default `docker`) taken from your environment:

```bash
RUNNER=sprite claude      # or RUNNER=local / docker
```

The `add_to_cart` tool usually takes 20–150 s; the worst case is ~200 s with `local`/`docker` and ~320 s with
`sprite` (restore up to 120 s + run up to 200 s). If Claude Code times out tool calls, raise the limit:
`MCP_TOOL_TIMEOUT=360000`. Each run is appended (key redacted) to `runs/runs.jsonl`.

## Discovery and recipe promotion

`python -m worker discover` explores the store with LunaRoute and writes candidate selectors and sanitized page
HTML (e.g. `uv run python -m worker discover --kind product --url <product url> --out discovery/<slug>`). Use it
when onboarding or when runs start needing recovery or failing, reconcile the output into
`retailer-recipes/riot_games/recipe.draft.yaml`, and after review promote it by copying it to `recipe.yaml` (then
rerun `deploy/sprite_setup.py` if you use the Sprite runner, since setup ships the promoted recipe for its smoke test).

## Known limitations

- **API key visible in local process args for Sprite exec**: `sprite exec --env LUNAROUTE_API_KEY=...` puts the key
  on the local command line (visible to other local processes, e.g. Task Manager). It is redacted in our logs and
  never written to the Sprite filesystem or checkpoint.
- **One Sprite run at a time**: the Sprite runner serializes runs (restore + exec) behind a lock; parallel requests
  wait.
- **Cart is discarded after the run**: the cart lives in a throwaway browser context; nothing persists to your own
  browser or Riot account. The result reports what was verified.
- `sprite restore` reverts the overlay filesystem only; the Sprite's `/tmp` is a separate disk that checkpoints do
  not capture, so per-run files go to `/home/sprite/run` instead.
- Every `sprite restore` auto-creates a `pre-restore ...` checkpoint, so checkpoints accumulate (one per Sprite run);
  prune them occasionally with `sprite checkpoint delete <id>`.
- The `sprite exec --file SRC:DEST` flag splits on the first `:`, so the runner stages files in a temp dir and passes
  relative sources.
