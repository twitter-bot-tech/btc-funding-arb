#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/coco/btc-funding-arb"
LOG="$ROOT/data/bitget_observer_board.log"
PID="$ROOT/data/bitget_observer_board.pid"

mkdir -p "$ROOT/data"
cd "$ROOT"

if [[ -f "$PID" ]] && kill -0 "$(cat "$PID")" 2>/dev/null; then
  echo "already running pid=$(cat "$PID")"
  exit 0
fi

nohup "$ROOT/.venv/bin/python" -u "$ROOT/run_bitget_observer_board.py" --interval-sec 60 --quote-usdt 5 >> "$LOG" 2>&1 &
echo "$!" > "$PID"
echo "started pid=$(cat "$PID") log=$LOG"
