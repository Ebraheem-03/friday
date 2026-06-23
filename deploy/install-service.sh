#!/usr/bin/env bash
# Install FRIDAY as a systemd *user* service: starts on login, auto-restarts on
# crash, runs in the background. No sudo required (user-scoped).
#
#   Usage:   ./deploy/install-service.sh
#   Manage:  systemctl --user {status,start,stop,restart} friday
#   Logs:    journalctl --user -u friday -f
#   Remove:  systemctl --user disable --now friday && rm ~/.config/systemd/user/friday.service
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRIDAY_BIN="$(command -v friday || true)"

if [ -z "$FRIDAY_BIN" ]; then
  echo "ERROR: 'friday' is not on PATH. Install it first:" >&2
  echo "  cd $REPO_DIR && pip install -e ." >&2
  exit 1
fi

if [ ! -f "$REPO_DIR/.env" ]; then
  echo "WARNING: $REPO_DIR/.env not found — GEMINI_API_KEY must be set there." >&2
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"

sed -e "s|__WORKDIR__|$REPO_DIR|g" \
    -e "s|__FRIDAY_BIN__|$FRIDAY_BIN|g" \
    "$REPO_DIR/deploy/friday.service.in" > "$UNIT_DIR/friday.service"
echo "Wrote $UNIT_DIR/friday.service"

systemctl --user daemon-reload
# Make the current session's display vars visible to the service manager.
systemctl --user import-environment WAYLAND_DISPLAY DISPLAY XDG_RUNTIME_DIR 2>/dev/null || true
systemctl --user enable --now friday.service

echo
echo "FRIDAY service installed and started."
echo "  Status: systemctl --user status friday"
echo "  Logs:   journalctl --user -u friday -f"
echo "  Stop:   systemctl --user stop friday"
