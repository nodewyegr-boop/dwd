#!/usr/bin/env bash
# Run the bot in the background with auto-restart

set -e
cd "$(dirname "$0")"

while true; do
    echo "[$(date)] starting bot..."
    python3 bot.py
    echo "[$(date)] bot exited — restarting in 5s..."
    sleep 5
done
