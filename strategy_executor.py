"""Unified strategy executor for OKX, Bybit, Gate, and Bitget spot.

Default behavior is dry-run. Live orders require:
  1. --live
  2. exchange-specific *_ALLOW_LIVE=1
  3. --confirm I_UNDERSTAND_LIVE_<EXCHANGE>_ORDER
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from backtest_confluence_range import add_features, load, score_row
from exchange.bitget_spot import BitgetSpotClient, load_env as load_bitget_env
from exchange.bybit_spot import BybitSpotClient, load_env as load_bybit_env
from exchange.gate_spot import GateSpotClient, load_env as load_gate_env
from exchange.okx_spot import OkxSpotClient, load_env as load_okx_env


ROOT = Path(__file__).parent
DATA = ROOT / "data"
LEDGER = DATA / "strategy_order_ledger.jsonl"


@dataclass(frozen=True)
class ExchangeSpec:
    name: str
    symbol: str
    allow_env: str
    confirm: str
    default_order_env: str
    max_order_env: str


SPECS = {
    "okx": ExchangeSpec("okx", "BTC-USDT", "OKX_ALLOW_LIVE", "I_UNDERSTAND_LIVE_OKX_ORDER", "OKX_DEFAULT_ORDER_USDT", "OKX_MAX_ORDER_USDT"),
    "bybit": ExchangeSpec("bybit", "BTCUSDT", "BYBIT_ALLOW_LIVE", "I_UNDERSTAND_LIVE_BYBIT_ORDER", "BYBIT_DEFAULT_ORDER_USDT", "BYBIT_MAX_ORDER_USDT"),
    "gate": ExchangeSpec("gate", "BTC_USDT", "GATE_ALLOW_LIVE", "I_UNDERSTAND_LIVE_GATE_ORDER", "GATE_DEFAULT_ORDER_USDT", "GATE_MAX_ORDER_USDT"),
    "bitget": ExchangeSpec("bitget", "BTCUSDT", "BITGET_ALLOW_LIVE", "I_UNDERSTAND_LIVE_BITGET_ORDER", "BITGET_DEFAULT_ORDER_USDT", "BITGET_MAX_ORDER_USDT"),
}


def pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def money(v: float) -> str:
    return f"${v:,.2f}"


def write_ledger(row: dict[str, Any]) -> None:
    DATA.mkdir(exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_all_env() -> None:
    load_okx_env()
    load_bybit_env()
    load_gate_env()
    load_bitget_env()


def live_allowed(args: argparse.Namespace, spec: ExchangeSpec) -> bool:
    return args.live and args.confirm == spec.confirm and os.environ.get(spec.allow_env, "0") == "1"


def current_signal() -> tuple[pd.Series, dict[str, Any], int, dict[str, bool], bool, bool]:
    summary = json.loads((DATA / "confluence_range_summary.json").read_text(encoding="utf-8"))
    params = summary["selected_params"]
    market = add_features(load()).dropna(subset=["range_hi", "range_lo", "pos_in_range", "range_width"])
    latest = market.iloc[-1]
    score, signals = score_row(latest, rsi_max=params["rsi_max"])
    range_ok = float(latest["range_width"]) >= params["min_width"] and float(latest["pos_in_range"]) <= params["buy_q"]
    buy_ready = range_ok and score >= params["min_score"] and bool(signals["not_one_way_down"])
    return latest, params, score, signals, range_ok, buy_ready


def size_for(exchange: str, quote_usdt: Decimal, price: Decimal, *, live: bool, symbol: str) -> Decimal:
    if exchange == "okx":
        return OkxSpotClient().quote_to_base_size(
            inst_id=symbol,
            quote_amount=quote_usdt,
            price=price,
            use_default_instrument=not live,
        )
    if exchange == "bybit":
        return BybitSpotClient().quote_to_base_size(quote_amount=quote_usdt, price=price)
    if exchange == "gate":
        return GateSpotClient().quote_to_base_size(quote_amount=quote_usdt, price=price)
    if exchange == "bitget":
        return BitgetSpotClient().quote_to_base_size(quote_amount=quote_usdt, price=price)
    raise ValueError(f"Unsupported exchange: {exchange}")


def place_live(exchange: str, *, symbol: str, size: Decimal, price: Decimal, client_id: str) -> dict[str, Any]:
    if exchange == "okx":
        return OkxSpotClient().place_limit_order(inst_id=symbol, side="buy", size=size, price=price, client_order_id=client_id)
    if exchange == "bybit":
        return BybitSpotClient().place_limit_order(symbol=symbol, side="Buy", qty=size, price=price, order_link_id=client_id)
    if exchange == "gate":
        return GateSpotClient().place_limit_order(currency_pair=symbol, side="buy", amount=size, price=price, text=f"t-{client_id}")
    if exchange == "bitget":
        return BitgetSpotClient().place_limit_order(symbol=symbol, side="buy", size=size, price=price, client_oid=client_id)
    raise ValueError(f"Unsupported exchange: {exchange}")


def main() -> None:
    load_all_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--exchange", choices=sorted(SPECS), default="bitget")
    parser.add_argument("--symbol", default=None, help="Override exchange-specific BTC symbol")
    parser.add_argument("--quote-usdt", default=None)
    parser.add_argument("--max-order-usdt", default=None)
    parser.add_argument("--limit-offset-bps", default="0", help="Buy below signal close by this many bps")
    parser.add_argument("--ignore-signal", action="store_true", help="For connectivity test only; still dry-run unless live guards pass")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    spec = SPECS[args.exchange]
    symbol = args.symbol or spec.symbol
    quote_usdt = Decimal(args.quote_usdt or os.environ.get(spec.default_order_env, "50"))
    max_order = Decimal(args.max_order_usdt or os.environ.get(spec.max_order_env, "100"))
    if quote_usdt <= 0 or quote_usdt > max_order:
        raise SystemExit(f"quote_usdt must be > 0 and <= max_order_usdt ({max_order})")

    latest, params, score, signals, range_ok, buy_ready = current_signal()
    if args.ignore_signal:
        buy_ready = True

    close_price = Decimal(str(float(latest["close"])))
    price = close_price * (Decimal("1") - Decimal(args.limit_offset_bps) / Decimal("10000"))
    price = price.quantize(Decimal("0.1"))
    is_live = live_allowed(args, spec)
    size = size_for(args.exchange, quote_usdt, price, live=is_live, symbol=symbol)
    client_id = f"btcrange{int(time.time())}"

    intent = {
        "ts": int(time.time()),
        "exchange": args.exchange,
        "symbol": symbol,
        "mode": "strategy_confluence_range",
        "side": "buy",
        "type": "limit",
        "price": str(price),
        "size": str(size),
        "quote_usdt": str(quote_usdt),
        "client_id": client_id,
        "live": is_live,
        "ignore_signal": args.ignore_signal,
        "live_guard": {
            "allow_env": spec.allow_env,
            "allow_env_enabled": os.environ.get(spec.allow_env, "0") == "1",
            "confirm_required": spec.confirm,
            "confirm_passed": args.confirm == spec.confirm,
        },
        "signal": {
            "source_ts_sgt": pd.Timestamp(latest["ts"]).tz_convert("Asia/Singapore").isoformat(),
            "close": money(float(latest["close"])),
            "score": f"{score}/6",
            "range_ok": range_ok,
            "pos_in_range": pct(float(latest["pos_in_range"])),
            "range_width": pct(float(latest["range_width"])),
            "not_one_way_down": bool(signals["not_one_way_down"]),
            "buy_ready": buy_ready,
            "min_score": params["min_score"],
            "buy_q": pct(params["buy_q"]),
        },
    }

    if not buy_ready:
        intent["status"] = "BLOCKED_BY_SIGNAL"
        write_ledger(intent)
        print(json.dumps(intent, ensure_ascii=False, indent=2))
        return

    if not is_live:
        intent["status"] = "DRY_RUN_NOT_SENT"
        write_ledger(intent)
        print(json.dumps(intent, ensure_ascii=False, indent=2))
        return

    result = place_live(args.exchange, symbol=symbol, size=size, price=price, client_id=client_id)
    intent["status"] = "SENT"
    intent["exchange_result"] = result
    write_ledger(intent)
    print(json.dumps(intent, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
