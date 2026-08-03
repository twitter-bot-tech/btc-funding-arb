"""CLI for Bitget spot dry-run and guarded live orders."""
from __future__ import annotations

import argparse
import json
import os
import time
from decimal import Decimal
from pathlib import Path

from exchange.bitget_spot import BitgetSpotClient, load_env


ROOT = Path(__file__).parent
LEDGER = ROOT / "data" / "bitget_order_ledger.jsonl"
LIVE_CONFIRM = "I_UNDERSTAND_LIVE_BITGET_ORDER"
TRANSFER_CONFIRM = "I_UNDERSTAND_BITGET_TRANSFER"


def write_ledger(row: dict) -> None:
    LEDGER.parent.mkdir(exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def live_allowed(args: argparse.Namespace) -> bool:
    return args.live and args.confirm == LIVE_CONFIRM and os.environ.get("BITGET_ALLOW_LIVE", "0") == "1"


def transfer_allowed(args: argparse.Namespace) -> bool:
    return args.live and args.confirm == TRANSFER_CONFIRM


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    ticker = sub.add_parser("ticker")
    ticker.add_argument("--symbol", default="BTCUSDT")

    assets = sub.add_parser("assets")
    assets.add_argument("--coin", default="USDT")

    all_balance = sub.add_parser("all-balance")

    funding_assets = sub.add_parser("funding-assets")
    funding_assets.add_argument("--coin", default="USDT")

    transfer = sub.add_parser("transfer")
    transfer.add_argument("--from-type", required=True)
    transfer.add_argument("--to-type", required=True)
    transfer.add_argument("--coin", default="USDT")
    transfer.add_argument("--amount", required=True)
    transfer.add_argument("--live", action="store_true")
    transfer.add_argument("--confirm", default="")

    max_open = sub.add_parser("max-open")
    max_open.add_argument("--symbol", default="BTCUSDT")
    max_open.add_argument("--side", default="buy")
    max_open.add_argument("--order-type", default="limit")
    max_open.add_argument("--price", default=None)

    account_info = sub.add_parser("account-info")

    order_info = sub.add_parser("order-info")
    order_info.add_argument("--order-id", default=None)
    order_info.add_argument("--client-oid", default=None)

    open_orders = sub.add_parser("open-orders")
    open_orders.add_argument("--symbol", default="BTCUSDT")

    cancel = sub.add_parser("cancel")
    cancel.add_argument("--symbol", default="BTCUSDT")
    cancel.add_argument("--order-id", default=None)
    cancel.add_argument("--client-oid", default=None)

    for name in ("buy-limit", "sell-limit"):
        p = sub.add_parser(name)
        p.add_argument("--symbol", default="BTCUSDT")
        p.add_argument("--price", required=True)
        p.add_argument("--quote-usdt", default=None, help="For buys: USDT amount converted to BTC size")
        p.add_argument("--size", default=None, help="Base size, e.g. BTC amount")
        p.add_argument("--live", action="store_true")
        p.add_argument("--confirm", default="")

    args = parser.parse_args()
    client = BitgetSpotClient()

    if args.cmd == "ticker":
        print(json.dumps(client.ticker(args.symbol), ensure_ascii=False, indent=2))
        return

    if args.cmd == "assets":
        print(json.dumps(client.assets(coin=args.coin), ensure_ascii=False, indent=2))
        return

    if args.cmd == "all-balance":
        print(json.dumps(client.all_account_balance(), ensure_ascii=False, indent=2))
        return

    if args.cmd == "funding-assets":
        print(json.dumps(client.funding_assets(coin=args.coin), ensure_ascii=False, indent=2))
        return

    if args.cmd == "transfer":
        client_oid = f"btgtransfer{int(time.time())}"
        intent = {
            "ts": int(time.time()),
            "exchange": "bitget",
            "action": "transfer",
            "from_type": args.from_type,
            "to_type": args.to_type,
            "coin": args.coin,
            "amount": args.amount,
            "client_oid": client_oid,
            "live": transfer_allowed(args),
        }
        if not transfer_allowed(args):
            intent["status"] = "DRY_RUN_NOT_SENT"
            print(json.dumps(intent, ensure_ascii=False, indent=2))
            return
        result = client.transfer(
            from_type=args.from_type,
            to_type=args.to_type,
            coin=args.coin,
            amount=Decimal(args.amount),
            client_oid=client_oid,
        )
        intent["status"] = "SENT"
        intent["bitget_result"] = result
        print(json.dumps(intent, ensure_ascii=False, indent=2))
        return

    if args.cmd == "max-open":
        price = Decimal(args.price) if args.price else None
        print(json.dumps(client.max_open_available(symbol=args.symbol, side=args.side, order_type=args.order_type, price=price), ensure_ascii=False, indent=2))
        return

    if args.cmd == "account-info":
        print(json.dumps(client.account_info(), ensure_ascii=False, indent=2))
        return

    if args.cmd == "order-info":
        print(json.dumps(client.order_info(order_id=args.order_id, client_oid=args.client_oid), ensure_ascii=False, indent=2))
        return

    if args.cmd == "open-orders":
        print(json.dumps(client.open_orders(args.symbol), ensure_ascii=False, indent=2))
        return

    if args.cmd == "cancel":
        print(json.dumps(client.cancel_order(order_id=args.order_id, client_oid=args.client_oid, symbol=args.symbol), ensure_ascii=False, indent=2))
        return

    side = "buy" if args.cmd == "buy-limit" else "sell"
    price = Decimal(args.price)
    if args.size:
        size = Decimal(args.size)
    elif args.quote_usdt and side == "buy":
        size = client.quote_to_base_size(quote_amount=Decimal(args.quote_usdt), price=price)
    else:
        raise SystemExit("Provide --size, or --quote-usdt for buy-limit")

    is_live = live_allowed(args)
    client_oid = f"btcrange{int(time.time())}"
    intent = {
        "ts": int(time.time()),
        "exchange": "bitget",
        "symbol": args.symbol,
        "side": side,
        "type": "limit",
        "price": str(price),
        "size": str(size),
        "client_oid": client_oid,
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
        size=size,
        price=price,
        client_oid=client_oid,
    )
    intent["status"] = "SENT"
    intent["bitget_result"] = result
    write_ledger(intent)
    print(json.dumps(intent, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
