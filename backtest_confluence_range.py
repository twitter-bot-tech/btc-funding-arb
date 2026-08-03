"""4h range strategy with multi-signal confluence filters.

This tests a K-line-only version of the idea from the screenshot:
  - 4h range low entry
  - RSI(14)
  - short/15m momentum
  - VWAP reclaim / deviation
  - SMA(8/21)
  - non-one-way-down market filter

Order-book imbalance is intentionally excluded because we do not have historical
order-book replay data in this project. Adding it without true L2 data would
create a fake backtest.
"""
from pathlib import Path
import itertools
import json
import math

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SRC = DATA / "btcusdt_15m_spot.csv"
OUT_SUMMARY = DATA / "confluence_range_summary.json"
OUT_TRADES = DATA / "confluence_range_trades.csv"
OUT_DAILY = DATA / "confluence_range_daily.csv"

CAPITAL = 10_000.0
FEE_SLIP_ROUNDTRIP = 0.0012
TARGET_DAILY = 50.0


def load():
    df = pd.read_csv(SRC, parse_dates=["ts"]).sort_values("ts").reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["ts_sgt"] = df["ts"].dt.tz_convert("Asia/Singapore")
    df["date_sgt"] = df["ts_sgt"].dt.date
    df["hour_sgt"] = df["ts_sgt"].dt.hour
    return df


def add_features(df):
    out = df.copy()
    bars_4h = 16
    bars_8h = 32
    bars_24h = 96

    out["range_hi"] = out["high"].rolling(bars_4h).max().shift(1)
    out["range_lo"] = out["low"].rolling(bars_4h).min().shift(1)
    out["range_width"] = out["range_hi"] / out["range_lo"] - 1
    out["pos_in_range"] = (out["close"] - out["range_lo"]) / (out["range_hi"] - out["range_lo"])

    delta = out["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi"] = (100 - 100 / (1 + rs)).shift(1)
    out["rsi_prev"] = out["rsi"].shift(1)

    out["mom_15m"] = (out["close"].shift(1) / out["close"].shift(2) - 1)
    out["mom_1h"] = (out["close"].shift(1) / out["close"].shift(5) - 1)
    out["mom_4h"] = (out["close"].shift(1) / out["close"].shift(17) - 1)

    typical = (out["high"] + out["low"] + out["close"]) / 3
    vwap_num = (typical * out["volume"]).rolling(bars_4h).sum().shift(1)
    vwap_den = out["volume"].rolling(bars_4h).sum().shift(1)
    out["vwap_4h"] = vwap_num / vwap_den
    out["vwap_dev"] = out["close"].shift(1) / out["vwap_4h"] - 1
    out["vwap_reclaim"] = (out["close"].shift(1) > out["vwap_4h"]) & (out["close"].shift(2) <= out["vwap_4h"].shift(1))

    out["sma8"] = out["close"].rolling(8).mean().shift(1)
    out["sma21"] = out["close"].rolling(21).mean().shift(1)
    out["sma_gap"] = out["sma8"] / out["sma21"] - 1
    out["sma_cross_up"] = (out["sma8"] > out["sma21"]) & (out["sma8"].shift(1) <= out["sma21"].shift(1))

    prev_close = out["close"].shift(1)
    out["ret_8h"] = prev_close / out["close"].shift(1 + bars_8h) - 1
    out["ret_24h"] = prev_close / out["close"].shift(1 + bars_24h) - 1
    out["width_24h"] = (
        out["high"].rolling(bars_24h).max().shift(1)
        / out["low"].rolling(bars_24h).min().shift(1)
        - 1
    )
    out["not_one_way_down"] = (out["ret_8h"] > -0.012) & (out["ret_24h"] > -0.020)
    return out


def score_row(r, rsi_max=48, vwap_dev_min=-0.006, sma_gap_min=-0.004):
    signals = {
        "rsi_rebound": bool(np.isfinite(r.rsi) and r.rsi <= rsi_max and r.rsi >= r.rsi_prev),
        "mom_15m_pos": bool(np.isfinite(r.mom_15m) and r.mom_15m > 0),
        "mom_1h_not_bad": bool(np.isfinite(r.mom_1h) and r.mom_1h > -0.004),
        "vwap_ok": bool(np.isfinite(r.vwap_dev) and (r.vwap_dev >= vwap_dev_min or r.vwap_reclaim)),
        "sma_ok": bool(np.isfinite(r.sma_gap) and (r.sma_gap >= sma_gap_min or r.sma_cross_up)),
        "not_one_way_down": bool(r.not_one_way_down),
    }
    return sum(signals.values()), signals


def run(
    df,
    *,
    days=90,
    buy_q=0.15,
    sell_q=0.75,
    min_width=0.015,
    stop_break=0.010,
    take_profit=0.010,
    max_hold_bars=16,
    min_score=4,
    alloc=1.0,
    rsi_max=48,
    vwap_dev_min=-0.006,
    sma_gap_min=-0.004,
):
    end_ts = df["ts"].max()
    data = add_features(df)
    data = data[data["ts"] >= end_ts - pd.Timedelta(days=days)].reset_index(drop=True)

    cash = CAPITAL
    btc = 0.0
    entry = 0.0
    entry_ts = None
    hold = 0
    pending_buy = None
    rows = []
    trades = []

    for i, r in enumerate(data.itertuples(index=False)):
        mark = float(r.close)
        action = "HOLD"

        if btc > 0:
            hold += 1
            exit_price = None
            reason = None
            if take_profit is not None and float(r.high) >= entry * (1 + take_profit):
                exit_price = entry * (1 + take_profit)
                reason = "TAKE_PCT"
            elif np.isfinite(r.range_lo) and float(r.low) <= float(r.range_lo) * (1 - stop_break):
                exit_price = float(r.range_lo) * (1 - stop_break)
                reason = "STOP_BREAK"
            elif np.isfinite(r.pos_in_range) and float(r.pos_in_range) >= sell_q:
                exit_price = mark
                reason = "SELL_RANGE"
            elif hold >= max_hold_bars:
                exit_price = mark
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
                    **(pending_buy or {}),
                })
                btc = 0.0
                entry = 0.0
                entry_ts = None
                hold = 0
                action = reason

        if btc == 0 and pending_buy is not None:
            notional = cash * alloc
            fee = notional * FEE_SLIP_ROUNDTRIP / 2
            exec_open = float(r.open)
            btc = (notional - fee) / exec_open
            cash -= notional
            entry = exec_open
            entry_ts = r.ts
            hold = 0
            action = "BUY_CONFLUENCE"

        pending_buy = None
        if btc == 0 and i < len(data) - 1 and np.isfinite(r.pos_in_range) and np.isfinite(r.range_width):
            score, signals = score_row(r, rsi_max=rsi_max, vwap_dev_min=vwap_dev_min, sma_gap_min=sma_gap_min)
            range_ok = float(r.range_width) >= min_width and float(r.pos_in_range) <= buy_q
            if range_ok and score >= min_score:
                pending_buy = {
                    "signal_ts": r.ts,
                    "signal_score": score,
                    **{f"sig_{k}": v for k, v in signals.items()},
                }

        rows.append({
            "ts": r.ts,
            "equity": cash + btc * mark,
            "position": 0.0 if cash + btc * mark <= 0 else btc * mark / (cash + btc * mark),
            "action": action,
        })

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
        rows[-1]["equity"] = cash
        rows[-1]["position"] = 0.0

    equity = pd.DataFrame(rows)
    trades = pd.DataFrame(trades)
    equity["date_sgt"] = pd.to_datetime(equity["ts"], utc=True).dt.tz_convert("Asia/Singapore").dt.date
    daily = equity.groupby("date_sgt").agg(equity=("equity", "last"), position=("position", "mean")).reset_index()
    daily["pnl"] = daily["equity"].diff().fillna(daily["equity"] - CAPITAL)
    daily["ret"] = daily["equity"].pct_change().fillna(0)
    if len(trades):
        trades["entry_ts"] = pd.to_datetime(trades["entry_ts"], utc=True)
        trades["exit_ts"] = pd.to_datetime(trades["exit_ts"], utc=True)
        trades["date_sgt"] = trades["exit_ts"].dt.tz_convert("Asia/Singapore").dt.date
    return equity, trades, daily


def summarize(trades, daily):
    ret = daily["ret"].fillna(0)
    dd = (daily["equity"] / daily["equity"].cummax() - 1).min() if len(daily) else 0
    active = daily[daily["pnl"] != 0]["pnl"]
    gross_win = active[active > 0].sum()
    gross_loss = abs(active[active < 0].sum())
    return {
        "trades": int(len(trades)),
        "total": float(daily["equity"].iloc[-1] - CAPITAL) if len(daily) else 0.0,
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


def main():
    df = load()
    grid = list(itertools.product(
        [3, 4, 5],
        [0.10, 0.15],
        [0.75, 0.85],
        [0.015, 0.020],
        [0.008, 0.010],
        [0.006, 0.010, 0.012],
        [40, 45, 48, 52],
    ))
    rows = []
    for min_score, buy_q, sell_q, min_width, stop, tp, rsi_max in grid:
        _, tr, dy = run(
            df,
            days=90,
            min_score=min_score,
            buy_q=buy_q,
            sell_q=sell_q,
            min_width=min_width,
            stop_break=stop,
            take_profit=tp,
            rsi_max=rsi_max,
        )
        s = summarize(tr, dy)
        if s["trades"] < 5:
            continue
        score = s["total"] + 1800 * s["max_dd"] + 2 * s["avg_daily"] + s["profit_factor"] * 20
        s.update({
            "score_rank": score,
            "min_score": min_score,
            "buy_q": buy_q,
            "sell_q": sell_q,
            "min_width": min_width,
            "stop_break": stop,
            "take_profit": tp,
            "rsi_max": rsi_max,
        })
        rows.append(s)

    rows = sorted(rows, key=lambda x: (x["score_rank"], x["total"]), reverse=True)
    best = rows[0]
    params = {k: best[k] for k in [
        "min_score", "buy_q", "sell_q", "min_width", "stop_break", "take_profit", "rsi_max"
    ]}
    _, tr90, dy90 = run(df, days=90, **params)
    _, tr30, dy30 = run(df, days=30, **params)

    tr90.to_csv(OUT_TRADES, index=False)
    dy90.to_csv(OUT_DAILY, index=False)
    summary = {
        "generated_at": pd.Timestamp.now(tz="Asia/Singapore").isoformat(),
        "source_last_ts": df["ts"].max().isoformat(),
        "notes": {
            "order_book_imbalance": "Not tested because historical L2/order-book replay data is unavailable.",
            "execution": "Signals use completed 15m bars and execute at the next bar open.",
            "signal_count": "RSI rebound, 15m momentum, 1h momentum, VWAP, SMA8/21, non-one-way-down.",
        },
        "selected_params": params,
        "last_90d": summarize(tr90, dy90),
        "last_30d": summarize(tr30, dy30),
        "top_variants": rows[:10],
        "recent_trades": tr90.tail(12).to_dict(orient="records") if len(tr90) else [],
    }
    OUT_SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("Confluence range backtest")
    print("Selected:", params)
    for label, tr, dy in [("last_90d", tr90, dy90), ("last_30d", tr30, dy30)]:
        s = summarize(tr, dy)
        print(
            f"{label}: pnl=${s['total']:+.2f} avg=${s['avg_daily']:+.2f} "
            f"trades={s['trades']} win={s['win']:.1f}% hit50={s['hit50']:.1f}% "
            f"loss50={s['loss50']:.1f}% dd={s['max_dd']*100:.2f}% pf={s['profit_factor']:.2f}"
        )
    print("\nTop variants:")
    for r in rows[:8]:
        print(
            f"score>={r['min_score']} rsi<={r['rsi_max']} pnl=${r['total']:+.2f} "
            f"trades={r['trades']} win={r['win']:.1f}% dd={r['max_dd']*100:.2f}% "
            f"pf={r['profit_factor']:.2f} tp={r['take_profit']*100:.1f}%"
        )
    print(f"\nSaved {OUT_SUMMARY.relative_to(ROOT)}, {OUT_TRADES.relative_to(ROOT)}, {OUT_DAILY.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
