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
if [[ -f "$APP_DIR/skywatcher-media-sync.service" ]]; then
  sudo cp "$APP_DIR/skywatcher-media-sync.service" /etc/systemd/system/skywatcher-media-sync.service
  sudo cp "$APP_DIR/skywatcher-media-sync.timer" /etc/systemd/system/skywatcher-media-sync.timer
fi
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
if [[ -f /etc/systemd/system/skywatcher-media-sync.timer ]]; then
  sudo systemctl enable --now skywatcher-media-sync.timer
fi
