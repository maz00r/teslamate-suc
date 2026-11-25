#!/usr/bin/env sh
# Options are read by Python straight from /data/options.json.
set -e
echo "[suc] start"
exec python3 /app/main.py
