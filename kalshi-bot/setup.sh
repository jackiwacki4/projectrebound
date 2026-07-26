#!/usr/bin/env bash
# One-shot setup for the projectrebound Phase 1 collector on macOS.
#
# Run it once, from inside the kalshi-bot folder:
#     ./setup.sh
#
# It checks your Python, installs everything into a clean private environment
# (which also sidesteps the common macOS "externally managed" pip error), and
# creates your config + settings files. It never overwrites files you've edited.
set -euo pipefail
cd "$(dirname "$0")"

echo "projectrebound - setup"
echo "======================"

# 1. Python 3.11+
if ! command -v python3 >/dev/null 2>&1; then
  echo "x  python3 not found."
  echo "   Install Python 3.11+ from https://www.python.org/downloads/ then run ./setup.sh again."
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "x  $(python3 -V) is too old (need 3.11+)."
  echo "   Install a newer Python from https://www.python.org/downloads/ then run ./setup.sh again."
  exit 1
fi
echo "ok $(python3 -V)"

# 2. Private virtual environment + libraries
if [ ! -d .venv ]; then
  echo ".. creating a private environment (.venv)"
  python3 -m venv .venv
fi
echo ".. installing libraries (about a minute)"
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install -r requirements.txt

# 3. Config + settings scaffolding (never clobber existing files)
if [ ! -f config/config.yaml ]; then
  cp config/config.example.yaml config/config.yaml
  echo ".. wrote config/config.yaml"
fi
if [ ! -f .env ]; then
  cp .env.example .env
  echo ".. wrote .env"
fi
mkdir -p secrets

cat <<'NEXT'

--------------------------------------------------------------------
Setup finished. Two things only you can do:

  1) Create a Kalshi API key at:
        kalshi.com  ->  Account  ->  Profile  ->  API Keys
     Save the downloaded private-key file here, with this exact name:
        secrets/kalshi_private_key.pem

  2) Put your Key ID into your settings. This opens the file in TextEdit:
        open -e .env
     Set   KALSHI_API_KEY_ID=...   to the Key ID Kalshi showed you, then save.

Then start collecting (leave this window open):
        ./run.sh run

And watch it anytime from a second window:
        ./run.sh report      # the validation report + trade ledger
        ./run.sh health      # a quick pulse check
--------------------------------------------------------------------
NEXT
