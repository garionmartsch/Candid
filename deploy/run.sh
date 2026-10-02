#!/bin/bash
# Launched by the launchd service (see launchd/com.candid.app.plist) — not
# meant to be run by hand except to test. Loads the real environment file
# and execs gunicorn so it becomes the supervised process (launchd then
# watches gunicorn directly, not this script).
set -euo pipefail

# Resolve paths relative to this script's own location, so the plist can
# point at run.sh from anywhere without hardcoding the install directory
# twice.
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$DEPLOY_DIR")"
ENV_FILE="$DEPLOY_DIR/candid.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy candid.env.example to candid.env and fill it in first." >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

cd "$APP_DIR"

# 127.0.0.1 only: nginx is what should be reachable from the network,
# terminating TLS and proxying to this. Gunicorn itself never listens
# anywhere but localhost. 3 workers is a reasonable default for a small
# team; raise it if load-testing (Phase 3) says you need more.
exec "$APP_DIR/venv/bin/gunicorn" \
  --workers 3 \
  --bind 127.0.0.1:8000 \
  --access-logfile - \
  --error-logfile - \
  app:app
