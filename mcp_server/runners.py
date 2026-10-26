"""Runners execute the worker (locally, in Docker, or on the riot-store Sprite) and parse its JSON result."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from worker.models import CartRequest, Result, Status
from worker.safety import redact

log = logging.getLogger("mcp_server.runners")

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"
WORKER_TIMEOUT_S = 200
SPRITE_HOME = "/home/sprite"  # verified via `echo $HOME` on riot-store (see deploy/sprite.json "home")
SPRITE_APP = f"{SPRITE_HOME}/app"
# Per-run inputs live on the overlay filesystem (wiped by `sprite restore`), not /tmp: on Sprites /tmp is a separate
# ext4 disk (/dev/vdb) that checkpoints do NOT capture, so files there survive a restore.
SPRITE_RUN = f"{SPRITE_HOME}/run"


def _secrets() -> list[str | None]:
    return [os.environ.get("LUNAROUTE_API_KEY")]


def parse_worker_output(stdout: str, stderr: str, returncode: int, runner: str) -> Result:
    for line in reversed([l for l in stdout.splitlines() if l.strip()]):
        try:
            result = Result.model_validate_json(line)
        except ValidationError:
            continue
        result.runner = runner
        return result
    tail = redact((stderr or stdout or "")[-500:], _secrets())
    r = Result.fail(Status.FAILED, f"worker produced no result (exit {returncode}): {tail}")
    r.runner = runner
    return r


def execute(cmd: list[str], runner: str, timeout_s: int = WORKER_TIMEOUT_S, env: dict | None = None,
            cwd: Path | None = None) -> Result:
    log.info("exec %s", redact(" ".join(cmd), _secrets()))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout_s, env=env, cwd=cwd)
    except subprocess.TimeoutExpired:
        r = Result.fail(Status.FAILED, f"timed out after {timeout_s}s")
        r.runner = runner
        return r
    except OSError as e:
        r = Result.fail(Status.FAILED, f"could not start {cmd[0]}: {e}")
        r.runner = runner
        return r
    return parse_worker_output(p.stdout, p.stderr, p.returncode, runner)


class Runner(Protocol):
    name: str

    def run(self, recipe_path: Path, request: CartRequest) -> Result: ...


def _request_file(request: CartRequest) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    f.write(request.model_dump_json())
    f.close()
    return Path(f.name)


class LocalRunner:
    name = "local"

    def run(self, recipe_path: Path, request: CartRequest) -> Result:
        req = _request_file(request)
        try:
            return execute([sys.executable, "-m", "worker", "run", "--recipe", str(recipe_path), "--request", str(req),
                            "--artifacts", str(RUNS_DIR), "--runner", "local"], "local")
        finally:
            req.unlink(missing_ok=True)


class DockerRunner:
    name = "docker"

    def __init__(self, image: str = "riot-cart-worker"):
        self.image = image

    def build_command(self, recipe_path: Path, request_path: Path, runs_dir: Path) -> list[str]:
        headed = os.environ.get("HEADED") == "1"
        worker = ["python", "-m", "worker", "run", "--recipe", "/in/recipe.yaml", "--request", "/in/request.json",
                  "--artifacts", "/out", "--runner", "docker"]
        return ["docker", "run", "--rm", "-e", "LUNAROUTE_API_KEY", "-e", "HEADED",
                "-v", f"{recipe_path}:/in/recipe.yaml:ro", "-v", f"{request_path}:/in/request.json:ro",
                "-v", f"{runs_dir}:/out", self.image] + (["xvfb-run", "-a"] if headed else []) + worker

    def run(self, recipe_path: Path, request: CartRequest) -> Result:
        RUNS_DIR.mkdir(exist_ok=True)
        req = _request_file(request)
        try:
            return execute(self.build_command(Path(recipe_path).resolve(), req, RUNS_DIR), "docker")
        finally:
            req.unlink(missing_ok=True)


def sprite_cmd() -> str:
    env = os.environ.get("SPRITE_CMD")
    if env:
        return env
    found = shutil.which("sprite")
    if found:
        return found
    win = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Sprite" / "sprite.exe"
    return str(win) if win.exists() else "sprite"


_sprite_lock = threading.Lock()


class SpriteRunner:
    """Restore the clean checkpoint, then exec the worker on the Sprite.

    sprite CLI shapes (verified against sprite.exe help + live runs, 2026-10-21):
      `sprite checkpoint create -s NAME --comment C` prints
          "✓ Checkpoint v4 created" / "  Restore with: sprite restore v4"
      `sprite checkpoint list -s NAME` prints a fixed-width table (ID, CREATED "YYYY-MM-DD HH:MM:SS", COMMENT) whose
          first row is "→ Current (active)"; ids look like v1, v2, ... (auto ones are hidden without --include-auto).
      `sprite restore ID -s NAME` prints "✓ Restored to checkpoint v4", exits 0, and returns once the overlay is
          replaced (an exec straight after sees the restored filesystem). It also auto-creates a
          "pre-restore <timestamp>" checkpoint every time, so checkpoints accumulate one per run.
      `sprite exec --file SRC:DEST` splits on the first ':' so a Windows drive-letter SRC breaks; SRC must be a relative
          path, hence the per-run staging directory used as cwd.
    """
    name = "sprite"

    def __init__(self, name: str | None = None, state_path: Path = ROOT / "deploy" / "sprite.json",
                 sprite: str | None = None):
        self.sprite_name = name or os.environ.get("SPRITE_NAME", "riot-store")
        self.state_path = state_path
        self.sprite = sprite or sprite_cmd()

    def restore_command(self, checkpoint: str) -> list[str]:
        return [self.sprite, "restore", checkpoint, "-s", self.sprite_name]

    def exec_command(self, api_key: str | None) -> list[str]:
        """Command to run from a staging dir holding recipe.yaml and request.json (relative --file sources)."""
        env = f"HEADED={os.environ.get('HEADED', '0')}"
        if api_key:
            env = f"LUNAROUTE_API_KEY={api_key},{env}"
        python = f"{SPRITE_APP}/.venv/bin/python"
        worker = [python, "-m", "worker", "run", "--recipe", f"{SPRITE_RUN}/recipe.yaml",
                  "--request", f"{SPRITE_RUN}/request.json", "--runner", "sprite"]
        if os.environ.get("HEADED") == "1":
            worker = ["xvfb-run", "-a"] + worker
        return [self.sprite, "exec", "-s", self.sprite_name, "--dir", SPRITE_APP, "--env", env,
                "--file", f"recipe.yaml:{SPRITE_RUN}/recipe.yaml", "--file", f"request.json:{SPRITE_RUN}/request.json",
                "--"] + worker

    def run(self, recipe_path: Path, request: CartRequest) -> Result:
        try:
            checkpoint = json.loads(self.state_path.read_text())["checkpoint"]
        except (OSError, ValueError, KeyError):
            r = Result.fail(Status.FAILED, "Sprite not prepared: run `uv run python deploy/sprite_setup.py` first")
            r.runner = "sprite"
            return r
        with _sprite_lock:
            try:
                p = subprocess.run(self.restore_command(checkpoint), capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=120)
                ok = p.returncode == 0
                err = p.stderr or p.stdout
            except (subprocess.TimeoutExpired, OSError) as e:
                ok, err = False, str(e)
            if not ok:
                r = Result.fail(Status.FAILED, f"sprite restore failed: {redact(err[-400:], _secrets())}")
                r.runner = "sprite"
                return r
            with tempfile.TemporaryDirectory() as stage:
                stage_dir = Path(stage)
                shutil.copyfile(recipe_path, stage_dir / "recipe.yaml")
                (stage_dir / "request.json").write_text(request.model_dump_json(), encoding="utf-8")
                return execute(self.exec_command(os.environ.get("LUNAROUTE_API_KEY")), "sprite", cwd=stage_dir)


def runner_from_env() -> Runner:
    kind = os.environ.get("RUNNER", "local").lower()
    if kind == "docker":
        return DockerRunner()
    if kind == "sprite":
        return SpriteRunner()
    return LocalRunner()
