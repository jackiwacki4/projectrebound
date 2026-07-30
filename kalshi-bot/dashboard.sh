#!/usr/bin/env bash
# Build a visual results page for every family you have a config for, then open
# them. Read-only: it only reads the databases and writes HTML into reports/.
#
#     ./dashboard.sh            build and open both
#     ./dashboard.sh sports     just one
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

WANTED="${1:-}"
built=()
for entry in "sports:config/sports.yaml" "weather:config/config.yaml"; do
  family="${entry%%:*}"; config="${entry##*:}"
  [ -n "$WANTED" ] && [ "$WANTED" != "$family" ] && continue
  [ -f "$config" ] || continue
  out="reports/$family.html"
  if PYTHONPATH="$ROOT/src" "$PY" -m kalshibot.cli dashboard --config "$config" --out "$out"; then
    built+=("$out")
  fi
done

if [ ${#built[@]} -eq 0 ]; then
  echo "Nothing built. Expected config/sports.yaml or config/config.yaml."
  exit 1
fi
# `open` is macOS; elsewhere just print the paths.
if command -v open >/dev/null 2>&1; then
  open "${built[@]}"
else
  echo "Open these in a browser:"; printf '  %s\n' "${built[@]}"
fi
