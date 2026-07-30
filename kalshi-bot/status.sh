#!/usr/bin/env bash
# One command to see how both collectors are doing.
#
#     ./status.sh              both families: alive?, how long collecting, report
#     ./status.sh sports       just one of them
#
# Read-only. Safe to run while the collectors are writing -- the database is in
# WAL mode, so a reader never blocks or disturbs a writer.
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

FAMILIES=("sports:config/sports.yaml" "weather:config/config.yaml")
WANTED="${1:-}"

# Which collector is running which family? They are the same program, so the
# only thing that distinguishes them is the --config argument on the command
# line -- and a collector started as plain `./run.sh run` has NO such argument
# and is running the DEFAULT config. Matching on the config path alone reported
# exactly that case as "not running" while its data was seconds old.
DEFAULT_CONFIG="config/config.yaml"

collector_pids() {   # $1 = config path for this family
  local found=""
  local pid cmd cfg
  for pid in $(pgrep -f "kalshibot.cli run" 2>/dev/null); do
    [ "$pid" = "$$" ] && continue
    [ "$pid" = "$PPID" ] && continue
    cmd="$(ps -o command= -p "$pid" 2>/dev/null)"
    case "$cmd" in
      *kalshibot.cli*run*) ;;
      *) continue ;;
    esac
    case "$cmd" in
      *--config*)
        cfg="${cmd##*--config }"
        cfg="${cfg%% *}"
        [ "$(basename "$cfg")" = "$(basename "$1")" ] && found="$found $pid"
        ;;
      *)  # no --config: it is running the default
        [ "$1" = "$DEFAULT_CONFIG" ] && found="$found $pid"
        ;;
    esac
  done
  echo "${found# }"
}

# How long has this database actually been collecting, and is it still? Two days
# of uptime and two days of DATA are different claims -- a collector that died
# after an hour still looks "installed", and only the span shows it.
span_report() {   # $1 = config path
  PYTHONPATH="$ROOT/src" "$PY" - "$1" <<'PY'
import sys, sqlite3, time, pathlib, yaml

cfg = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text())
db = pathlib.Path(cfg.get("storage", {}).get("db_path", "./data/kalshibot.db"))
if not db.exists():
    print("    database    : not created yet -- the collector has never written")
    raise SystemExit

conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
row = conn.execute(
    "SELECT COUNT(*) n, MIN(captured_ts) lo, MAX(captured_ts) hi FROM book_snapshots"
).fetchone()
if not row["n"]:
    print("    collecting  : no price snapshots yet")
    raise SystemExit

now = time.time() * 1000
hours = (row["hi"] - row["lo"]) / 3_600_000
mins_ago = (now - row["hi"]) / 60_000
size_mb = db.stat().st_size / 1_048_576
print(f"    collecting  : {hours:.1f} hours of price history, {row['n']:,} snapshots")
print(f"    last update : {mins_ago:.0f} minutes ago" +
      ("   <-- STALE, the collector may have stopped" if mins_ago > 10 else ""))
# Full-depth order books every 60s add up fast. Say so in months, not bytes,
# while there is still time to do something about it.
per_day = size_mb / max(hours / 24, 1e-9)
print(f"    database    : {db}  ({size_mb:.1f} MB, growing ~{per_day:.0f} MB/day"
      f" = ~{per_day * 30 / 1024:.1f} GB/month)")
if per_day * 30 / 1024 > 3:
    print("                  ^ worth watching: this will fill a laptop in months")

# Which code is this collector actually running? `git pull` does not restart it,
# so the code on disk and the code in memory can differ silently.
from kalshibot.runtime.version import git_revision, read_version_stamp
stamp = read_version_stamp(str(db))
on_disk = git_revision()
if stamp is None:
    print("    code        : unknown (started before version tracking -- "
          "restart to enable)")
else:
    running = stamp.get("revision", "unknown")
    if running == on_disk:
        print(f"    code        : {running}  (matches the folder)")
    else:
        print(f"    code        : running {running}, folder has {on_disk}")
        print("                  ^ RESTART NEEDED to load the newer code: ./update.sh")
PY
}

for entry in "${FAMILIES[@]}"; do
  family="${entry%%:*}"
  config="${entry##*:}"
  [ -n "$WANTED" ] && [ "$WANTED" != "$family" ] && continue
  [ -f "$config" ] || continue

  echo
  echo "================================================================"
  # `tr`, not ${var^^}: macOS still ships bash 3.2, where ^^ is a syntax error.
  echo "  $(echo "$family" | tr '[:lower:]' '[:upper:]')  ($config)"
  echo "================================================================"

  pids="$(collector_pids "$config")"
  if [ -n "${pids// /}" ]; then
    echo "    running     : YES (pid ${pids% })"
  else
    echo "    running     : NO  <-- not collecting right now"
    echo "                  start it:  ./run.sh run --config $config"
    echo "                  or reinstall auto-start:  ./install-autostart.sh"
  fi
  span_report "$config"
  echo
  PYTHONPATH="$ROOT/src" "$PY" -m kalshibot.cli report --config "$config"
done

echo
echo "Full ledger with a bigger stake:  ./run.sh report --config config/sports.yaml --stake 25"
