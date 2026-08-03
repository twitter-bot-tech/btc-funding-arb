"""Donchian(20) paper trader for BTC spot.

Runs once per invocation:
  1. Fetch latest 25 daily BTCUSDT candles from Binance (no auth needed)
  2. Compute 20-day high/low (excluding today's bar, so signal is stable)
  3. If flat + today's close > prev_20d_high   -> ENTER LONG
     If long + today's close < prev_20d_low    -> EXIT
     Otherwise: HOLD
  4. Update state file, log to ledger, optionally send Telegram alert

Cron it: every day at 00:05 UTC (5 min after daily candle close).

State file: state.json
Ledger:    ledger.csv (one row per signal day with realized/unrealized P&L)
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
LEDGER_FILE = ROOT / "ledger.csv"

SYMBOL = "BTCUSDT"
WINDOW = 20
CAPITAL = float(os.environ.get("CAPITAL", "100000"))     # paper capital
FEE_BPS = 10
SLIP_BPS = 5
COST_PER_LEG = (FEE_BPS + SLIP_BPS) / 10000

_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line: continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def fetch_daily(limit=25):
    """Robust fetch with retry on DNS/connection errors (Mac wake race condition)."""
    url = "https://api.binance.com/api/v3/klines"
    last_exc = None
    for i in range(8):
        try:
            r = requests.get(url, params={"symbol": SYMBOL, "interval": "1d", "limit": limit}, timeout=20)
            r.raise_for_status()
            rows = r.json()
            break
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.SSLError) as e:
            last_exc = e
            time.sleep(min(60, 2 ** (i + 1)))
    else:
        raise last_exc
    out = []
    for k in rows:
        out.append({
            "openTime": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "closeTime": datetime.fromtimestamp(k[6] / 1000, tz=timezone.utc),
        })
    return out


def load_state():
    if not STATE_FILE.exists():
        return {
            "position": "flat",      # 'flat' or 'long'
            "entry_price": None,
            "entry_date": None,
            "btc_held": 0.0,
            "cash": CAPITAL,
            "realized_pnl_total": 0.0,
            "trades": 0,
        }
    return json.loads(STATE_FILE.read_text())


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def append_ledger(row):
    headers = ["timestamp", "action", "price", "btc", "cash", "equity",
               "realized_pnl", "unrealized_pnl", "note"]
    new_file = not LEDGER_FILE.exists()
    with LEDGER_FILE.open("a") as f:
        if new_file:
            f.write(",".join(headers) + "\n")
        f.write(",".join(str(row.get(h, "")) for h in headers) + "\n")


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        return r.ok
    except Exception:
        return False


def main():
    candles = fetch_daily(limit=WINDOW + 5)
    if len(candles) < WINDOW + 1:
        print(f"Not enough candles: {len(candles)}")
        return

    # Use last CLOSED daily bar as "today's signal bar".
    # Binance's daily klines: last entry is current (possibly unfinished) UTC day.
    # closeTime in the future -> still open. Exclude it.
    now_ms = time.time()
    closed = [c for c in candles if c["closeTime"].timestamp() < now_ms]
    if not closed:
        print("No closed bars yet")
        return
    today = closed[-1]
    lookback = closed[-(WINDOW + 1):-1]   # 20 bars BEFORE today
    if len(lookback) < WINDOW:
        print(f"Insufficient lookback: {len(lookback)}")
        return

    high_n = max(c["high"] for c in lookback)
    low_n = min(c["low"] for c in lookback)
    close = today["close"]
    date_str = today["openTime"].date().isoformat()

    state = load_state()
    action = "HOLD"
    note = ""

    if state["position"] == "flat" and close > high_n:
        # ENTER LONG
        btc = (state["cash"] / close) * (1 - COST_PER_LEG)
        cost_paid = state["cash"] * COST_PER_LEG
        state["btc_held"] = btc
        state["cash"] = 0.0
        state["entry_price"] = close
        state["entry_date"] = date_str
        state["position"] = "long"
        state["trades"] += 1
        action = "BUY"
        note = f"Breakout > {high_n:.2f} (20d high), fee={cost_paid:.2f}"

    elif state["position"] == "long" and close < low_n:
        # EXIT
        proceeds = state["btc_held"] * close * (1 - COST_PER_LEG)
        realized = proceeds - (state["entry_price"] * state["btc_held"])  # rough
        # cleaner realized: proceeds - capital_at_entry (which was state.cash before buy = unknown now)
        # Use entry-price-based realized PnL accounting:
        realized = (close * (1 - COST_PER_LEG) - state["entry_price"]) * state["btc_held"]
        state["cash"] = proceeds
        state["btc_held"] = 0.0
        state["realized_pnl_total"] += realized
        state["position"] = "flat"
        state["entry_price"] = None
        state["entry_date"] = None
        state["trades"] += 1
        action = "SELL"
        note = f"Breakdown < {low_n:.2f} (20d low), realized=${realized:+,.2f}"

    # Compute current equity & unrealized
    equity = state["cash"] + state["btc_held"] * close
    unrealized = 0.0
    if state["position"] == "long":
        unrealized = (close - state["entry_price"]) * state["btc_held"]

    # Ledger
    append_ledger({
        "timestamp": date_str,
        "action": action,
        "price": f"{close:.2f}",
        "btc": f"{state['btc_held']:.6f}",
        "cash": f"{state['cash']:.2f}",
        "equity": f"{equity:.2f}",
        "realized_pnl": f"{state['realized_pnl_total']:.2f}",
        "unrealized_pnl": f"{unrealized:.2f}",
        "note": note.replace(",", ";"),
    })

    save_state(state)

    # Output
    total_pnl = equity - CAPITAL
    print(f"=== Paper trade {date_str} ===")
    print(f"  BTC close:     ${close:,.2f}")
    print(f"  20d high/low:  ${high_n:,.2f} / ${low_n:,.2f}")
    print(f"  Action:        {action}  {note}")
    print(f"  Position:      {state['position']}  ({state['btc_held']:.6f} BTC @ ${state['entry_price'] or 0:,.2f})")
    print(f"  Cash:          ${state['cash']:,.2f}")
    print(f"  Equity:        ${equity:,.2f}")
    print(f"  Realized P&L:  ${state['realized_pnl_total']:+,.2f}")
    print(f"  Unrealized:    ${unrealized:+,.2f}")
    print(f"  Total P&L:     ${total_pnl:+,.2f}  ({total_pnl/CAPITAL*100:+.2f}%)")
    print(f"  Trades so far: {state['trades']}")

    # Telegram only on signal change
    if action in ("BUY", "SELL"):
        msg = (
            f"*BTC Donchian {action}* `{date_str}`\n"
            f"Price: ${close:,.2f}\n"
            f"20d H/L: ${high_n:,.0f} / ${low_n:,.0f}\n"
            f"{note}\n"
            f"Equity: ${equity:,.2f}\n"
            f"Total P&L: ${total_pnl:+,.2f} ({total_pnl/CAPITAL*100:+.2f}%)"
        )
        ok = send_telegram(msg)
        print(f"  Telegram alert sent: {ok}")


if __name__ == "__main__":
    main()
