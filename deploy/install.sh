#!/usr/bin/env bash
# Install the Mealie MCP server in a Debian 12/13 Proxmox LXC, without Docker.
#
# Run as root inside the container, from a checkout of this repo:
#   git clone <your repo url> /root/mealie-mcp   (or copy the folder over with scp)
#   bash /root/mealie-mcp/deploy/install.sh
#
# Options:
#   --repo URL   clone this URL instead of the checkout the script sits in
#   --ref NAME   branch or tag to install (default: the checkout's current branch, or jamie/main)
#
# Safe to re-run: existing code is fast-forwarded and the env file is never overwritten.
set -euo pipefail

APP_USER="mealie-mcp"
BASE_DIR="/opt/mealie-mcp"
APP_DIR="${BASE_DIR}/app"
ENV_DIR="/etc/mealie-mcp"
ENV_FILE="${ENV_DIR}/mealie-mcp.env"
UNIT_FILE="/etc/systemd/system/mealie-mcp.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

REPO=""
REF=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root inside the LXC"
[[ -r /etc/debian_version ]] || die "expected a Debian container"

as_app() {
  runuser -u "$APP_USER" -- env \
    HOME="$BASE_DIR" \
    UV_CACHE_DIR="${BASE_DIR}/.cache/uv" \
    UV_PYTHON_INSTALL_DIR="${BASE_DIR}/python" \
    "$@"
}

say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl ca-certificates >/dev/null

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv to /usr/local/bin"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL=/usr/local/bin sh
fi

say "Creating the ${APP_USER} service user"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$BASE_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -o "$APP_USER" -g "$APP_USER" -m 0755 "$BASE_DIR"

if [[ -z "$REPO" ]]; then
  git -C "$SRC_DIR" rev-parse --git-dir >/dev/null 2>&1 \
    || die "$SRC_DIR is not a git checkout; pass --repo URL"
  REPO="$SRC_DIR"
  [[ -n "$REF" ]] || REF="$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD)"
fi
[[ -n "$REF" ]] || REF="jamie/main"

# Git runs as root, so root's credentials (e.g. a GitHub deploy key in /root/.ssh)
# work for a private repo; the service user then owns the files.
rgit() { git -c safe.directory="$APP_DIR" -C "$APP_DIR" "$@"; }
if [[ -d "${APP_DIR}/.git" ]]; then
  say "Updating code in ${APP_DIR} (${REF})"
  rgit fetch --quiet "$REPO" "$REF"
  rgit merge --ff-only --quiet FETCH_HEAD
else
  say "Cloning ${REPO} (${REF}) into ${APP_DIR}"
  git clone --quiet --branch "$REF" "$REPO" "$APP_DIR"
  if [[ "$REPO" == "$SRC_DIR" ]]; then
    ORIGIN="$(git -C "$SRC_DIR" remote get-url origin 2>/dev/null || true)"
    if [[ -n "$ORIGIN" ]]; then
      rgit remote set-url origin "$ORIGIN"
    fi
  fi
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

say "Installing Python 3.12 and dependencies with uv (frozen to uv.lock)"
as_app bash -c "cd '$APP_DIR' && uv sync --frozen --no-dev --python 3.12"

say "Setting up ${ENV_FILE}"
install -d -m 0750 "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  install -m 0600 "${APP_DIR}/deploy/mealie-mcp.env.example" "$ENV_FILE"
  echo "Created ${ENV_FILE} from the example. Fill it in before starting."
else
  echo "Kept existing ${ENV_FILE}"
fi

say "Installing the systemd unit"
install -m 0644 "${APP_DIR}/deploy/mealie-mcp.service" "$UNIT_FILE"
systemctl daemon-reload
systemctl enable --quiet mealie-mcp

if grep -qE '^MEALIE_API_KEY=.+' "$ENV_FILE"; then
  say "Restarting mealie-mcp"
  systemctl restart mealie-mcp
  sleep 3
  PORT="$(grep -E '^MCP_PORT=' "$ENV_FILE" | cut -d= -f2)"
  if curl -fsS "http://127.0.0.1:${PORT:-8000}/healthz" >/dev/null; then
    echo "Running: http://$(hostname -I | awk '{print $1}'):${PORT:-8000}/mcp"
  else
    echo "Service did not answer /healthz yet. Check: journalctl -u mealie-mcp -n 50"
  fi
else
  cat <<NEXT

Next steps:
  1. Edit ${ENV_FILE}: MEALIE_BASE_URL and MEALIE_API_KEY at minimum.
     Leave the AUTHENTIK_* lines commented until Authentik is set up.
  2. systemctl start mealie-mcp
  3. curl http://127.0.0.1:8000/healthz
  4. journalctl -u mealie-mcp -f     (logs)
NEXT
fi
