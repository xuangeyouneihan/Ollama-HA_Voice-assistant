#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="humblevoice.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="${SCRIPT_DIR}/${SERVICE_NAME}"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SERVICE_USER="${SUDO_USER:-${USER}}"
SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"
RENDERED_FILE="$(mktemp)"

if [[ ! -f "${SERVICE_FILE}" ]]; then
  echo "Service file not found: ${SERVICE_FILE}" >&2
  exit 1
fi

if [[ ! -x "${PROJECT_ROOT}/.venv/bin/python" ]]; then
  echo "Python executable not found: ${PROJECT_ROOT}/.venv/bin/python" >&2
  echo "Please create the virtual environment and install dependencies first." >&2
  exit 1
fi

cleanup() {
  rm -f "${RENDERED_FILE}"
}
trap cleanup EXIT

sed \
  -e "s|__SERVICE_USER__|${SERVICE_USER}|g" \
  -e "s|__SERVICE_GROUP__|${SERVICE_GROUP}|g" \
  -e "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g" \
  "${SERVICE_FILE}" > "${RENDERED_FILE}"

sudo cp "${RENDERED_FILE}" "/etc/systemd/system/${SERVICE_NAME}"
sudo systemctl daemon-reload
sudo systemctl enable --now "${SERVICE_NAME}"

echo "Installed and started ${SERVICE_NAME}"
sudo systemctl --no-pager --full status "${SERVICE_NAME}" | sed -n '1,20p'
