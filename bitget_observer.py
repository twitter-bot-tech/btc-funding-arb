"""Bitget observe-only loop for the BTC confluence range strategy.

This script never places orders. It reads live Bitget ticker/account data,
evaluates the same strategy signal used by strategy_executor.py, and writes
state snapshots for review and dashboard integration.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from exchange.bitget_spot import BitgetSpotClient, load_env
from strategy_executor import current_signal, money, pct


ROOT = Path(__file__).parent
DATA = ROOT / "data"
STATE = DATA / "bitget_observer_state.json"
LOG = DATA / "bitget_observer_log.jsonl"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    DATA.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    DATA.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def as_decimal(value: Any, default: str = "0") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    return Decimal(str(value))


def bitget_last_price(ticker: dict[str, Any]) -> Decimal:
    for key in ("lastPr", "last", "close", "price"):
        if key in ticker and ticker[key] not in (None, ""):
            return as_decimal(ticker[key])
    raise RuntimeError(f"Cannot find last price in Bitget ticker: {ticker}")


def find_asset(assets_payload: dict[str, Any], coin: str) -> dict[str, Any]:
    data = assets_payload.get("data") or {}
    rows = data.get("assets") if isinstance(data, dict) else []
    for row in rows or []:
        if row.get("coin") == coin:
            return row
    return {}


def build_snapshot(client: BitgetSpotClient, *, symbol: str, quote_usdt: Decimal, take_profit_pct: Decimal, public_only: bool = False) -> dict[str, Any]:
    ticker = client.ticker(symbol)
    assets = {"data": {"assets": []}} if public_only else client.assets(coin="USDT")
    latest, params, score, signals, range_ok, buy_ready = current_signal()

    live_price = bitget_last_price(ticker)
    signal_close = Decimal(str(float(latest["close"])))
    buy_zone = Decimal(str(float(latest["range_lo"] + (latest["range_hi"] - latest["range_lo"]) * params["buy_q"])))
    sell_zone = Decimal(str(float(latest["range_lo"] + (latest["range_hi"] - latest["range_lo"]) * params["sell_q"])))
    stop_zone = Decimal(str(float(latest["range_lo"] * (1 - params["stop_break"]))))
    planned_size = client.quote_to_base_size(quote_amount=quote_usdt, price=live_price)
    take_profit_price = live_price * (Decimal("1") + take_profit_pct)
    asset = find_asset(assets, "USDT")

    blockers: list[str] = []
    if not range_ok:
        blockers.append("价格不在低吸区或区间宽度不足")
    if score < params["min_score"]:
        blockers.append("共振分数不足")
    if not signals["not_one_way_down"]:
        blockers.append("疑似单边下跌")
    if not public_only and as_decimal(asset.get("available")) < quote_usdt:
        blockers.append("UTA 可用 USDT 不足")

    action = "BUY_WATCH" if buy_ready and not blockers else "WAIT"
    now = datetime.now().astimezone()
    return {
        "ts": int(time.time()),
        "local_time": now.isoformat(),
        "exchange": "bitget",
        "mode": "PUBLIC_TEST" if public_only else "OBSERVE_ONLY",
        "symbol": symbol,
        "action": action,
        "will_place_order": False,
        "live_price": str(live_price),
        "signal": {
            "source_ts_sgt": pd.Timestamp(latest["ts"]).tz_convert("Asia/Singapore").isoformat(),
            "signal_close": money(float(signal_close)),
            "score": f"{score}/6",
            "range_ok": range_ok,
            "pos_in_range": pct(float(latest["pos_in_range"])),
            "range_width": pct(float(latest["range_width"])),
            "not_one_way_down": bool(signals["not_one_way_down"]),
            "buy_ready": buy_ready,
            "min_score": params["min_score"],
            "buy_q": pct(params["buy_q"]),
        },
        "plan": {
            "quote_usdt": str(quote_usdt),
            "planned_size_btc": str(planned_size),
            "buy_zone_max": str(buy_zone.quantize(Decimal("0.01"))),
            "sell_zone": str(sell_zone.quantize(Decimal("0.01"))),
            "stop_zone": str(stop_zone.quantize(Decimal("0.01"))),
            "take_profit_pct": pct(float(take_profit_pct)),
            "take_profit_price_if_enter_now": str(take_profit_price.quantize(Decimal("0.01"))),
        },
        "account": {
            "uta_usdt_available": "PUBLIC_TEST" if public_only else asset.get("available", "0"),
            "uta_usdt_locked": "PUBLIC_TEST" if public_only else asset.get("locked", "0"),
            "uta_usdt_equity": "PUBLIC_TEST" if public_only else (assets.get("data") or {}).get("usdtEquity", ""),
        },
        "blockers": blockers,
        "raw_ticker": ticker,
    }


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--quote-usdt", default="5")
    parser.add_argument("--take-profit-pct", default="0.012")
    parser.add_argument("--interval-sec", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--public-only", action="store_true", help="Use public Bitget ticker only; skip private account APIs")
    args = parser.parse_args()

    client = BitgetSpotClient()
    quote_usdt = Decimal(args.quote_usdt)
    take_profit_pct = Decimal(args.take_profit_pct)

    while True:
        snapshot = build_snapshot(
            client,
            symbol=args.symbol,
            quote_usdt=quote_usdt,
            take_profit_pct=take_profit_pct,
            public_only=args.public_only,
        )
        write_json(STATE, snapshot)
        append_jsonl(LOG, snapshot)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        if args.once:
            return
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    main()
