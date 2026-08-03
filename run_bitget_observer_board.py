"""Refresh Bitget observe-only state and rebuild the strategy board every 60s.

This loop does not place orders. It only reads Bitget ticker/account state,
updates data/bitget_observer_state.json, and rebuilds strategy_board.html.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).parent
DATA = ROOT / "data"
HEALTH = DATA / "bitget_observer_health.json"


def run_script(name: str, *args: str) -> None:
    subprocess.run([sys.executable, str(ROOT / name), *args], cwd=ROOT, check=True)


def write_health(payload: dict) -> None:
    DATA.mkdir(exist_ok=True)
    HEALTH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def refresh_once(*, quote_usdt: str, public_only: bool) -> None:
    observer_args = ["--once", "--quote-usdt", quote_usdt]
    if public_only:
        observer_args.append("--public-only")
    run_script("bitget_observer.py", *observer_args)
    run_script("build_strategy_board.py")
    print(datetime.now().astimezone().isoformat(), "observer board refreshed", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval-sec", type=int, default=60)
    parser.add_argument("--quote-usdt", default="5")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--public-only", action="store_true", help="Skip private account APIs for free cloud tests")
    args = parser.parse_args()

    failures = 0
    while True:
        started = datetime.now().astimezone()
        try:
            refresh_once(quote_usdt=args.quote_usdt, public_only=args.public_only)
            failures = 0
            write_health({
                "status": "ok",
                "mode": "observe_only",
                "last_ok_at": datetime.now().astimezone().isoformat(),
                "last_started_at": started.isoformat(),
                "interval_sec": args.interval_sec,
                "quote_usdt": args.quote_usdt,
                "public_only": args.public_only,
                "consecutive_failures": failures,
            })
        except Exception as exc:  # noqa: BLE001
            failures += 1
            now = datetime.now().astimezone().isoformat()
            write_health({
                "status": "error",
                "mode": "observe_only",
                "last_error_at": now,
                "last_started_at": started.isoformat(),
                "interval_sec": args.interval_sec,
                "quote_usdt": args.quote_usdt,
                "public_only": args.public_only,
                "consecutive_failures": failures,
                "error": repr(exc),
            })
            print(now, "observer board refresh failed", repr(exc), flush=True)
        if args.once:
            return
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    main()
