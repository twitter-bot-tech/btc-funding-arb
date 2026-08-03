#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: ./deploy_to_vps.sh user@host [remote_dir]"
  echo "example: ./deploy_to_vps.sh root@1.2.3.4 /opt/btc-funding-arb"
  exit 1
fi

TARGET="$1"
REMOTE_DIR="${2:-/opt/btc-funding-arb}"

rsync -az --delete \
  --exclude ".env" \
  --exclude ".venv/" \
  --exclude "__pycache__/" \
  --exclude "*.pyc" \
  --exclude "*.log" \
  --exclude "data/*.log" \
  --exclude "data/*.pid" \
  --exclude "data/*order_ledger.jsonl" \
  ./ "$TARGET:$REMOTE_DIR/"

echo "uploaded to $TARGET:$REMOTE_DIR"
echo "next on VPS:"
echo "  cd $REMOTE_DIR"
echo "  sudo bash deploy/setup_vps_ubuntu.sh"
echo "  sudo nano $REMOTE_DIR/.env"
echo "  sudo systemctl restart bitget-observer-board"
