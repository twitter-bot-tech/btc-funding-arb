"""Persistent paper trading for the public BTC strategy test.

This is intentionally exchange-public-data only. It never calls private account
APIs and never places orders. GitHub Actions persists the state file between
runs by committing it back to the repository.
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from bitget_observer import bitget_last_price
from exchange.bitget_spot import BitgetSpotClient
from strategy_executor import current_signal


ROOT = Path(__file__).parent
DATA = ROOT / "data"
STATE = DATA / "paper_state.json"
TRADES = DATA / "paper_trades.csv"
DAILY = DATA / "paper_daily.csv"


def money_value(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def load_state(capital: Decimal) -> dict[str, Any]:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {
        "mode": "PAPER_TRADING",
        "capital": str(capital),
        "cash": str(capital),
        "btc": "0",
        "entry_price": None,
        "entry_ts": None,
        "entry_signal_ts": None,
        "hold_runs": 0,
        "last_action": "INIT",
        "last_reason": "initial state",
        "last_update": None,
        "realized_pnl": "0",
        "trade_count": 0,
    }


def write_state(state: dict[str, Any]) -> None:
    DATA.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def append_trade(row: dict[str, Any]) -> None:
    DATA.mkdir(exist_ok=True)
    exists = TRADES.exists()
    fields = [
        "entry_ts",
        "exit_ts",
        "entry_price",
        "exit_price",
        "qty_btc",
        "notional_usdt",
        "reason",
        "pnl",
        "ret_pct",
        "hold_runs",
        "entry_signal_ts",
    ]
    with TRADES.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fields})


def append_daily(snapshot: dict[str, Any]) -> None:
    DATA.mkdir(exist_ok=True)
    today = datetime.now().astimezone().date().isoformat()
    rows: list[dict[str, str]] = []
    if DAILY.exists():
        with DAILY.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        rows = [row for row in rows if row.get("date") != today]
    rows.append({
        "date": today,
        "equity": snapshot["equity"],
        "cash": snapshot["cash"],
        "btc": snapshot["btc"],
        "unrealized_pnl": snapshot["unrealized_pnl"],
        "realized_pnl": snapshot["realized_pnl"],
        "last_action": snapshot["last_action"],
        "last_price": snapshot["last_price"],
        "updated_at": snapshot["updated_at"],
    })
    with DAILY.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def run_once(*, capital: Decimal, quote_usdt: Decimal, fee_roundtrip: Decimal, take_profit: Decimal, stop_loss: Decimal, max_hold_runs: int) -> dict[str, Any]:
    client = BitgetSpotClient()
    ticker = client.ticker("BTCUSDT")
    live_price = bitget_last_price(ticker)
    latest, params, score, signals, range_ok, buy_ready = current_signal()
    signal_ts = pd.Timestamp(latest["ts"]).tz_convert("Asia/Singapore").isoformat()

    state = load_state(capital)
    cash = Decimal(str(state["cash"]))
    btc = Decimal(str(state["btc"]))
    entry_price = Decimal(str(state["entry_price"])) if state.get("entry_price") else None
    realized = Decimal(str(state.get("realized_pnl", "0")))
    hold_runs = int(state.get("hold_runs", 0))
    now = datetime.now().astimezone().isoformat()
    action = "WAIT"
    reason = "no position; signal not ready"

    if btc > 0 and entry_price is not None:
        hold_runs += 1
        pnl_pct = live_price / entry_price - Decimal("1")
        should_exit = False
        reason = "holding"
        if pnl_pct >= take_profit:
            should_exit = True
            reason = "TAKE_PROFIT"
        elif pnl_pct <= -stop_loss:
            should_exit = True
            reason = "STOP_LOSS"
        elif float(latest["pos_in_range"]) >= params["sell_q"]:
            should_exit = True
            reason = "SELL_RANGE"
        elif hold_runs >= max_hold_runs:
            should_exit = True
            reason = "MAX_HOLD"

        if should_exit:
            gross = btc * live_price
            fee = gross * fee_roundtrip / Decimal("2")
            cash += gross - fee
            entry_notional = btc * entry_price
            pnl = cash - entry_notional
            realized += pnl
            append_trade({
                "entry_ts": state.get("entry_ts"),
                "exit_ts": now,
                "entry_price": money_value(entry_price),
                "exit_price": money_value(live_price),
                "qty_btc": str(btc),
                "notional_usdt": money_value(entry_notional),
                "reason": reason,
                "pnl": money_value(pnl),
                "ret_pct": str((pnl / entry_notional * Decimal("100")).quantize(Decimal("0.0001"))),
                "hold_runs": hold_runs,
                "entry_signal_ts": state.get("entry_signal_ts"),
            })
            btc = Decimal("0")
            entry_price = None
            hold_runs = 0
            action = "SELL"
        else:
            action = "HOLD"

    if btc == 0 and action != "SELL":
        blockers: list[str] = []
        if not range_ok:
            blockers.append("price_not_in_buy_zone")
        if score < params["min_score"]:
            blockers.append("score_too_low")
        if not signals["not_one_way_down"]:
            blockers.append("one_way_down")
        if cash < quote_usdt:
            blockers.append("cash_too_low")

        if buy_ready and not blockers:
            notional = min(quote_usdt, cash)
            fee = notional * fee_roundtrip / Decimal("2")
            btc = (notional - fee) / live_price
            cash -= notional
            entry_price = live_price
            hold_runs = 0
            action = "BUY"
            reason = "BUY_CONFLUENCE"
            state["entry_ts"] = now
            state["entry_signal_ts"] = signal_ts
        else:
            reason = ",".join(blockers) if blockers else "signal_not_ready"

    equity = cash + btc * live_price
    unrealized = Decimal("0") if not entry_price or btc == 0 else btc * (live_price - entry_price)
    state.update({
        "mode": "PAPER_TRADING",
        "capital": str(capital),
        "cash": str(cash),
        "btc": str(btc),
        "entry_price": str(entry_price) if entry_price else None,
        "hold_runs": hold_runs,
        "last_action": action,
        "last_reason": reason,
        "last_update": now,
        "last_price": str(live_price),
        "equity": money_value(equity),
        "unrealized_pnl": money_value(unrealized),
        "realized_pnl": money_value(realized),
        "trade_count": int(state.get("trade_count", 0)) + (1 if action == "SELL" else 0),
        "signal": {
            "source_ts_sgt": signal_ts,
            "score": f"{score}/6",
            "range_ok": range_ok,
            "pos_in_range": f"{float(latest['pos_in_range']) * 100:.2f}%",
            "range_width": f"{float(latest['range_width']) * 100:.2f}%",
            "buy_ready": buy_ready,
            "not_one_way_down": bool(signals["not_one_way_down"]),
        },
    })
    if btc == 0:
        state["entry_ts"] = None
        state["entry_signal_ts"] = None

    write_state(state)
    append_daily({
        "equity": state["equity"],
        "cash": money_value(cash),
        "btc": str(btc),
        "unrealized_pnl": state["unrealized_pnl"],
        "realized_pnl": state["realized_pnl"],
        "last_action": action,
        "last_price": str(live_price),
        "updated_at": now,
    })
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capital", default="10000")
    parser.add_argument("--quote-usdt", default="50")
    parser.add_argument("--fee-roundtrip", default="0.0012")
    parser.add_argument("--take-profit", default="0.012")
    parser.add_argument("--stop-loss", default="0.01")
    parser.add_argument("--max-hold-runs", type=int, default=16)
    args = parser.parse_args()
    run_once(
        capital=Decimal(args.capital),
        quote_usdt=Decimal(args.quote_usdt),
        fee_roundtrip=Decimal(args.fee_roundtrip),
        take_profit=Decimal(args.take_profit),
        stop_loss=Decimal(args.stop_loss),
        max_hold_runs=args.max_hold_runs,
    )


if __name__ == "__main__":
    main()
