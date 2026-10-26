import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("sprite_setup", ROOT / "deploy" / "sprite_setup.py")
sprite_setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sprite_setup)

LISTING = """ID                             CREATED             COMMENT
------------------------------ ------------------- --------------------------------------------------------------------------------
\u2192 Current (active)             2026-10-21 17:12:23 
v6                             2026-10-21 17:20:00 clean-20261021T171210Z
v5                             2026-10-21 17:12:23 pre-restore 2026-10-21 17:12:23.82157869 +0000 UTC m=+5075.2
v4                             2026-10-21 17:12:11 clean-20261021T171210Z
v3 (auto)                      2026-10-18 20:44:31 auto:shutdown
v1                             2026-10-18 19:22:42 initial checkpoint
"""


def test_parse_checkpoint_list_picks_newest_matching():
    assert sprite_setup.parse_checkpoint_list(LISTING, "clean-20261021T171210Z") == "v6"
    assert sprite_setup.parse_checkpoint_list(LISTING, "initial checkpoint") == "v1"
    assert sprite_setup.parse_checkpoint_list(LISTING, "clean-nope") is None


def test_parse_smoke_takes_last_json_line():
    out = 'noise\n{"headed": false, "url": "https://x/", "blocker": null}\n'
    assert sprite_setup.parse_smoke(out) == {"headed": False, "url": "https://x/", "blocker": None}
    assert "error" in sprite_setup.parse_smoke("Traceback: boom")


def test_smoke_command_headed_uses_xvfb():
    assert sprite_setup.smoke_command(False) == ["/home/sprite/app/.venv/bin/python", "/tmp/smoke.py"]
    assert sprite_setup.smoke_command(True)[:2] == ["xvfb-run", "-a"]
    assert sprite_setup.smoke_command(True)[-1] == "--headed"


def test_archive_ships_both_packages_and_never_secrets():
    paths = sprite_setup.ARCHIVE_PATHS
    assert {"pyproject.toml", "worker", "mcp_server", "deploy/install_worker.sh"} <= set(paths)
    assert not any(".env" in p for p in paths)
    script = sprite_setup.install_script()
    assert "install_worker.sh /home/sprite/app" in script and "LUNAROUTE" not in script


def test_build_state_shape():
    st = sprite_setup.build_state("riot-store", "v4", "/home/sprite", "20261021T171210Z", [{"headed": False}])
    assert set(st) == {"name", "checkpoint", "home", "created", "smoke"}
