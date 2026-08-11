#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/btc-funding-arb}"
APP_USER="${APP_USER:-btcbot}"
SERVICES=(
  "strategy-refresh"
  "bitget-observer-board"
  "paper-trader"
  "bitget-live-5u-test"
)

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root: sudo bash deploy/setup_vps_ubuntu.sh"
  exit 1
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip nginx rsync ca-certificates

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

mkdir -p "$APP_DIR/data"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

if [[ ! -f "$APP_DIR/.env" ]]; then
  install -o "$APP_USER" -g "$APP_USER" -m 600 "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "created $APP_DIR/.env from .env.example; fill Bitget keys before starting live integrations"
else
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
fi

for SERVICE in "${SERVICES[@]}"; do
  cp "$APP_DIR/deploy/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
done
systemctl daemon-reload
for SERVICE in "${SERVICES[@]}"; do
  systemctl enable "$SERVICE"
  systemctl restart "$SERVICE"
done

cp "$APP_DIR/deploy/nginx-bitget-board.conf" /etc/nginx/sites-available/bitget-board
ln -sf /etc/nginx/sites-available/bitget-board /etc/nginx/sites-enabled/bitget-board
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo "installed services: ${SERVICES[*]}"
echo "status:"
for SERVICE in "${SERVICES[@]}"; do
  echo "  systemctl status $SERVICE"
done
echo "logs:"
for SERVICE in "${SERVICES[@]}"; do
  echo "  journalctl -u $SERVICE -f"
done
echo "health: $APP_DIR/.venv/bin/python $APP_DIR/check_bitget_observer_health.py"
