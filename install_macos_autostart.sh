#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/coco/btc-funding-arb"
UID_NUM="$(id -u)"
DOMAIN="gui/$UID_NUM"
LABELS=(
  "com.coco.strategy-refresh"
  "com.coco.bitget-observer-board"
  "com.coco.paper-trader"
  "com.coco.bitget-live-5u-test"
)

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data"
for LABEL in "${LABELS[@]}"; do
  SRC="$ROOT/$LABEL.plist"
  DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
  cp "$SRC" "$DEST"
  launchctl bootout "$DOMAIN" "$DEST" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$DEST"
  launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true
  echo "installed launch agent: $DEST"
done

echo "logs:"
echo "  tail -f $ROOT/data/strategy_refresh.log"
echo "  tail -f $ROOT/data/bitget_observer_board.log"
echo "  tail -f $ROOT/data/paper_trader.log"
echo "  tail -f $ROOT/data/bitget_live_5u.log"
