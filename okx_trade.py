"""CLI for OKX spot manual/dry-run orders.

Examples:
  python okx_trade.py ticker
  python okx_trade.py balance --ccy USDT
  python okx_trade.py buy-limit --quote-usdt 50 --price 63000
  python okx_trade.py buy-limit --quote-usdt 50 --price 63000 --live --confirm I_UNDERSTAND_LIVE_OKX_ORDER
"""
from __future__ import annotations

import argparse
import json
import os
import time
from decimal import Decimal
from pathlib import Path

from exchange.okx_spot import OkxSpotClient, load_env


ROOT = Path(__file__).parent
LEDGER = ROOT / "data" / "okx_order_ledger.jsonl"
LIVE_CONFIRM = "I_UNDERSTAND_LIVE_OKX_ORDER"


def write_ledger(row: dict) -> None:
    LEDGER.parent.mkdir(exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def live_allowed(args: argparse.Namespace) -> bool:
    return args.live and args.confirm == LIVE_CONFIRM and os.environ.get("OKX_ALLOW_LIVE", "0") == "1"


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    ticker = sub.add_parser("ticker")
    ticker.add_argument("--inst-id", default="BTC-USDT")

    balance = sub.add_parser("balance")
    balance.add_argument("--ccy", default=None)

    for name in ("buy-limit", "sell-limit"):
        p = sub.add_parser(name)
        p.add_argument("--inst-id", default="BTC-USDT")
        p.add_argument("--price", required=True)
        p.add_argument("--quote-usdt", type=str, default=None, help="For buys: USDT amount converted to BTC size")
        p.add_argument("--size", type=str, default=None, help="Base size, e.g. BTC amount")
        p.add_argument("--live", action="store_true")
        p.add_argument("--confirm", default="")

    args = parser.parse_args()
    client = OkxSpotClient()

    if args.cmd == "ticker":
        print(json.dumps(client.ticker(args.inst_id), ensure_ascii=False, indent=2))
        return

    if args.cmd == "balance":
        print(json.dumps(client.balance(args.ccy), ensure_ascii=False, indent=2))
        return

    side = "buy" if args.cmd == "buy-limit" else "sell"
    price = Decimal(args.price)
    is_live = live_allowed(args)
    if args.size:
        size = Decimal(args.size)
    elif args.quote_usdt and side == "buy":
        size = client.quote_to_base_size(
            inst_id=args.inst_id,
            quote_amount=Decimal(args.quote_usdt),
            price=price,
            use_default_instrument=not is_live,
        )
    else:
        raise SystemExit("Provide --size, or --quote-usdt for buy-limit")

    client_order_id = f"btcrange{int(time.time())}"
    intent = {
        "ts": int(time.time()),
        "exchange": "okx",
        "inst_id": args.inst_id,
        "side": side,
        "type": "limit",
        "price": str(price),
        "size": str(size),
        "client_order_id": client_order_id,
        "live": is_live,
    }

    if not is_live:
        intent["status"] = "DRY_RUN_NOT_SENT"
        write_ledger(intent)
        print(json.dumps(intent, ensure_ascii=False, indent=2))
        return

    result = client.place_limit_order(
        inst_id=args.inst_id,
        side=side,
        size=size,
        price=price,
        client_order_id=client_order_id,
    )
    intent["status"] = "SENT"
    intent["okx_result"] = result
    write_ledger(intent)
    print(json.dumps(intent, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
