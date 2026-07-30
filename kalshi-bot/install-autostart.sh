#!/usr/bin/env bash
# Make the collectors start on login and restart on crash, so collection is not
# hostage to a Terminal window staying open.
#
#     ./install-autostart.sh              install and start
#     ./install-autostart.sh --dry-run    print what it WOULD write, change nothing
#     ./install-autostart.sh --uninstall  stop and remove
#
# It fills in the real paths itself -- there is no file to hand-edit. One agent
# is installed per config file that exists (weather, sports), each with its own
# log files so they cannot overwrite each other's output.
set -euo pipefail
cd "$(dirname "$0")"

ROOT="$(pwd)"
VENV_PY="$ROOT/.venv/bin/python"
AGENT_DIR="$HOME/Library/LaunchAgents"
PREFIX="com.projectrebound.kalshibot"

DRY_RUN=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) echo "unknown option: $arg"; exit 2 ;;
  esac
done

# label -> config file. Only families you actually have a config for get installed.
FAMILIES=("weather:config/config.yaml" "sports:config/sports.yaml")

emit_plist() {   # $1 = label, $2 = config path, $3 = family name
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$1</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/caffeinate</string>
        <string>-s</string>
        <string>$VENV_PY</string>
        <string>-m</string>
        <string>kalshibot.cli</string>
        <string>run</string>
        <string>--config</string>
        <string>$ROOT/$2</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$ROOT</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONPATH</key>
        <string>$ROOT/src</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$ROOT/logs/launchd.$3.out.log</string>
    <key>StandardErrorPath</key>
    <string>$ROOT/logs/launchd.$3.err.log</string>

    <key>ThrottleInterval</key>
    <integer>30</integer>
</dict>
</plist>
PLIST
}

unload_agent() {  # $1 = label
  launchctl bootout "gui/$UID/$1" 2>/dev/null || launchctl unload "$AGENT_DIR/$1.plist" 2>/dev/null || true
}

# ---- uninstall ----
if [ "$UNINSTALL" = "1" ]; then
  for entry in "${FAMILIES[@]}"; do
    family="${entry%%:*}"
    label="$PREFIX.$family"
    unload_agent "$label"
    rm -f "$AGENT_DIR/$label.plist"
    echo "removed $label"
  done
  echo
  echo "Auto-start removed. Your data and config are untouched."
  echo "Run by hand again with:  ./run.sh run --config config/sports.yaml"
  exit 0
fi

# ---- preflight ----
if [ "$DRY_RUN" = "0" ]; then
  if [ "$(uname -s)" != "Darwin" ]; then
    echo "x  This installs a macOS launchd agent and only works on a Mac."
    exit 1
  fi
  if [ ! -x "$VENV_PY" ]; then
    echo "x  No virtual environment found at .venv"
    echo "   Run ./setup.sh first, then try again."
    exit 1
  fi

  # Stop any agent WE installed before looking for strays. Re-running this
  # script (after a git pull, say) must not be blocked by its own agent -- the
  # only thing worth refusing over is a copy started by hand in a Terminal.
  for entry in "${FAMILIES[@]}"; do
    unload_agent "$PREFIX.${entry%%:*}"
  done
  sleep 1

  # Two collectors on one database would poll and write everything twice.
  # Self and parent are excluded: `pgrep -f` matches whole command lines, so a
  # shell whose own command line happens to contain the pattern would otherwise
  # report itself and this script could never run.
  if pgrep -f "kalshibot.cli run" 2>/dev/null | grep -vx -e "$$" -e "$PPID" >/dev/null; then
    echo "x  A collector is still running that launchd did not start --"
    echo "   almost certainly a Terminal window you started it in."
    echo
    echo "   Stop it: click that window and press Control + C."
    echo "   (Do this for every window you have one running in.)"
    echo "   Then run this script again -- from then on it starts by itself."
    exit 1
  fi
  mkdir -p "$AGENT_DIR" "$ROOT/logs"
fi

# ---- install ----
installed=0
for entry in "${FAMILIES[@]}"; do
  family="${entry%%:*}"
  config="${entry##*:}"
  label="$PREFIX.$family"

  if [ ! -f "$ROOT/$config" ]; then
    echo "-- skipping $family (no $config)"
    continue
  fi

  if [ "$DRY_RUN" = "1" ]; then
    echo "===== would write $AGENT_DIR/$label.plist"
    emit_plist "$label" "$config" "$family"
    installed=$((installed + 1))
    continue
  fi

  emit_plist "$label" "$config" "$family" > "$AGENT_DIR/$label.plist"
  unload_agent "$label"          # replace cleanly if it was already installed
  if launchctl bootstrap "gui/$UID" "$AGENT_DIR/$label.plist" 2>/dev/null \
     || launchctl load -w "$AGENT_DIR/$label.plist" 2>/dev/null; then
    echo "ok  $family collector installed and started ($config)"
    installed=$((installed + 1))
  else
    echo "x   $family collector could not be started by launchd"
    echo "    The file is at $AGENT_DIR/$label.plist if you want to inspect it."
  fi
done

if [ "$DRY_RUN" = "1" ]; then
  echo
  echo "Dry run only -- nothing was written or started."
  exit 0
fi

echo
if [ "$installed" = "0" ]; then
  echo "Nothing was installed. Expected config/config.yaml or config/sports.yaml."
  exit 1
fi

sleep 2
echo "Running now:"
launchctl list | grep "$PREFIX" || echo "  (nothing listed yet -- check the logs below)"

cat <<NEXT

They now start on login and restart if they crash. You can close this window.

  see it working     ./run.sh report --config config/sports.yaml
  startup errors     tail -n 40 logs/launchd.sports.err.log
  stop everything    ./install-autostart.sh --uninstall

NEXT
