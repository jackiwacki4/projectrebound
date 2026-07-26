#!/usr/bin/env bash
# Wrapper so you never have to think about PYTHONPATH or virtualenvs.
#
#     ./run.sh run                 start collecting (leave the window open)
#     ./run.sh report              validation report + trade ledger
#     ./run.sh report --stake 25   same, with P&L scaled to $25 per trade
#     ./run.sh health              quick pulse check
#     ./run.sh reset-breaker       clear a tripped daily-loss breaker
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Setup hasn't been run yet. Run ./setup.sh first."
  exit 1
fi

PYTHONPATH=src exec ./.venv/bin/python -m kalshibot.cli "$@"
