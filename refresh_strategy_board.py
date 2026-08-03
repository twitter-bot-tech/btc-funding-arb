"""Refresh BTC spot data and rebuild the strategy board.

Cadence:
  - browser live BTC price: handled in strategy_board.html every 1 second
  - spot 15m K-line + page rebuild: every 15 minutes in --loop mode
  - confluence backtest/parameter scan: every 60 minutes in --loop mode
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import requests


ROOT = Path(__file__).parent
DATA = ROOT / "data"
SPOT_CSV = DATA / "btcusdt_15m_spot.csv"
REFRESH_STATE = DATA / "refresh_state.json"
SYMBOL = "BTCUSDT"
SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"
INTERVAL_MS = 15 * 60 * 1000


def request_json(url: str, params: dict) -> list:
    last_error = None
    for verify in (True, False):
        for attempt in range(4):
            try:
                response = requests.get(url, params=params, timeout=20, verify=verify)
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(1 + attempt)
    raise RuntimeError(f"request failed: {last_error}")


def fetch_spot_15m(days: int = 130) -> pd.DataFrame:
    DATA.mkdir(exist_ok=True)
    start_ms = int((pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)).timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    cursor = start_ms
    rows_out = []
    while cursor < now_ms:
        rows = request_json(
            SPOT_KLINES_URL,
            {"symbol": SYMBOL, "interval": "15m", "startTime": cursor, "limit": 1000},
        )
        if not rows:
            break
        rows_out.extend(rows)
        last = rows[-1][0]
        if last <= cursor:
            break
        cursor = last + INTERVAL_MS
        print(f"spot rows={len(rows_out):>6d} last={pd.Timestamp(last, unit='ms', tz='UTC')}")
        time.sleep(0.08)

    df = pd.DataFrame(rows_out, columns=[
        "openTime", "open", "high", "low", "close", "volume",
        "closeTime", "quoteVolume", "trades", "takerBase", "takerQuote", "ignore",
    ])
    df["ts"] = pd.to_datetime(df["openTime"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume", "quoteVolume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df = df[["ts", "open", "high", "low", "close", "volume", "quoteVolume", "trades"]]
    df.to_csv(SPOT_CSV, index=False)
    return df


def run_script(name: str) -> None:
    subprocess.run([sys.executable, str(ROOT / name)], cwd=ROOT, check=True)


def write_state(df: pd.DataFrame, *, backtest: bool) -> None:
    state = {
        "updated_at": pd.Timestamp.now(tz="Asia/Singapore").isoformat(),
        "source": "Binance Spot BTCUSDT 15m",
        "last_kline_utc": df["ts"].max().isoformat(),
        "last_kline_sgt": df["ts"].max().tz_convert("Asia/Singapore").isoformat(),
        "last_close": float(df["close"].iloc[-1]),
        "backtest_ran": backtest,
        "cadence": {
            "live_price": "1 second in browser",
            "kline_and_signal": "15 minutes via this script",
            "backtest": "60 minutes via this script",
        },
    }
    REFRESH_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def refresh_once(*, backtest: bool) -> None:
    df = fetch_spot_15m()
    if backtest:
        run_script("backtest_confluence_range.py")
    run_script("build_strategy_board.py")
    write_state(df, backtest=backtest)
    print(
        "refreshed",
        f"last={df['ts'].max().tz_convert('Asia/Singapore')}",
        f"close={df['close'].iloc[-1]:.2f}",
        f"backtest={backtest}",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="refresh forever")
    parser.add_argument("--no-backtest", action="store_true", help="skip the backtest on this run")
    parser.add_argument("--kline-seconds", type=int, default=15 * 60)
    parser.add_argument("--backtest-seconds", type=int, default=60 * 60)
    args = parser.parse_args()

    if not args.loop:
        refresh_once(backtest=not args.no_backtest)
        return

    last_backtest = 0.0
    while True:
        now = time.time()
        should_backtest = not args.no_backtest and now - last_backtest >= args.backtest_seconds
        refresh_once(backtest=should_backtest)
        if should_backtest:
            last_backtest = now
        time.sleep(args.kline_seconds)


if __name__ == "__main__":
    main()
