"""Prepare the existing `riot-store` Sprite: upload the worker, install it, smoke-test Chromium against Riot,
checkpoint the clean state and record it in deploy/sprite.json. Never creates a Sprite; never passes the API key.

Usage: uv run python deploy/sprite_setup.py [--name riot-store]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp_server.runners import SPRITE_APP, sprite_cmd  # noqa: E402

STATE_PATH = ROOT / "deploy" / "sprite.json"
RECIPE = "retailer-recipes/riot_games/recipe.yaml"
# The wheel packages both worker/ and mcp_server/, so both must ship with pyproject.toml.
ARCHIVE_PATHS = ["pyproject.toml", "worker", "mcp_server", "deploy/install_worker.sh", RECIPE]
SMOKE_URL = "https://merch.riotgames.com/en-us/"

SMOKE_PY = f'''
import json, sys
from pathlib import Path
from worker.browser import launch, new_context, detect_blocker
from worker.recipe import load_recipe
headed = "--headed" in sys.argv
recipe = load_recipe(Path("{SPRITE_APP}/{RECIPE}"))
pw, browser = launch(headed=headed)
out = {{"headed": headed}}
try:
    page = new_context(browser).new_page()
    page.goto("{SMOKE_URL}", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    blocker = detect_blocker(page, recipe)
    out.update(url=page.url, title=page.title(), blocker=blocker.value if blocker else None)
except Exception as e:
    out["error"] = f"{{type(e).__name__}}: {{e}}"[:300]
finally:
    browser.close(); pw.stop()
print(json.dumps(out))
'''


def install_script() -> str:
    # Replace code dirs (keep .venv) so a re-run never mixes old and new modules.
    return (f"set -e; mkdir -p {SPRITE_APP} && rm -rf {SPRITE_APP}/worker {SPRITE_APP}/mcp_server "
            f"&& tar xzf /tmp/app.tgz -C {SPRITE_APP} && bash {SPRITE_APP}/deploy/install_worker.sh {SPRITE_APP}")


def smoke_command(headed: bool) -> list[str]:
    py = [f"{SPRITE_APP}/.venv/bin/python", "/tmp/smoke.py"]
    return ["xvfb-run", "-a"] + py + ["--headed"] if headed else py


def parse_checkpoint_list(text: str, comment: str) -> str | None:
    """Return the newest checkpoint id whose comment equals `comment`.

    `sprite checkpoint list` prints a fixed-width table:
        ID                             CREATED             COMMENT
        ------------------------------ ------------------- ----------
        -> Current (active)            2026-10-18 20:44:31
        v1                             2026-10-18 19:22:42 initial checkpoint
    (the active row starts with a unicode arrow and is never a restorable id).
    """
    best: tuple[str, str] | None = None
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or parts[0] in ("ID",) or parts[0].startswith("-") or "Current" in line:
            continue
        cid, day, clock, cmt = parts
        if cmt.strip() != comment:
            continue
        created = f"{day} {clock}"
        if best is None or created > best[1]:
            best = (cid, created)
    return best[0] if best else None


def parse_smoke(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {"error": f"no smoke output: {stdout[-300:]}"}


def build_state(name: str, checkpoint: str, home: str, created: str, smoke: list[dict]) -> dict:
    return {"name": name, "checkpoint": checkpoint, "home": home, "created": created, "smoke": smoke}


class Sprite:
    def __init__(self, name: str):
        self.name = name
        self.bin = sprite_cmd()

    def run(self, args: list[str], timeout: int = 1800, cwd: Path | None = None, check: bool = True,
            echo: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.bin] + args
        print(f"$ sprite {' '.join(args)}", flush=True)
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=cwd)
        if echo:
            if p.stdout.strip():
                print(p.stdout.rstrip()[-4000:], flush=True)
            if p.stderr.strip():
                print(p.stderr.rstrip()[-4000:], file=sys.stderr, flush=True)
        if check and p.returncode != 0:
            raise SystemExit(f"sprite {args[0]} failed (exit {p.returncode})")
        return p

    def exec(self, command: list[str], files: dict[str, str] | None = None, cwd: Path | None = None,
             **kw) -> subprocess.CompletedProcess:
        args = ["exec", "-s", self.name]
        for local, remote in (files or {}).items():
            args += ["--file", f"{local}:{remote}"]
        return self.run(args + ["--"] + command, cwd=cwd, **kw)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="riot-store")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252; apt/pip output is UTF-8
        stream.reconfigure(encoding="utf-8", errors="replace")
    sp = Sprite(args.name)

    listed = sp.run(["list", "--prefix", args.name], timeout=60, echo=False).stdout.split()
    if args.name not in listed:
        raise SystemExit(f"Sprite {args.name!r} not found in `sprite list`; create it yourself first "
                         f"(this script never creates Sprites).")

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        subprocess.run(["git", "archive", "--format=tar.gz", "-o", str(tmpd / "app.tgz"), "HEAD"] + ARCHIVE_PATHS,
                       cwd=ROOT, check=True)
        (tmpd / "smoke.py").write_text(SMOKE_PY, encoding="utf-8")
        # --file paths are relative to cwd: the CLI splits source:dest on the first ':' (breaks on C:\...).
        sp.exec(["bash", "-c", install_script()], files={"app.tgz": "/tmp/app.tgz"}, cwd=tmpd)
        home = sp.exec(["bash", "-c", "echo $HOME"], echo=False).stdout.strip().splitlines()[-1]
        smoke = []
        for headed in (False, True):
            p = sp.exec(smoke_command(headed), files={"smoke.py": "/tmp/smoke.py"}, cwd=tmpd, timeout=180,
                        check=False)
            result = parse_smoke(p.stdout)
            print(f"smoke {'headed' if headed else 'headless'}: {json.dumps(result)}", flush=True)
            smoke.append(result)
        sp.exec(["rm", "-f", "/tmp/app.tgz", "/tmp/smoke.py"], echo=False)
    if all("error" in s for s in smoke):
        raise SystemExit("smoke failed in both modes; not checkpointing")

    created = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    comment = f"clean-{created}"
    sp.run(["checkpoint", "create", "-s", args.name, "--comment", comment], timeout=600)
    listing = sp.run(["checkpoint", "list", "-s", args.name], timeout=60).stdout
    ckpt = parse_checkpoint_list(listing, comment)
    if not ckpt:
        raise SystemExit(f"checkpoint {comment!r} not found in `sprite checkpoint list`")
    state = build_state(args.name, ckpt, home, created, smoke)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {STATE_PATH}: checkpoint {ckpt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
