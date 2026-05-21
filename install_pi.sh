#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/haggy/skywatcher"
ARCHIVE="/home/haggy/skywatcher-app.tar.gz"
SERVICE_NAME="skywatcher.service"

mkdir -p "$APP_DIR"
tar -xzf "$ARCHIVE" -C "$APP_DIR"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

sudo cp "$APP_DIR/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
