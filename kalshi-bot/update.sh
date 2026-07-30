#!/usr/bin/env bash
# Get the latest code AND make the running bots actually use it.
#
#     ./update.sh
#
# The step people miss: `git pull` does not restart anything. Python read the
# source once at startup, so a pull leaves the old model running with no outward
# sign -- same logs, same data arriving. This pulls, checks the tests still pass,
# and only then restarts the collectors.
#
# If the tests fail it STOPS without restarting. Old-but-working beats
# new-but-broken when the thing is unattended for days.
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
AGENT_DIR="$HOME/Library/LaunchAgents"
PREFIX="com.projectrebound.kalshibot"

say() { printf '\n== %s\n' "$1"; }

before="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

# ---- 1. local edits ----
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  say "You have local changes to tracked files"
  git status --short
  cat <<'NOTE'
  A pull may conflict with these. Options:
    keep them   : git stash          (then `git stash pop` after this finishes)
    discard them: git checkout -- .  (throws your edits away, permanently)
  Re-run ./update.sh once you have decided.
NOTE
  exit 1
fi

# ---- 2. pull ----
say "Getting the latest code"
if ! git pull --ff-only; then
  cat <<'NOTE'

  The pull did not go through cleanly. Nothing has been restarted, so your bots
  are still running exactly as before -- no harm done. Paste the message above
  and it can be sorted out.
NOTE
  exit 1
fi
after="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

if [ "$before" = "$after" ]; then
  echo "  Already on the latest code ($after)."
else
  echo "  Updated: $before -> $after"
fi

# ---- 3. prove it works before trusting it ----
say "Checking the tests still pass"
if ! "$PY" -m pytest --version >/dev/null 2>&1; then
  echo "  pytest is not installed; installing it into .venv"
  "$PY" -m pip install -q pytest >/dev/null 2>&1 || true
fi
if "$PY" -m pytest --version >/dev/null 2>&1; then
  if PYTHONPATH="$ROOT/src" "$PY" -m pytest -q; then
    echo "  Tests pass."
  else
    # Unquoted heredoc on purpose: $before must expand to the real commit, or
    # this prints an instruction that does nothing.
    cat <<NOTE

  STOPPING: the tests do not pass on this code, so nothing was restarted. Your
  bots are still running the previous version and still collecting. To go back
  to exactly what you had:  git checkout $before
NOTE
    exit 1
  fi
else
  echo "  Could not run the tests (pytest unavailable). Continuing without them."
fi

# ---- 4. restart so the new code is actually loaded ----
say "Restarting the collectors"
installed=0
for family in sports weather; do
  [ -f "$AGENT_DIR/$PREFIX.$family.plist" ] && installed=1
done

if [ "$installed" = "1" ]; then
  # install-autostart.sh stops its own agents first, so this is the restart.
  ./install-autostart.sh
else
  cat <<'NOTE'
  Your bots are not set up to run in the background, so they cannot be restarted
  from here. In each window where one is running:

      press Control + C, then run that same command again

  Or set up background running once, and future updates restart themselves:

      ./install-autostart.sh
NOTE
fi

# ---- 5. confirm what is now running ----
say "What is running now"
sleep 2
./status.sh 2>/dev/null | grep -E "^  [A-Z]+|running |code " || true
cat <<'NOTE'

Every "code" line above should show the same short code as the folder. If one
still says RESTART NEEDED, that collector did not come back up -- check
logs/launchd.<family>.err.log
NOTE
