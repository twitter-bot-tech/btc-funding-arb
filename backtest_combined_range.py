"""Combined BTC rolling-range strategy.

Two spot-only sleeves:
  1) Main range sleeve: higher opportunity count, all-day 4h range reversion.
  2) SG daytime sleeve: sparse helper, only 13:00-16:00 Singapore time and only
     in choppy / low-vol / non-one-way-down regimes.

Each sleeve has its own capital allocation and risk controls. This avoids the
common mistake of letting a helper strategy double the total exposure.
"""
from pathlib import Path
import json
import math

import numpy as np
import pandas as pd

from backtest_sg_day_range import add_market_filter, add_range, load

ROOT = Path(__file__).parent
DATA = ROOT / "data"
OUT_DAILY = DATA / "combined_range_daily.csv"
OUT_TRADES = DATA / "combined_range_trades.csv"
OUT_SUMMARY = DATA / "combined_range_summary.json"

CAPITAL = 10_000.0
FEE_SLIP_ROUNDTRIP = 0.0012
TARGET_DAILY = 50.0


def run_sleeve(
    df,
    *,
    name,
    capital,
    range_hours,
    buy_q,
    sell_q,
    min_width,
    stop_break,
    take_profit=None,
    max_hold_bars=16,
    session_start=None,
    session_len=None,
    market_filter=False,
    profit_only_exit=False,
):
    data = add_market_filter(add_range(df, range_hours)).reset_index(drop=True)
    hours = None
    if session_start is not None and session_len is not None:
        hours = set(range(session_start, session_start + session_len))

    cash = capital
    btc = 0.0
    entry = 0.0
    entry_ts = None
    hold = 0
    trades = []
    rows = []
    pending_buy = False

    for i, r in enumerate(data.itertuples(index=False)):
        price_mark = float(r.close)
        action = "HOLD"

        if btc > 0:
            hold += 1
            exit_price = None
            reason = None
            if take_profit is not None and float(r.high) >= entry * (1 + take_profit):
                exit_price = entry * (1 + take_profit)
                reason = "TAKE_PCT"
            elif not profit_only_exit and np.isfinite(r.range_lo) and float(r.low) <= float(r.range_lo) * (1 - stop_break):
                exit_price = float(r.range_lo) * (1 - stop_break)
                reason = "STOP_BREAK"
            elif np.isfinite(r.pos_in_range) and float(r.pos_in_range) >= sell_q:
                exit_price = price_mark
                reason = "SELL_RANGE"
            elif hold >= max_hold_bars:
                exit_price = price_mark
                reason = "TIME"

            if exit_price is not None:
                candidate_pnl = btc * (exit_price - entry) - (btc * entry + btc * exit_price) * FEE_SLIP_ROUNDTRIP / 2
                if profit_only_exit and candidate_pnl <= 0:
                    exit_price = None
                    reason = None

            if exit_price is not None:
                gross = btc * exit_price
                fee = gross * FEE_SLIP_ROUNDTRIP / 2
                pnl = candidate_pnl
                cash += gross - fee
                trades.append({
                    "sleeve": name,
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

        if btc == 0 and pending_buy:
            notional = cash
            fee = notional * FEE_SLIP_ROUNDTRIP / 2
            exec_open = float(r.open)
            btc = (notional - fee) / exec_open
            cash -= notional
            entry = exec_open
            entry_ts = r.ts
            hold = 0
            action = "BUY_LOW_NEXT_OPEN"

        pending_buy = False
        if btc == 0 and i < len(data) - 1 and np.isfinite(r.pos_in_range) and np.isfinite(r.range_width):
            session_ok = True if hours is None else r.hour_sgt in hours
            regime_ok = bool(r.regime_ok) if market_filter else True
            if session_ok and regime_ok and float(r.range_width) >= min_width and float(r.pos_in_range) <= buy_q:
                pending_buy = True

        rows.append({
            "ts": r.ts,
            "equity": cash + btc * price_mark,
            "position": 0.0 if cash + btc * price_mark <= 0 else btc * price_mark / (cash + btc * price_mark),
            "action": action,
        })

    if btc > 0:
        r = data.iloc[-1]
        price = float(r["close"])
        pnl = btc * (price - entry) - (btc * entry + btc * price) * FEE_SLIP_ROUNDTRIP / 2
        if (not profit_only_exit) or pnl > 0:
            gross = btc * price
            fee = gross * FEE_SLIP_ROUNDTRIP / 2
            cash += gross - fee
            trades.append({
                "sleeve": name,
                "entry_ts": entry_ts,
                "exit_ts": r["ts"],
                "entry": entry,
                "exit": price,
                "reason": "FINAL",
                "pnl": pnl,
                "ret_pct": pnl / (btc * entry) * 100,
                "hold_bars": hold,
            })
            rows[-1]["equity"] = cash
            rows[-1]["position"] = 0.0

    equity = pd.DataFrame(rows)
    trades = pd.DataFrame(trades)
    if len(trades):
        trades["entry_ts"] = pd.to_datetime(trades["entry_ts"], utc=True)
        trades["exit_ts"] = pd.to_datetime(trades["exit_ts"], utc=True)
        trades["date_sgt"] = trades["exit_ts"].dt.tz_convert("Asia/Singapore").dt.date
    return equity, trades


def daily_from_equity(equity, capital=CAPITAL):
    out = equity.copy()
    out["date_sgt"] = pd.to_datetime(out["ts"], utc=True).dt.tz_convert("Asia/Singapore").dt.date
    daily = out.groupby("date_sgt").agg(equity=("equity", "last"), position=("position", "mean")).reset_index()
    daily["pnl"] = daily["equity"].diff().fillna(daily["equity"] - capital)
    daily["ret"] = daily["equity"].pct_change().fillna(0)
    return daily


def summarize(trades, daily):
    ret = daily["ret"].fillna(0)
    dd = (daily["equity"] / daily["equity"].cummax() - 1).min() if len(daily) else 0
    active = daily[daily["pnl"] != 0]["pnl"]
    gross_win = active[active > 0].sum()
    gross_loss = abs(active[active < 0].sum())
    return {
        "trades": int(len(trades)),
        "total": float(daily["equity"].iloc[-1] - daily["equity"].iloc[0]) if len(daily) else 0.0,
        "final_vs_initial": float(daily["equity"].iloc[-1] - CAPITAL) if len(daily) else 0.0,
        "avg_daily": float(daily["pnl"].mean()) if len(daily) else 0.0,
        "hit50": float((daily["pnl"] >= TARGET_DAILY).mean() * 100) if len(daily) else 0.0,
        "loss50": float((daily["pnl"] <= -TARGET_DAILY).mean() * 100) if len(daily) else 0.0,
        "win": float((trades["pnl"] > 0).mean() * 100) if len(trades) else 0.0,
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else 99.0,
        "avg_trade": float(trades["pnl"].mean()) if len(trades) else 0.0,
        "best": float(trades["pnl"].max()) if len(trades) else 0.0,
        "worst": float(trades["pnl"].min()) if len(trades) else 0.0,
        "max_dd": float(dd),
        "sharpe": float(ret.mean() * 365 / (ret.std() * math.sqrt(365))) if ret.std() > 1e-12 else 0.0,
    }


def run_combo(df, days=90, main_weight=0.70, sg_weight=0.30, profit_only_exit=False):
    end_ts = df["ts"].max()
    data = df[df["ts"] >= end_ts - pd.Timedelta(days=days)].copy().reset_index(drop=True)

    main_eq, main_tr = run_sleeve(
        data,
        name="MAIN_4H_RANGE",
        capital=CAPITAL * main_weight,
        range_hours=4,
        buy_q=0.15,
        sell_q=0.75,
        min_width=0.015,
        stop_break=0.010,
        take_profit=None,
        max_hold_bars=16,
        market_filter=False,
        profit_only_exit=profit_only_exit,
    )
    sg_eq, sg_tr = run_sleeve(
        data,
        name="SG_FILTERED_RANGE",
        capital=CAPITAL * sg_weight,
        range_hours=4,
        buy_q=0.15,
        sell_q=0.75,
        min_width=0.015,
        stop_break=0.008,
        take_profit=0.012,
        max_hold_bars=16,
        session_start=13,
        session_len=3,
        market_filter=True,
        profit_only_exit=profit_only_exit,
    )

    eq = main_eq[["ts", "equity", "position"]].rename(columns={"equity": "main_equity", "position": "main_position"})
    eq = eq.merge(
        sg_eq[["ts", "equity", "position"]].rename(columns={"equity": "sg_equity", "position": "sg_position"}),
        on="ts",
        how="inner",
    )
    eq["equity"] = eq["main_equity"] + eq["sg_equity"]
    eq["position"] = (
        eq["main_equity"] * eq["main_position"] + eq["sg_equity"] * eq["sg_position"]
    ) / eq["equity"]
    trades = pd.concat([main_tr, sg_tr], ignore_index=True).sort_values("exit_ts").reset_index(drop=True)
    daily = daily_from_equity(eq)
    return eq, trades, daily


def main():
    df = load()
    eq90, tr90, dy90 = run_combo(df, days=90)
    eq30, tr30, dy30 = run_combo(df, days=30)
    eq90_po, tr90_po, dy90_po = run_combo(df, days=90, profit_only_exit=True)
    eq30_po, tr30_po, dy30_po = run_combo(df, days=30, profit_only_exit=True)

    tr90.to_csv(OUT_TRADES, index=False)
    dy90.to_csv(OUT_DAILY, index=False)

    summary = {
        "generated_at": pd.Timestamp.now(tz="Asia/Singapore").isoformat(),
        "source_last_ts": df["ts"].max().isoformat(),
        "capital": CAPITAL,
        "allocation": {"main_4h_range": 0.70, "sg_filtered_range": 0.30},
        "rules": {
            "main": "70% capital, all-day 4h range, buy bottom 15%, sell 75%, stop 1.0%, max hold 16 bars, no fixed TP",
            "sg": "30% capital, 13:00-16:00 SGT, 4h range, market filter ON, fixed TP 1.2%, stop 0.8%",
            "profit_only_experiment": "Optional: do not close losing positions; only close when net PnL is positive. Unrealized drawdown still counts in equity.",
        },
        "last_90d": summarize(tr90, dy90),
        "last_30d": summarize(tr30, dy30),
        "profit_only_last_90d": summarize(tr90_po, dy90_po),
        "profit_only_last_30d": summarize(tr30_po, dy30_po),
        "sleeve_pnl_90d": tr90.groupby("sleeve")["pnl"].sum().to_dict() if len(tr90) else {},
        "recent_trades": tr90.tail(12).to_dict(orient="records") if len(tr90) else [],
    }
    OUT_SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("Combined main + SG filtered range")
    for label, trades, daily in [("last_90d", tr90, dy90), ("last_30d", tr30, dy30)]:
        s = summarize(trades, daily)
        print(
            f"{label}: pnl=${s['final_vs_initial']:+.2f} avg=${s['avg_daily']:+.2f} "
            f"trades={s['trades']} win={s['win']:.1f}% hit50={s['hit50']:.1f}% "
            f"loss50={s['loss50']:.1f}% dd={s['max_dd']*100:.2f}% pf={s['profit_factor']:.2f}"
        )
    for label, trades, daily in [("profit_only_90d", tr90_po, dy90_po), ("profit_only_30d", tr30_po, dy30_po)]:
        s = summarize(trades, daily)
        print(
            f"{label}: pnl=${s['final_vs_initial']:+.2f} avg=${s['avg_daily']:+.2f} "
            f"trades={s['trades']} win={s['win']:.1f}% hit50={s['hit50']:.1f}% "
            f"loss50={s['loss50']:.1f}% dd={s['max_dd']*100:.2f}% pf={s['profit_factor']:.2f}"
        )
    print("Sleeve pnl 90d:", summary["sleeve_pnl_90d"])
    print(f"Saved {OUT_SUMMARY.relative_to(ROOT)}, {OUT_TRADES.relative_to(ROOT)}, {OUT_DAILY.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
