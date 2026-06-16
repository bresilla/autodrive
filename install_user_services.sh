#!/usr/bin/env bash
set -euo pipefail

SERVICE_DIR="${HOME}/.config/systemd/user"

mkdir -p "${SERVICE_DIR}"
cp autodrive-canlog.service autodrive-api.service "${SERVICE_DIR}/"

systemctl --user daemon-reload
systemctl --user enable --now autodrive-canlog.service
systemctl --user enable --now autodrive-api.service

echo "Installed and started:"
echo "  autodrive-canlog.service -> /home/oxbo/data/can"
echo "  autodrive-api.service    -> http://0.0.0.0:8080"
echo
echo "Check status with:"
echo "  systemctl --user status autodrive-canlog.service"
echo "  systemctl --user status autodrive-api.service"
