#!/usr/bin/env bash
# Pull the latest code, sync dependencies and restart. Run as root inside the LXC.
#   bash /opt/mealie-mcp/app/deploy/update.sh [branch]
set -euo pipefail

APP_USER="mealie-mcp"
BASE_DIR="/opt/mealie-mcp"
APP_DIR="${BASE_DIR}/app"
ENV_FILE="/etc/mealie-mcp/mealie-mcp.env"

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

as_app() {
  runuser -u "$APP_USER" -- env \
    HOME="$BASE_DIR" \
    UV_CACHE_DIR="${BASE_DIR}/.cache/uv" \
    UV_PYTHON_INSTALL_DIR="${BASE_DIR}/python" \
    "$@"
}

# Git as root (root's deploy key works for a private repo), then hand files back
rgit() { git -c safe.directory="$APP_DIR" -C "$APP_DIR" "$@"; }
REF="${1:-$(rgit rev-parse --abbrev-ref HEAD)}"
BEFORE="$(rgit rev-parse --short HEAD)"
rgit fetch --quiet origin "$REF"
rgit merge --ff-only --quiet FETCH_HEAD
AFTER="$(rgit rev-parse --short HEAD)"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
echo "Code: ${BEFORE} -> ${AFTER}"

as_app bash -c "cd '$APP_DIR' && uv sync --frozen --no-dev --python 3.12"
install -m 0644 "${APP_DIR}/deploy/mealie-mcp.service" /etc/systemd/system/mealie-mcp.service
systemctl daemon-reload
systemctl restart mealie-mcp

PORT="$(grep -E '^MCP_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2 || true)"
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS "http://127.0.0.1:${PORT:-8000}/healthz" >/dev/null 2>&1; then
    echo "Healthy."
    exit 0
  fi
  sleep 2
done
echo "No answer from /healthz after 20s. journalctl -u mealie-mcp -n 50" >&2
exit 1
