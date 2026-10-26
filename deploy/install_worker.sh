#!/usr/bin/env bash
# Install the Riot cart worker into APP_DIR/.venv (idempotent).
set -euo pipefail

APP_DIR="${1:?usage: install_worker.sh APP_DIR}"
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then SUDO="sudo"; fi

if ! command -v xvfb-run >/dev/null 2>&1; then
  $SUDO apt-get update
  $SUDO apt-get install -y --no-install-recommends xvfb xauth
fi

if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install "$APP_DIR"
# apt deps need root; the browser itself must install as the invoking user (per-user cache)
$SUDO "$APP_DIR/.venv/bin/playwright" install-deps chromium
"$APP_DIR/.venv/bin/playwright" install chromium
