#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/coco/btc-funding-arb"
LABEL="com.coco.bitget-observer-board"
SRC="$ROOT/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data"
cp "$SRC" "$DEST"
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"
launchctl start "$LABEL" 2>/dev/null || true
echo "installed launch agent: $DEST"
echo "check log: tail -f $ROOT/data/bitget_observer_board.log"
