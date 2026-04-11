#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="humblevoice.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

if systemctl list-unit-files | grep -q "^${SERVICE_NAME}"; then
  sudo systemctl stop "${SERVICE_NAME}" || true
  sudo systemctl disable "${SERVICE_NAME}" || true
fi

if [[ -f "${SERVICE_PATH}" ]]; then
  sudo rm -f "${SERVICE_PATH}"
fi

sudo systemctl daemon-reload
sudo systemctl reset-failed

echo "Uninstalled ${SERVICE_NAME}"
