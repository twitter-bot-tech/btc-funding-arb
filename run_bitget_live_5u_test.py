"""Guarded Bitget live test loop capped at 5 USDT."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from bitget_observer import bitget_last_price
from exchange.bitget_spot import BitgetSpotClient, load_env
from strategy_executor import current_signal


ROOT = Path(__file__).parent
DATA = ROOT / "data"
STATE = DATA / "bitget_live_5u_state.json"
HEALTH = DATA / "bitget_live_5u_health.json"
LEDGER = DATA / "bitget_live_5u_ledger.jsonl"
CONFIRM = "I_UNDERSTAND_5U_LIVE_BITGET_TEST"
MAX_QUOTE = Decimal("5")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    DATA.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_ledger(payload: dict[str, Any]) -> None:
    DATA.mkdir(exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_state() -> dict[str, Any]:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"mode": "LIVE_5U_TEST", "status": "FLAT", "orders_sent": 0}


def asset_amount(payload: dict[str, Any], coin: str, key: str = "available") -> Decimal:
    data = payload.get("data") if isinstance(payload, dict) else {}
    assets = data.get("assets") if isinstance(data, dict) else []
    for row in assets or []:
        if row.get("coin") == coin:
            return Decimal(str(row.get(key) or "0"))
    return Decimal("0")


def open_order_count(payload: dict[str, Any]) -> int:
    data = payload.get("data")
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("orderList", "orders", "list"):
            if isinstance(data.get(key), list):
                return len(data[key])
    return 0


def guard_enabled(confirm: str) -> bool:
    return (
        os.environ.get("LIVE_5U_TEST_ALLOW", "0") == "1"
        and os.environ.get("BITGET_ALLOW_LIVE", "0") == "1"
        and confirm == CONFIRM
    )


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    load_env()
    if args.quote_usdt <= 0 or args.quote_usdt > MAX_QUOTE:
        raise SystemExit(f"--quote-usdt must be > 0 and <= {MAX_QUOTE}")

    client = BitgetSpotClient()
    state = load_state()
    now = datetime.now().astimezone().isoformat()
    latest, params, score, signals, range_ok, buy_ready = current_signal()
    ticker = client.ticker(args.symbol)
    live_price = bitget_last_price(ticker)
    btc_available = asset_amount(client.assets("BTC"), "BTC")
    usdt_available = asset_amount(client.assets("USDT"), "USDT")
    opens = open_order_count(client.open_orders(args.symbol))
    enabled = guard_enabled(args.confirm)
    action = "WAIT"
    reason = "signal_not_ready"
    result: dict[str, Any] | None = None

    if not enabled:
        reason = "live_guard_disabled"
    elif opens > 0:
        reason = "open_order_exists"
    elif btc_available > 0:
        entry_price = Decimal(str(state.get("entry_price") or live_price))
        pnl_pct = live_price / entry_price - Decimal("1")
        hold_runs = int(state.get("hold_runs", 0)) + 1
        should_sell = False
        reason = "holding"
        if pnl_pct >= args.take_profit:
            should_sell = True
            reason = "TAKE_PROFIT"
        elif pnl_pct <= -args.stop_loss:
            should_sell = True
            reason = "STOP_LOSS"
        elif float(latest["pos_in_range"]) >= params["sell_q"]:
            should_sell = True
            reason = "SELL_RANGE"
        elif hold_runs >= args.max_hold_runs:
            should_sell = True
            reason = "MAX_HOLD"

        state["hold_runs"] = hold_runs
        if should_sell:
            client_oid = f"btg5us{int(time.time())}"
            price = live_price.quantize(Decimal("0.1"))
            result = client.place_limit_order(
                symbol=args.symbol,
                side="sell",
                size=btc_available,
                price=price,
                client_oid=client_oid,
            )
            action = "SELL_LIMIT_SENT"
            state.update({"status": "SELL_ORDER_SENT", "last_sell_oid": client_oid})
    elif buy_ready and usdt_available >= args.quote_usdt:
        client_oid = f"btg5ub{int(time.time())}"
        price = live_price.quantize(Decimal("0.1"))
        size = client.quote_to_base_size(quote_amount=args.quote_usdt, price=price)
        result = client.place_limit_order(
            symbol=args.symbol,
            side="buy",
            size=size,
            price=price,
            client_oid=client_oid,
        )
        action = "BUY_LIMIT_SENT"
        reason = "BUY_CONFLUENCE"
        state.update({
            "status": "BUY_ORDER_SENT",
            "entry_price": str(price),
            "entry_signal_ts": str(latest["ts"]),
            "hold_runs": 0,
            "last_buy_oid": client_oid,
            "orders_sent": int(state.get("orders_sent", 0)) + 1,
        })
    elif usdt_available < args.quote_usdt:
        reason = "insufficient_usdt"
    elif not range_ok:
        reason = "price_not_in_buy_zone"
    elif score < params["min_score"]:
        reason = "score_too_low"
    elif not signals["not_one_way_down"]:
        reason = "one_way_down"

    snapshot = {
        "mode": "LIVE_5U_TEST",
        "updated_at": now,
        "enabled": enabled,
        "action": action,
        "reason": reason,
        "symbol": args.symbol,
        "quote_usdt": str(args.quote_usdt),
        "live_price": str(live_price),
        "btc_available": str(btc_available),
        "usdt_available": str(usdt_available),
        "open_orders": opens,
        "signal": {
            "source_ts_sgt": str(latest["ts_sgt"]),
            "score": f"{score}/6",
            "range_ok": range_ok,
            "pos_in_range": f"{float(latest['pos_in_range']) * 100:.2f}%",
            "range_width": f"{float(latest['range_width']) * 100:.2f}%",
            "buy_ready": buy_ready,
            "not_one_way_down": bool(signals["not_one_way_down"]),
        },
        "exchange_result": result,
        "state": state,
    }
    state.update({
        "updated_at": now,
        "last_action": action,
        "last_reason": reason,
        "live_price": str(live_price),
        "btc_available": str(btc_available),
        "usdt_available": str(usdt_available),
        "open_orders": opens,
    })
    write_json(STATE, state)
    write_json(HEALTH, {"status": "ok", "last_ok_at": now, "action": action, "reason": reason})
    append_ledger(snapshot)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval-sec", type=int, default=15 * 60)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--quote-usdt", type=Decimal, default=Decimal("5"))
    parser.add_argument("--take-profit", type=Decimal, default=Decimal("0.012"))
    parser.add_argument("--stop-loss", type=Decimal, default=Decimal("0.01"))
    parser.add_argument("--max-hold-runs", type=int, default=16)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()

    failures = 0
    while True:
        try:
            run_once(args)
            failures = 0
        except Exception as exc:  # noqa: BLE001
            failures += 1
            now = datetime.now().astimezone().isoformat()
            write_json(HEALTH, {
                "status": "error",
                "last_error_at": now,
                "consecutive_failures": failures,
                "error": repr(exc),
            })
            print(now, "live 5u test failed", repr(exc), flush=True)
        if args.once:
            return
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    main()
