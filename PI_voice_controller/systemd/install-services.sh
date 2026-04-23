#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${1:-/home/cs5272smarthome/cs5272-smart-home}"
SYSTEMD_DIR="$REPO_ROOT/PI_voice_controller/systemd"

echo "Linking services from: $SYSTEMD_DIR"
sudo systemctl link "$SYSTEMD_DIR/tinyllama-server.service"
sudo systemctl link "$SYSTEMD_DIR/voice-controller.service"
sudo systemctl daemon-reload
sudo systemctl enable tinyllama-server.service voice-controller.service
sudo systemctl restart tinyllama-server.service voice-controller.service

echo "Done. Check status with:"
echo "  systemctl status tinyllama-server.service"
echo "  systemctl status voice-controller.service"
