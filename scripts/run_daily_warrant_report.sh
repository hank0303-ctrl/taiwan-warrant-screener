#!/bin/zsh
set -eu

cd "/Users/hank/Downloads/Hank claude"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] warrant daily report started"

git pull --ff-only origin main

/usr/bin/python3 - <<'PY'
from warrant_screener import run_screening

run_screening()
PY

echo "[$(date '+%Y-%m-%d %H:%M:%S')] warrant daily report finished"
