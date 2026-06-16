#!/usr/bin/env bash
set -euo pipefail

SERVICE_DIR="${HOME}/.config/systemd/user"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "${SERVICE_DIR}"

cat > "${SERVICE_DIR}/autodrive-canlog.service" <<EOF
[Unit]
Description=AutoDrive hourly CAN logger
After=default.target

[Service]
Type=simple
ExecStartPre=/usr/bin/mkdir -p /home/oxbo/data/can
ExecStart=${REPO_DIR}/hourly_can_logger.py /home/oxbo/data/can --can-bus can0 --flush-every 1
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF

cat > "${SERVICE_DIR}/autodrive-api.service" <<EOF
[Unit]
Description=AutoDrive REST API server
After=default.target

[Service]
Type=simple
ExecStart=${REPO_DIR}/api_server.py --host 0.0.0.0 --port 8080 --can-bus can0
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable autodrive-canlog.service
systemctl --user enable autodrive-api.service
systemctl --user restart autodrive-canlog.service
systemctl --user restart autodrive-api.service

echo "Installed and started:"
echo "  autodrive-canlog.service -> /home/oxbo/data/can"
echo "  autodrive-api.service    -> http://0.0.0.0:8080"
echo
echo "Check status with:"
echo "  systemctl --user status autodrive-canlog.service"
echo "  systemctl --user status autodrive-api.service"
