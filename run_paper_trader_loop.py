"""Run paper_trader.py on a fixed interval and write a health file."""
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
HEALTH = DATA / "paper_trader_health.json"


def write_health(payload: dict) -> None:
    DATA.mkdir(exist_ok=True)
    HEALTH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_once(args: argparse.Namespace) -> None:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "paper_trader.py"),
            "--capital",
            args.capital,
            "--quote-usdt",
            args.quote_usdt,
            "--fee-roundtrip",
            args.fee_roundtrip,
            "--take-profit",
            args.take_profit,
            "--stop-loss",
            args.stop_loss,
            "--max-hold-runs",
            str(args.max_hold_runs),
        ],
        cwd=ROOT,
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval-sec", type=int, default=15 * 60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--capital", default="10000")
    parser.add_argument("--quote-usdt", default="50")
    parser.add_argument("--fee-roundtrip", default="0.0012")
    parser.add_argument("--take-profit", default="0.012")
    parser.add_argument("--stop-loss", default="0.01")
    parser.add_argument("--max-hold-runs", type=int, default=16)
    args = parser.parse_args()

    failures = 0
    while True:
        started = datetime.now().astimezone()
        try:
            run_once(args)
            failures = 0
            write_health({
                "status": "ok",
                "mode": "paper_trading",
                "last_ok_at": datetime.now().astimezone().isoformat(),
                "last_started_at": started.isoformat(),
                "interval_sec": args.interval_sec,
                "quote_usdt": args.quote_usdt,
                "consecutive_failures": failures,
            })
        except Exception as exc:  # noqa: BLE001
            failures += 1
            now = datetime.now().astimezone().isoformat()
            write_health({
                "status": "error",
                "mode": "paper_trading",
                "last_error_at": now,
                "last_started_at": started.isoformat(),
                "interval_sec": args.interval_sec,
                "quote_usdt": args.quote_usdt,
                "consecutive_failures": failures,
                "error": repr(exc),
            })
            print(now, "paper trader failed", repr(exc), flush=True)
        if args.once:
            return
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    main()
