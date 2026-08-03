#!/usr/bin/env bash
set -euo pipefail

LABEL="com.coco.bitget-observer-board"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl stop "$LABEL" 2>/dev/null || true
launchctl unload "$DEST" 2>/dev/null || true
rm -f "$DEST"
echo "removed launch agent: $DEST"
