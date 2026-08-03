"""Rolling range reversion with immediate take-profit.

Spot-only BTC strategy:
  - Build a rolling high/low range from the prior N hours.
  - Buy when price is near the bottom of that range.
  - Exit on the first of:
      1) fixed take-profit percentage from entry,
      2) range-position target,
      3) break below rolling low stop,
      4) max holding time.

The fixed take-profit is useful for spot scalping because waiting for the full
range high often turns winners into time exits.
"""
from pathlib import Path
import itertools
import math
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SRC = DATA / "btcusdt_15m_futures.csv"
OUT_TRADES = DATA / "range_takeprofit_trades.csv"
OUT_DAILY = DATA / "range_takeprofit_daily.csv"

CAPITAL = 10_000.0
FEE_SLIP_ROUNDTRIP = 0.0012
TARGET_DAILY = 50.0


def load():
    df = pd.read_csv(SRC, parse_dates=["ts"]).sort_values("ts").reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["ts_cst"] = df["ts"].dt.tz_convert("Asia/Shanghai")
    df["date_cst"] = df["ts_cst"].dt.date
    return df


def add_range(df, range_hours):
    df = df.copy()
    bars = int(range_hours * 4)
    df["range_hi"] = df["high"].rolling(bars).max().shift(1)
    df["range_lo"] = df["low"].rolling(bars).min().shift(1)
    df["range_width"] = df["range_hi"] / df["range_lo"] - 1
    df["pos_in_range"] = (df["close"] - df["range_lo"]) / (df["range_hi"] - df["range_lo"])
    return df


def run(df, days=90, range_hours=4, buy_q=0.15, sell_q=0.75,
        min_width=0.015, stop_break=0.008, take_profit=0.006,
        alloc=0.5, max_hold_bars=16):
    data = add_range(df, range_hours)
    data = data[data["ts"] >= data["ts"].max() - pd.Timedelta(days=days)].reset_index(drop=True)

    cash = CAPITAL
    btc = 0.0
    entry = 0.0
    entry_ts = None
    hold = 0
    trades = []
    equity_rows = []

    for r in data.itertuples(index=False):
        price = float(r.close)
        action = "HOLD"

        if btc > 0:
            hold += 1
            exit_price = None
            reason = None
            if float(r.high) >= entry * (1 + take_profit):
                exit_price = entry * (1 + take_profit)
                reason = "TAKE_PCT"
            elif np.isfinite(r.range_lo) and float(r.low) <= float(r.range_lo) * (1 - stop_break):
                exit_price = float(r.range_lo) * (1 - stop_break)
                reason = "STOP_BREAK"
            elif np.isfinite(r.pos_in_range) and float(r.pos_in_range) >= sell_q:
                exit_price = price
                reason = "SELL_RANGE"
            elif hold >= max_hold_bars:
                exit_price = price
                reason = "TIME"

            if exit_price is not None:
                gross = btc * exit_price
                fee = gross * FEE_SLIP_ROUNDTRIP / 2
                pnl = btc * (exit_price - entry) - (btc * entry + btc * exit_price) * FEE_SLIP_ROUNDTRIP / 2
                cash += gross - fee
                trades.append({
                    "entry_ts": entry_ts,
                    "exit_ts": r.ts,
                    "entry": entry,
                    "exit": exit_price,
                    "reason": reason,
                    "pnl": pnl,
                    "ret_pct": pnl / (btc * entry) * 100,
                    "hold_bars": hold,
                })
                btc = 0.0
                entry = 0.0
                entry_ts = None
                hold = 0
                action = reason

        if btc == 0 and np.isfinite(r.pos_in_range) and np.isfinite(r.range_width):
            if float(r.range_width) >= min_width and float(r.pos_in_range) <= buy_q:
                notional = cash * alloc
                fee = notional * FEE_SLIP_ROUNDTRIP / 2
                btc = (notional - fee) / price
                cash -= notional
                entry = price
                entry_ts = r.ts
                hold = 0
                action = "BUY_LOW"

        equity_rows.append({"ts": r.ts, "equity": cash + btc * price, "action": action})

    if btc > 0:
        r = data.iloc[-1]
        price = float(r["close"])
        gross = btc * price
        fee = gross * FEE_SLIP_ROUNDTRIP / 2
        pnl = btc * (price - entry) - (btc * entry + btc * price) * FEE_SLIP_ROUNDTRIP / 2
        cash += gross - fee
        trades.append({
            "entry_ts": entry_ts,
            "exit_ts": r["ts"],
            "entry": entry,
            "exit": price,
            "reason": "FINAL",
            "pnl": pnl,
            "ret_pct": pnl / (btc * entry) * 100,
            "hold_bars": hold,
        })
        equity_rows[-1]["equity"] = cash

    equity = pd.DataFrame(equity_rows)
    trades = pd.DataFrame(trades)
    equity["date_cst"] = pd.to_datetime(equity["ts"], utc=True).dt.tz_convert("Asia/Shanghai").dt.date
    daily = equity.groupby("date_cst").agg(equity=("equity", "last")).reset_index()
    daily["pnl"] = daily["equity"].diff().fillna(daily["equity"] - CAPITAL)
    if len(trades):
        trades["entry_ts"] = pd.to_datetime(trades["entry_ts"], utc=True)
        trades["exit_ts"] = pd.to_datetime(trades["exit_ts"], utc=True)
        trades["date_cst"] = trades["exit_ts"].dt.tz_convert("Asia/Shanghai").dt.date
    return equity, trades, daily


def summarize(trades, daily):
    ret = daily["equity"].pct_change().fillna(0)
    dd = (daily["equity"] / daily["equity"].cummax() - 1).min() if len(daily) else 0
    return {
        "trades": len(trades),
        "total": daily["equity"].iloc[-1] - CAPITAL,
        "avg_daily": daily["pnl"].mean(),
        "hit50": (daily["pnl"] >= TARGET_DAILY).mean() * 100,
        "loss50": (daily["pnl"] <= -TARGET_DAILY).mean() * 100,
        "win": (trades["pnl"] > 0).mean() * 100 if len(trades) else 0,
        "avg_trade": trades["pnl"].mean() if len(trades) else 0,
        "best": trades["pnl"].max() if len(trades) else 0,
        "worst": trades["pnl"].min() if len(trades) else 0,
        "max_dd": dd,
        "sharpe": ret.mean() * 365 / (ret.std() * math.sqrt(365)) if ret.std() > 1e-12 else np.nan,
    }


def main():
    df = load()
    grid = list(itertools.product(
        [4, 6, 12, 24],
        [0.03, 0.05, 0.10, 0.15],
        [0.65, 0.75, 0.85],
        [0.010, 0.015, 0.020],
        [0.005, 0.008, 0.010],
        [0.003, 0.005, 0.008, 0.010],
        [0.5, 1.0],
        [16, 24, 48, 96],
    ))
    rows = []
    for params in grid:
        rh, bq, sq, mw, stop, tp, alloc, hold = params
        _, trades, daily = run(
            df, range_hours=rh, buy_q=bq, sell_q=sq, min_width=mw,
            stop_break=stop, take_profit=tp, alloc=alloc, max_hold_bars=hold,
        )
        s = summarize(trades, daily)
        s.update({
            "range_hours": rh, "buy_q": bq, "sell_q": sq, "min_width": mw,
            "stop_break": stop, "take_profit": tp, "alloc": alloc, "max_hold_bars": hold,
        })
        rows.append(s)

    rows = sorted(rows, key=lambda x: (x["total"], x["max_dd"]), reverse=True)
    print("Top fixed take-profit variants, last 90d:")
    for r in rows[:12]:
        print(
            f"{r['range_hours']:>2}h pnl=${r['total']:+8.2f} avg=${r['avg_daily']:+6.2f} "
            f"trades={r['trades']:3d} win={r['win']:5.1f}% hit50={r['hit50']:4.1f}% "
            f"loss50={r['loss50']:4.1f}% dd={r['max_dd']*100:6.2f}% "
            f"buy<={r['buy_q']:.2f} sell>={r['sell_q']:.2f} width>{r['min_width']*100:.1f}% "
            f"stop={r['stop_break']*100:.1f}% tp={r['take_profit']*100:.1f}% "
            f"alloc={r['alloc']*100:.0f}% hold={r['max_hold_bars']}"
        )

    best = rows[0]
    _, trades, daily = run(
        df, range_hours=best["range_hours"], buy_q=best["buy_q"], sell_q=best["sell_q"],
        min_width=best["min_width"], stop_break=best["stop_break"],
        take_profit=best["take_profit"], alloc=best["alloc"], max_hold_bars=best["max_hold_bars"],
    )
    trades.to_csv(OUT_TRADES, index=False)
    daily.to_csv(OUT_DAILY, index=False)
    print("\nBest variant recent trades:")
    print(trades.tail(12).to_string(index=False, formatters={
        "entry": "{:.2f}".format,
        "exit": "{:.2f}".format,
        "pnl": "{:+.2f}".format,
        "ret_pct": "{:+.2f}".format,
    }) if len(trades) else "No trades")
    print("\nBest variant daily tail:")
    print(daily.tail(12).to_string(index=False, formatters={
        "equity": "{:.2f}".format,
        "pnl": "{:+.2f}".format,
    }))
    print(f"\nSaved {OUT_TRADES.relative_to(ROOT)} and {OUT_DAILY.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
