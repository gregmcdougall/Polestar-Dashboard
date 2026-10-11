#!/usr/bin/env bash
# Run the dashboard as a macOS LaunchAgent: starts when you log in (with
# FileVault on, unlocking the Mac at boot logs you in, so this is effectively
# "at boot"), restarts if it crashes.
#
#   scripts/service.sh install     install + start (re-run after moving the project)
#   scripts/service.sh restart     restart, e.g. after pulling new server code
#   scripts/service.sh status      is it running? what does the API say?
#   scripts/service.sh logs        follow the log
#   scripts/service.sh uninstall   stop + remove
#
# Port: DASHBOARD_PORT (default 8766).
set -euo pipefail

LABEL="local.polestar-dashboard"
PORT="${DASHBOARD_PORT:-8766}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/polestar-dashboard.log"
DOMAIN="gui/$(id -u)"
UV="$(command -v uv || true)"

xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }

is_loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

port_owner() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1; }

install() {
  [[ -n "$UV" ]] || { echo "uv not found on PATH" >&2; exit 1; }
  [[ -f "$ROOT/.env" ]] || { echo "No .env in $ROOT — copy .env.example and add your credentials first" >&2; exit 1; }

  if is_loaded; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    sleep 1
  fi
  if pid="$(port_owner)" && [[ -n "$pid" ]]; then
    echo "Port $PORT is already in use by PID $pid:" >&2
    ps -o command= -p "$pid" >&2
    echo "Stop that server first (Ctrl+C in its terminal), then re-run install." >&2
    exit 1
  fi

  (cd "$ROOT" && "$UV" sync --frozen --quiet)
  mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
  touch "$LOG" && chmod 600 "$LOG"
  chmod 600 "$ROOT/.env"

  cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml_escape "$UV")</string>
    <string>run</string>
    <string>--frozen</string>
    <string>python</string>
    <string>-m</string>
    <string>server</string>
    <string>--port</string>
    <string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml_escape "$ROOT")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(xml_escape "$(dirname "$UV")"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(xml_escape "$LOG")</string>
  <key>StandardErrorPath</key><string>$(xml_escape "$LOG")</string>
</dict>
</plist>
EOF
  plutil -lint -s "$PLIST"

  launchctl bootstrap "$DOMAIN" "$PLIST"
  launchctl enable "$DOMAIN/$LABEL"
  echo "Installed $LABEL → http://127.0.0.1:$PORT  (log: $LOG)"
  wait_until_up
}

wait_until_up() {
  for _ in $(seq 1 20); do
    if curl -fsS "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
      echo "Server is up."
      return 0
    fi
    sleep 1
  done
  echo "Server didn't answer within 20 s — check: $0 logs" >&2
  return 1
}

uninstall() {
  if is_loaded; then launchctl bootout "$DOMAIN/$LABEL" || true; fi
  rm -f "$PLIST"
  echo "Removed $LABEL (log kept at $LOG)"
}

restart() {
  is_loaded || { echo "Not installed — run: $0 install" >&2; exit 1; }
  (cd "$ROOT" && "$UV" sync --frozen --quiet)
  launchctl kickstart -k "$DOMAIN/$LABEL"
  echo "Restarted $LABEL"
  wait_until_up
}

status() {
  if is_loaded; then
    launchctl print "$DOMAIN/$LABEL" | grep -E '^\s*(state|pid|last exit code) =' | sed 's/^\s*/  /'
  else
    echo "  not installed"
  fi
  if body="$(curl -fsS "http://127.0.0.1:$PORT/api/status" 2>/dev/null)"; then
    echo "  api: $body"
  else
    echo "  api: not responding on port $PORT"
  fi
}

case "${1:-}" in
  install) install ;;
  uninstall) uninstall ;;
  restart) restart ;;
  status) status ;;
  logs) touch "$LOG"; tail -n 50 -f "$LOG" ;;
  *) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
