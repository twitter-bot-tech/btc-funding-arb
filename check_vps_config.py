"""Basic server-side config check for the observe-only Bitget deployment."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from exchange.bitget_spot import load_env


ROOT = Path(__file__).parent
ENV = ROOT / ".env"


def main() -> None:
    if not ENV.exists():
        raise SystemExit("missing .env")
    mode = ENV.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SystemExit(".env permissions too open; run: chmod 600 .env")

    load_env()
    missing = [
        key
        for key in ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE", "BITGET_BASE_URL")
        if not os.environ.get(key, "").strip()
    ]
    if missing:
        raise SystemExit(f"missing env keys: {', '.join(missing)}")
    if os.environ.get("BITGET_ALLOW_LIVE", "0") != "0":
        raise SystemExit("BITGET_ALLOW_LIVE must stay 0 for observe-only deployment")
    print("vps_config_ok: observe-only, env present, live disabled")


if __name__ == "__main__":
    main()
