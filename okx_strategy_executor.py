"""Turn the current BTC confluence signal into an OKX spot order intent.

Default behavior is dry-run. Real orders require:
  1. --live
  2. --confirm I_UNDERSTAND_LIVE_OKX_ORDER
  3. OKX_ALLOW_LIVE=1 in the environment
"""
from __future__ import annotations

import argparse
import json
import os
import time
from decimal import Decimal
from pathlib import Path

import pandas as pd

from backtest_confluence_range import add_features, load, score_row
from exchange.okx_spot import OkxSpotClient, load_env


ROOT = Path(__file__).parent
DATA = ROOT / "data"
LEDGER = DATA / "okx_order_ledger.jsonl"
LIVE_CONFIRM = "I_UNDERSTAND_LIVE_OKX_ORDER"


def pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def money(v: float) -> str:
    return f"${v:,.2f}"


def write_ledger(row: dict) -> None:
    DATA.mkdir(exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def live_allowed(args: argparse.Namespace) -> bool:
    return args.live and args.confirm == LIVE_CONFIRM and os.environ.get("OKX_ALLOW_LIVE", "0") == "1"


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--inst-id", default="BTC-USDT")
    parser.add_argument("--quote-usdt", default=os.environ.get("OKX_DEFAULT_ORDER_USDT", "50"))
    parser.add_argument("--max-order-usdt", default=os.environ.get("OKX_MAX_ORDER_USDT", "100"))
    parser.add_argument("--limit-offset-bps", default="0", help="Buy below close by this many bps")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    quote_usdt = Decimal(args.quote_usdt)
    max_order = Decimal(args.max_order_usdt)
    if quote_usdt <= 0 or quote_usdt > max_order:
        raise SystemExit(f"quote_usdt must be > 0 and <= max_order_usdt ({max_order})")

    summary = json.loads((DATA / "confluence_range_summary.json").read_text(encoding="utf-8"))
    params = summary["selected_params"]
    market = add_features(load()).dropna(subset=["range_hi", "range_lo", "pos_in_range", "range_width"])
    latest = market.iloc[-1]
    score, signals = score_row(latest, rsi_max=params["rsi_max"])
    range_ok = float(latest["range_width"]) >= params["min_width"] and float(latest["pos_in_range"]) <= params["buy_q"]
    buy_ready = range_ok and score >= params["min_score"] and bool(signals["not_one_way_down"])

    close_price = Decimal(str(float(latest["close"])))
    limit_price = close_price * (Decimal("1") - Decimal(args.limit_offset_bps) / Decimal("10000"))
    limit_price = limit_price.quantize(Decimal("0.1"))

    is_live = live_allowed(args)
    client = OkxSpotClient()
    size = client.quote_to_base_size(
        inst_id=args.inst_id,
        quote_amount=quote_usdt,
        price=limit_price,
        use_default_instrument=not is_live,
    )

    intent = {
        "ts": int(time.time()),
        "exchange": "okx",
        "inst_id": args.inst_id,
        "mode": "strategy_confluence_range",
        "side": "buy",
        "type": "limit",
        "price": str(limit_price),
        "size": str(size),
        "quote_usdt": str(quote_usdt),
        "live": is_live,
        "signal": {
            "source_ts_sgt": pd.Timestamp(latest["ts"]).tz_convert("Asia/Singapore").isoformat(),
            "close": money(float(latest["close"])),
            "score": f"{score}/6",
            "range_ok": range_ok,
            "pos_in_range": pct(float(latest["pos_in_range"])),
            "range_width": pct(float(latest["range_width"])),
            "not_one_way_down": bool(signals["not_one_way_down"]),
            "buy_ready": buy_ready,
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

    intent["client_order_id"] = f"btcrange{int(time.time())}"
    result = client.place_limit_order(
        inst_id=args.inst_id,
        side="buy",
        size=size,
        price=limit_price,
        client_order_id=intent["client_order_id"],
    )
    intent["status"] = "SENT"
    intent["okx_result"] = result
    write_ledger(intent)
    print(json.dumps(intent, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
