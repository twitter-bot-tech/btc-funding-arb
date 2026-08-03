"""CLI for Bybit V5 spot dry-run and guarded live orders."""
from __future__ import annotations

import argparse
import json
import os
import time
from decimal import Decimal
from pathlib import Path

from exchange.bybit_spot import BybitSpotClient, load_env


ROOT = Path(__file__).parent
LEDGER = ROOT / "data" / "bybit_order_ledger.jsonl"
LIVE_CONFIRM = "I_UNDERSTAND_LIVE_BYBIT_ORDER"


def write_ledger(row: dict) -> None:
    LEDGER.parent.mkdir(exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def live_allowed(args: argparse.Namespace) -> bool:
    return args.live and args.confirm == LIVE_CONFIRM and os.environ.get("BYBIT_ALLOW_LIVE", "0") == "1"


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    ticker = sub.add_parser("ticker")
    ticker.add_argument("--symbol", default="BTCUSDT")

    balance = sub.add_parser("balance")
    balance.add_argument("--coin", default="USDT")
    balance.add_argument("--account-type", default="UNIFIED")

    for name in ("buy-limit", "sell-limit"):
        p = sub.add_parser(name)
        p.add_argument("--symbol", default="BTCUSDT")
        p.add_argument("--price", required=True)
        p.add_argument("--quote-usdt", default=None, help="For buys: USDT amount converted to BTC qty")
        p.add_argument("--qty", default=None, help="Base qty, e.g. BTC amount")
        p.add_argument("--live", action="store_true")
        p.add_argument("--confirm", default="")

    args = parser.parse_args()
    client = BybitSpotClient()

    if args.cmd == "ticker":
        print(json.dumps(client.ticker(args.symbol), ensure_ascii=False, indent=2))
        return

    if args.cmd == "balance":
        print(json.dumps(client.wallet_balance(args.account_type, args.coin), ensure_ascii=False, indent=2))
        return

    side = "Buy" if args.cmd == "buy-limit" else "Sell"
    price = Decimal(args.price)
    if args.qty:
        qty = Decimal(args.qty)
    elif args.quote_usdt and side == "Buy":
        qty = client.quote_to_base_size(quote_amount=Decimal(args.quote_usdt), price=price)
    else:
        raise SystemExit("Provide --qty, or --quote-usdt for buy-limit")

    is_live = live_allowed(args)
    order_link_id = f"btcrange{int(time.time())}"
    intent = {
        "ts": int(time.time()),
        "exchange": "bybit",
        "symbol": args.symbol,
        "side": side,
        "type": "Limit",
        "price": str(price),
        "qty": str(qty),
        "order_link_id": order_link_id,
        "live": is_live,
    }

    if not is_live:
        intent["status"] = "DRY_RUN_NOT_SENT"
        write_ledger(intent)
        print(json.dumps(intent, ensure_ascii=False, indent=2))
        return

    result = client.place_limit_order(
        symbol=args.symbol,
        side=side,
        qty=qty,
        price=price,
        order_link_id=order_link_id,
    )
    intent["status"] = "SENT"
    intent["bybit_result"] = result
    write_ledger(intent)
    print(json.dumps(intent, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
