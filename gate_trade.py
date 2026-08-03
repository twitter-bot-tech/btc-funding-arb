"""CLI for Gate spot dry-run and guarded live orders."""
from __future__ import annotations

import argparse
import json
import os
import time
from decimal import Decimal
from pathlib import Path

from exchange.gate_spot import GateSpotClient, load_env


ROOT = Path(__file__).parent
LEDGER = ROOT / "data" / "gate_order_ledger.jsonl"
LIVE_CONFIRM = "I_UNDERSTAND_LIVE_GATE_ORDER"


def write_ledger(row: dict) -> None:
    LEDGER.parent.mkdir(exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def live_allowed(args: argparse.Namespace) -> bool:
    return args.live and args.confirm == LIVE_CONFIRM and os.environ.get("GATE_ALLOW_LIVE", "0") == "1"


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    ticker = sub.add_parser("ticker")
    ticker.add_argument("--currency-pair", default="BTC_USDT")

    balance = sub.add_parser("balance")
    balance.add_argument("--currency", default="USDT")

    for name in ("buy-limit", "sell-limit"):
        p = sub.add_parser(name)
        p.add_argument("--currency-pair", default="BTC_USDT")
        p.add_argument("--price", required=True)
        p.add_argument("--quote-usdt", default=None, help="For buys: USDT amount converted to BTC amount")
        p.add_argument("--amount", default=None, help="Base amount, e.g. BTC amount")
        p.add_argument("--live", action="store_true")
        p.add_argument("--confirm", default="")

    args = parser.parse_args()
    client = GateSpotClient()

    if args.cmd == "ticker":
        print(json.dumps(client.ticker(args.currency_pair), ensure_ascii=False, indent=2))
        return

    if args.cmd == "balance":
        print(json.dumps(client.balances(args.currency), ensure_ascii=False, indent=2))
        return

    side = "buy" if args.cmd == "buy-limit" else "sell"
    price = Decimal(args.price)
    if args.amount:
        amount = Decimal(args.amount)
    elif args.quote_usdt and side == "buy":
        amount = client.quote_to_base_size(quote_amount=Decimal(args.quote_usdt), price=price)
    else:
        raise SystemExit("Provide --amount, or --quote-usdt for buy-limit")

    is_live = live_allowed(args)
    text = f"t-btcrange{int(time.time())}"
    intent = {
        "ts": int(time.time()),
        "exchange": "gate",
        "currency_pair": args.currency_pair,
        "side": side,
        "type": "limit",
        "price": str(price),
        "amount": str(amount),
        "text": text,
        "live": is_live,
    }

    if not is_live:
        intent["status"] = "DRY_RUN_NOT_SENT"
        write_ledger(intent)
        print(json.dumps(intent, ensure_ascii=False, indent=2))
        return

    result = client.place_limit_order(
        currency_pair=args.currency_pair,
        side=side,
        amount=amount,
        price=price,
        text=text,
    )
    intent["status"] = "SENT"
    intent["gate_result"] = result
    write_ledger(intent)
    print(json.dumps(intent, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
