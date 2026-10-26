import json
import subprocess
import sys
from pathlib import Path

from mcp_server import runners
from mcp_server.runners import DockerRunner, SpriteRunner, execute, parse_worker_output
from worker.models import CartRequest, Result, Status

OK = Result(status=Status.CART_READY, message="ok").model_dump_json()


def test_parse_noisy_stdout():
    r = parse_worker_output(f"warning: blah\n{OK}\n\n", "", 0, "docker")
    assert r.status == Status.CART_READY and r.runner == "docker"


def test_parse_garbage_redacts(monkeypatch):
    monkeypatch.setenv("LUNAROUTE_API_KEY", "sk-secret")
    r = parse_worker_output("Traceback...\n", "boom sk-secret", 1, "local")
    assert r.status == Status.FAILED
    assert "exit 1" in r.message and "sk-secret" not in r.message


def test_execute_timeout():
    r = execute([sys.executable, "-c", "import time; time.sleep(5)"], "local", timeout_s=1)
    assert r.status == Status.FAILED and "timed out after 1s" in r.message


def test_execute_parses_result():
    code = f"print('noise'); print({OK!r})"
    r = execute([sys.executable, "-c", code], "local")
    assert r.status == Status.CART_READY and r.runner == "local"


def test_docker_command(monkeypatch):
    monkeypatch.delenv("HEADED", raising=False)
    cmd = DockerRunner().build_command(Path("/r/recipe.yaml"), Path("/r/req.json"), Path("/r/runs"))
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "LUNAROUTE_API_KEY" in cmd and not any("LUNAROUTE_API_KEY=" in c for c in cmd)
    assert f"{Path('/r/recipe.yaml')}:/in/recipe.yaml:ro" in cmd
    assert cmd[cmd.index("riot-cart-worker") + 1:] == [
        "python", "-m", "worker", "run", "--recipe", "/in/recipe.yaml", "--request", "/in/request.json",
        "--artifacts", "/out", "--runner", "docker"]


def test_docker_headed_uses_xvfb(monkeypatch):
    monkeypatch.setenv("HEADED", "1")
    cmd = DockerRunner().build_command(Path("a"), Path("b"), Path("c"))
    i = cmd.index("riot-cart-worker")
    assert cmd[i + 1:i + 3] == ["xvfb-run", "-a"]


def test_sprite_exec_command(monkeypatch):
    monkeypatch.delenv("HEADED", raising=False)
    s = SpriteRunner(sprite="sprite")
    cmd = s.exec_command("k123")
    assert cmd[:4] == ["sprite", "exec", "-s", "riot-store"]
    assert cmd[cmd.index("--dir") + 1] == "/home/sprite/app"
    assert "--env" in cmd and any(c.startswith("LUNAROUTE_API_KEY=k123") for c in cmd)
    # --file sources must be relative: the CLI splits SRC:DEST on the first ':' (breaks on C:\...)
    files = [cmd[i + 1] for i, c in enumerate(cmd) if c == "--file"]
    assert files == ["recipe.yaml:/home/sprite/run/recipe.yaml", "request.json:/home/sprite/run/request.json"]
    assert cmd[cmd.index("--") + 1:] == [
        "/home/sprite/app/.venv/bin/python", "-m", "worker", "run", "--recipe", "/home/sprite/run/recipe.yaml",
        "--request", "/home/sprite/run/request.json", "--runner", "sprite"]
    assert s.restore_command("v7") == ["sprite", "restore", "v7", "-s", "riot-store"]


def test_sprite_exec_without_key_and_headed(monkeypatch):
    monkeypatch.setenv("HEADED", "1")
    cmd = SpriteRunner(sprite="sprite").exec_command(None)
    assert cmd[cmd.index("--env") + 1] == "HEADED=1"
    assert cmd[cmd.index("--") + 1:][:2] == ["xvfb-run", "-a"]


def test_sprite_run_stages_files_and_execs_from_stage(monkeypatch, tmp_path):
    state = tmp_path / "sprite.json"
    state.write_text(json.dumps({"checkpoint": "v4"}))
    recipe = tmp_path / "r.yaml"
    recipe.write_text("recipe: 1")
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("cwd")))
        if cmd[1] == "exec":
            stage = Path(kw["cwd"])
            assert (stage / "recipe.yaml").read_text() == "recipe: 1"
            assert json.loads((stage / "request.json").read_text())["product_name"] == "x"
            return subprocess.CompletedProcess(cmd, 0, stdout=OK + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="Restored", stderr="")

    monkeypatch.setattr(runners.subprocess, "run", fake_run)
    r = SpriteRunner(sprite="sprite", state_path=state).run(recipe, CartRequest(product_name="x"))
    assert r.status == Status.CART_READY and r.runner == "sprite"
    assert calls[0][0] == ["sprite", "restore", "v4", "-s", "riot-store"]
    assert calls[1][0][1] == "exec" and not Path(calls[1][1]).exists()  # staging dir cleaned up


def test_sprite_restore_failure_redacted(monkeypatch, tmp_path):
    state = tmp_path / "sprite.json"
    state.write_text(json.dumps({"checkpoint": "v1"}))
    monkeypatch.setenv("LUNAROUTE_API_KEY", "sk-zzz")

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="restore failed sk-zzz")

    monkeypatch.setattr(runners.subprocess, "run", fake_run)
    r = SpriteRunner(sprite="sprite", state_path=state).run(tmp_path / "r.yaml", CartRequest(product_name="x"))
    assert r.status == Status.FAILED and "restore" in r.message and "sk-zzz" not in r.message


def test_sprite_missing_setup(tmp_path):
    r = SpriteRunner(sprite="sprite", state_path=tmp_path / "none.json").run(
        tmp_path / "r.yaml", CartRequest(product_name="x"))
    assert r.status == Status.FAILED and "sprite_setup" in r.message


def test_runner_from_env(monkeypatch):
    monkeypatch.setenv("RUNNER", "docker")
    assert runners.runner_from_env().name == "docker"
    monkeypatch.setenv("RUNNER", "sprite")
    assert runners.runner_from_env().name == "sprite"
    monkeypatch.delenv("RUNNER")
    assert runners.runner_from_env().name == "local"
