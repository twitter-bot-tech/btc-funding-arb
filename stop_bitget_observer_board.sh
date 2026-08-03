#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/coco/btc-funding-arb"
PID="$ROOT/data/bitget_observer_board.pid"

if [[ ! -f "$PID" ]]; then
  echo "not running: pid file missing"
  exit 0
fi

pid="$(cat "$PID")"
if kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  echo "stopped pid=$pid"
else
  echo "not running: stale pid=$pid"
fi
rm -f "$PID"
