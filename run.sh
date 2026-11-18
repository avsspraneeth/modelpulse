#!/bin/zsh
# ModelPulse launcher
# Usage:
#   ./run.sh            start in foreground (Ctrl-C to stop)
#   ./run.sh start      start as a background daemon (nohup)
#   ./run.sh stop       stop the daemon
#   ./run.sh status     show status
#   ./run.sh install    install launchd auto-start (survives reboot/login)
#   ./run.sh uninstall  remove launchd auto-start
set -euo pipefail
cd "$(dirname "$0")"
PORT="${MODELPULE_PORT:-8787}"
PIDFILE=".modelpulse.pid"
PLIST="$HOME/Library/LaunchAgents/com.praneeth.modelpulse.plist"
PY="$(command -v python3)"

cmd="${1:-}"

is_running() {
  [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

case "$cmd" in
  start)
    if is_running; then echo "already running (pid $(cat "$PIDFILE"))"; exit 0; fi
    nohup "$PY" modelpulse.py --port "$PORT" >> modelpulse.out.log 2>&1 &
    echo $! > "$PIDFILE"
    sleep 2
    if is_running; then echo "ModelPulse started on http://127.0.0.1:$PORT (pid $(cat "$PIDFILE"))"; else echo "failed to start — check modelpulse.out.log"; exit 1; fi
    ;;
  stop)
    if is_running; then kill "$(cat "$PIDFILE")"; rm -f "$PIDFILE"; echo "stopped"; else echo "not running"; fi
    ;;
  status)
    if is_running; then echo "running (pid $(cat "$PIDFILE")) on :$PORT"; else echo "not running"; fi
    ;;
  check)
    "$PY" modelpulse.py --check
    ;;
  config)
    "$PY" modelpulse.py --init-config >/dev/null 2>&1
    echo "Config file: $PWD/config.json"
    echo "Edit it to point ModelPulse at your oMLX / Hermes / Splash setup, then:"
    echo "  ./run.sh check     # verify all paths resolve"
    echo "  ./run.sh stop && ./run.sh start   # apply (or reload launchd)"
    [[ "${2:-}" == "-e" ]] && "${EDITOR:-vi}" "$PWD/config.json"
    ;;
  install)
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.praneeth.modelpulse</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$PWD/modelpulse.py</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$PWD/modelpulse.out.log</string>
  <key>StandardErrorPath</key><string>$PWD/modelpulse.err.log</string>
</dict>
</plist>
PLIST
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "installed launchd auto-start (label com.praneeth.modelpulse)"
    ;;
  uninstall)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed launchd auto-start"
    ;;
  *)
    # foreground
    exec "$PY" modelpulse.py --port "$PORT"
    ;;
esac
