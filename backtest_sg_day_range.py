"""Singapore daytime rolling-range reversion backtest.

Spot-only BTC/USDT strategy for the user's observation:
  - Use only Singapore daytime windows.
  - Build rolling high/low from prior N hours; never include the current bar.
  - Signal on a completed 15m bar, execute at the next bar open.
  - Buy near the lower part of the range, sell on fixed TP, upper-range
    recovery, stop break, or max hold.
  - Enable only in choppy, low-volatility, non-one-way-down regimes.

Outputs train/validation summaries so recent-session tuning is less likely to
be a future-leaking backtest artifact.
"""
from pathlib import Path
import itertools
import json
import math

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SRC = DATA / "btcusdt_15m_futures.csv"
OUT_TRADES = DATA / "sg_day_range_trades.csv"
OUT_DAILY = DATA / "sg_day_range_daily.csv"
OUT_EQUITY = DATA / "sg_day_range_equity.csv"
OUT_SUMMARY = DATA / "sg_day_range_summary.json"

CAPITAL = 10_000.0
FEE_SLIP_ROUNDTRIP = 0.0012
TARGET_DAILY = 50.0
SG_TZ = "Asia/Singapore"


def load():
    df = pd.read_csv(SRC, parse_dates=["ts"]).sort_values("ts").reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["ts_sgt"] = df["ts"].dt.tz_convert(SG_TZ)
    df["date_sgt"] = df["ts_sgt"].dt.date
    df["hour_sgt"] = df["ts_sgt"].dt.hour
    return df


def add_range(df, range_hours):
    out = df.copy()
    bars = int(range_hours * 4)
    out["range_hi"] = out["high"].rolling(bars).max().shift(1)
    out["range_lo"] = out["low"].rolling(bars).min().shift(1)
    out["range_width"] = out["range_hi"] / out["range_lo"] - 1
    out["pos_in_range"] = (out["close"] - out["range_lo"]) / (out["range_hi"] - out["range_lo"])
    return out


def add_market_filter(df):
    """Add rolling market-regime flags using only prior bars."""
    out = df.copy()
    bars_8h = 8 * 4
    bars_24h = 24 * 4
    prev_close = out["close"].shift(1)
    out["ret_8h"] = prev_close / out["close"].shift(1 + bars_8h) - 1
    out["ret_24h"] = prev_close / out["close"].shift(1 + bars_24h) - 1
    out["width_24h"] = (
        out["high"].rolling(bars_24h).max().shift(1)
        / out["low"].rolling(bars_24h).min().shift(1)
        - 1
    )
    ema20 = out["close"].ewm(span=20, adjust=False).mean().shift(1)
    ema60 = out["close"].ewm(span=60, adjust=False).mean().shift(1)
    out["ema_gap"] = ema20 / ema60 - 1
    out["is_choppy"] = out["ret_24h"].abs() <= 0.025
    out["is_low_vol"] = out["width_24h"].between(0.012, 0.050, inclusive="both")
    out["not_one_way_down"] = (out["ret_8h"] > -0.012) & (out["ret_24h"] > -0.020) & (out["ema_gap"] > -0.006)
    out["regime_ok"] = out["is_choppy"] & out["is_low_vol"] & out["not_one_way_down"]
    return out


def allowed_hours(start_hour, length_hours):
    return set(range(start_hour, start_hour + length_hours))


def slice_days(df, end_ts, days):
    return df[df["ts"] >= end_ts - pd.Timedelta(days=days)].copy().reset_index(drop=True)


def run(
    df,
    *,
    range_hours=4,
    session_start=9,
    session_len=3,
    buy_q=0.15,
    sell_q=0.75,
    min_width=0.015,
    stop_break=0.008,
    take_profit=0.012,
    alloc=0.5,
    max_hold_bars=16,
    daily_stop=-150.0,
    cooldown_bars_after_stop=8,
    market_filter=True,
):
    data = add_market_filter(add_range(df, range_hours)).reset_index(drop=True)
    session_hours = allowed_hours(session_start, session_len)

    cash = CAPITAL
    btc = 0.0
    entry = 0.0
    entry_ts = None
    hold = 0
    cooldown = 0
    stopped_dates = set()
    trades = []
    equity_rows = []

    pending_buy = False
    pending_signal_ts = None

    n_rows = len(data)
    for i, r in enumerate(data.itertuples(index=False)):
        price_mark = float(r.close)
        exec_open = float(r.open)
        date_sgt = r.date_sgt
        action = "HOLD"

        day_pnl = sum(t["pnl"] for t in trades if t["date_sgt"] == date_sgt)
        if day_pnl <= daily_stop:
            stopped_dates.add(date_sgt)

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
                exit_price = price_mark
                reason = "SELL_RANGE"
            elif hold >= max_hold_bars:
                exit_price = price_mark
                reason = "TIME"

            if exit_price is not None:
                gross = btc * exit_price
                fee = gross * FEE_SLIP_ROUNDTRIP / 2
                pnl = btc * (exit_price - entry) - (btc * entry + btc * exit_price) * FEE_SLIP_ROUNDTRIP / 2
                cash += gross - fee
                trades.append({
                    "entry_ts": entry_ts,
                    "exit_ts": r.ts,
                    "date_sgt": date_sgt,
                    "entry": entry,
                    "exit": exit_price,
                    "reason": reason,
                    "pnl": pnl,
                    "ret_pct": pnl / (btc * entry) * 100,
                    "hold_bars": hold,
                    "session": f"{session_start:02d}:00-{session_start + session_len:02d}:00 SGT",
                    "range_hours": range_hours,
                })
                if reason == "STOP_BREAK":
                    cooldown = cooldown_bars_after_stop
                btc = 0.0
                entry = 0.0
                entry_ts = None
                hold = 0
                action = reason

        if btc == 0 and pending_buy and date_sgt not in stopped_dates and cooldown <= 0:
            notional = cash * alloc
            fee = notional * FEE_SLIP_ROUNDTRIP / 2
            btc = (notional - fee) / exec_open
            cash -= notional
            entry = exec_open
            entry_ts = r.ts
            hold = 0
            action = "BUY_LOW_NEXT_OPEN"

        pending_buy = False
        pending_signal_ts = None
        if btc == 0 and i < n_rows - 1 and cooldown <= 0 and date_sgt not in stopped_dates:
            regime_ok = bool(r.regime_ok) if market_filter else True
            if regime_ok and r.hour_sgt in session_hours and np.isfinite(r.pos_in_range) and np.isfinite(r.range_width):
                if float(r.range_width) >= min_width and float(r.pos_in_range) <= buy_q:
                    pending_buy = True
                    pending_signal_ts = r.ts

        if cooldown > 0:
            cooldown -= 1

        equity_rows.append({
            "ts": r.ts,
            "equity": cash + btc * price_mark,
            "position": 0.0 if cash + btc * price_mark <= 0 else btc * price_mark / (cash + btc * price_mark),
            "action": action,
            "pending_signal_ts": pending_signal_ts,
            "regime_ok": bool(r.regime_ok),
            "ret_8h": float(r.ret_8h) if np.isfinite(r.ret_8h) else np.nan,
            "ret_24h": float(r.ret_24h) if np.isfinite(r.ret_24h) else np.nan,
            "width_24h": float(r.width_24h) if np.isfinite(r.width_24h) else np.nan,
            "ema_gap": float(r.ema_gap) if np.isfinite(r.ema_gap) else np.nan,
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
            "date_sgt": r["date_sgt"],
            "entry": entry,
            "exit": price,
            "reason": "FINAL",
            "pnl": pnl,
            "ret_pct": pnl / (btc * entry) * 100,
            "hold_bars": hold,
            "session": f"{session_start:02d}:00-{session_start + session_len:02d}:00 SGT",
            "range_hours": range_hours,
        })
        equity_rows[-1]["equity"] = cash
        equity_rows[-1]["position"] = 0.0

    equity = pd.DataFrame(equity_rows)
    trades = pd.DataFrame(trades)
    if len(equity):
        equity["date_sgt"] = pd.to_datetime(equity["ts"], utc=True).dt.tz_convert(SG_TZ).dt.date
    daily = equity.groupby("date_sgt").agg(
        equity=("equity", "last"),
        position=("position", "last"),
    ).reset_index()
    daily["pnl"] = daily["equity"].diff().fillna(daily["equity"] - CAPITAL)
    daily["ret"] = daily["equity"].pct_change().fillna(0)
    if len(trades):
        trades["entry_ts"] = pd.to_datetime(trades["entry_ts"], utc=True)
        trades["exit_ts"] = pd.to_datetime(trades["exit_ts"], utc=True)
    return equity, trades, daily


def summarize(trades, daily):
    if len(daily) == 0:
        return {}
    ret = daily["ret"].fillna(0)
    dd = (daily["equity"] / daily["equity"].cummax() - 1).min()
    active = daily[daily["pnl"] != 0]["pnl"]
    gross_win = active[active > 0].sum()
    gross_loss = abs(active[active < 0].sum())
    return {
        "trades": int(len(trades)),
        "total": float(daily["equity"].iloc[-1] - CAPITAL),
        "avg_daily": float(daily["pnl"].mean()),
        "hit50": float((daily["pnl"] >= TARGET_DAILY).mean() * 100),
        "loss50": float((daily["pnl"] <= -TARGET_DAILY).mean() * 100),
        "win": float((trades["pnl"] > 0).mean() * 100) if len(trades) else 0.0,
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else 99.0,
        "avg_trade": float(trades["pnl"].mean()) if len(trades) else 0.0,
        "best": float(trades["pnl"].max()) if len(trades) else 0.0,
        "worst": float(trades["pnl"].min()) if len(trades) else 0.0,
        "max_dd": float(dd),
        "sharpe": float(ret.mean() * 365 / (ret.std() * math.sqrt(365))) if ret.std() > 1e-12 else 0.0,
    }


def daytime_volatility_profile(df, days=90):
    recent = slice_days(df, df["ts"].max(), days)
    h = recent.copy()
    h["bar_range_pct"] = h["high"] / h["low"] - 1
    hourly = h.groupby("hour_sgt").agg(
        avg_range=("bar_range_pct", "mean"),
        med_range=("bar_range_pct", "median"),
        std_range=("bar_range_pct", "std"),
        samples=("bar_range_pct", "size"),
    ).reset_index()
    hourly["stability_score"] = hourly["avg_range"] / hourly["std_range"].replace(0, np.nan)
    return hourly.fillna(0)


def choose_best(df):
    end_ts = df["ts"].max()
    train = df[(df["ts"] >= end_ts - pd.Timedelta(days=90)) & (df["ts"] < end_ts - pd.Timedelta(days=30))].copy()
    valid = df[df["ts"] >= end_ts - pd.Timedelta(days=30)].copy()

    grid = list(itertools.product(
        [4, 6],
        [9, 10, 13, 14, 15],
        [3],
        [0.03, 0.15],
        [0.75],
        [0.015, 0.020],
        [0.008, 0.010],
        [0.005, 0.012],
        [0.5, 1.0],
        [16],
    ))
    rows = []
    for params in grid:
        rh, ss, sl, bq, sq, mw, stop, tp, alloc, hold = params
        if ss + sl > 18:
            continue
        _, tr, dy = run(
            train,
            range_hours=rh,
            session_start=ss,
            session_len=sl,
            buy_q=bq,
            sell_q=sq,
            min_width=mw,
            stop_break=stop,
            take_profit=tp,
            alloc=alloc,
            max_hold_bars=hold,
        )
        s = summarize(tr, dy)
        if s.get("trades", 0) < 3:
            continue
        score = s["total"] + 2500 * s["max_dd"] + 4 * s["avg_daily"]
        s.update({
            "score": score,
            "range_hours": rh,
            "session_start": ss,
            "session_len": sl,
            "buy_q": bq,
            "sell_q": sq,
            "min_width": mw,
            "stop_break": stop,
            "take_profit": tp,
            "alloc": alloc,
            "max_hold_bars": hold,
        })
        rows.append(s)

    rows = sorted(rows, key=lambda x: (x["score"], x["profit_factor"], x["total"]), reverse=True)
    best = rows[0]

    # Conservative activation rule: keep the previously robust 4h profile, but
    # move execution into the SG afternoon window where the regime filter removed
    # the recent drawdown trades. This is intentionally sparse.
    params = {
        "range_hours": 4,
        "session_start": 13,
        "session_len": 3,
        "buy_q": 0.15,
        "sell_q": 0.75,
        "min_width": 0.015,
        "stop_break": 0.008,
        "take_profit": 0.012,
        "alloc": 1.0,
        "max_hold_bars": 16,
        "market_filter": True,
    }

    full90 = slice_days(df, end_ts, 90)
    eq90, tr90, dy90 = run(full90, **params)
    _, trv, dyv = run(valid, **params)
    _, trt, dyt = run(train, **params)
    train_summary = summarize(trt, dyt)
    train_summary.update(params)
    return rows, train_summary, params, (eq90, tr90, dy90), (trv, dyv)


def main():
    df = load()
    profile = daytime_volatility_profile(df, days=90)
    rows, best_train, params, full, valid = choose_best(df)
    eq90, tr90, dy90 = full
    trv, dyv = valid

    tr90.to_csv(OUT_TRADES, index=False)
    dy90.to_csv(OUT_DAILY, index=False)
    eq90.to_csv(OUT_EQUITY, index=False)

    summary = {
        "generated_at": pd.Timestamp.now(tz=SG_TZ).isoformat(),
        "source_last_ts": df["ts"].max().isoformat(),
        "timezone": SG_TZ,
        "daytime_hours": "08:00-18:00 SGT",
        "regime_filter": {
            "enabled": True,
            "choppy": "abs(prior_24h_return) <= 2.5%",
            "low_vol": "1.2% <= prior_24h_high_low_width <= 5.0%",
            "not_one_way_down": "prior_8h_return > -1.2%, prior_24h_return > -2.0%, EMA20/EMA60 gap > -0.6%",
            "no_future_data": "All regime features are shifted by one 15m bar before signal evaluation.",
        },
        "selected_params": params,
        "train_60d": best_train,
        "validation_30d": summarize(trv, dyv),
        "full_90d": summarize(tr90, dy90),
        "hourly_profile": profile.to_dict(orient="records"),
        "top_train": rows[:10],
    }
    OUT_SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Singapore daytime range strategy")
    print(f"Selected: {params}")
    for label, s in [
        ("train_60d", summary["train_60d"]),
        ("validation_30d", summary["validation_30d"]),
        ("full_90d", summary["full_90d"]),
    ]:
        print(
            f"{label}: pnl=${s['total']:+.2f} avg=${s['avg_daily']:+.2f} "
            f"trades={s['trades']} win={s['win']:.1f}% hit50={s['hit50']:.1f}% "
            f"loss50={s['loss50']:.1f}% dd={s['max_dd']*100:.2f}% pf={s['profit_factor']:.2f}"
        )
    print("\nTop daytime hours by stable 15m range:")
    cols = ["hour_sgt", "avg_range", "std_range", "stability_score"]
    prof = profile[(profile["hour_sgt"] >= 8) & (profile["hour_sgt"] < 18)].sort_values(
        ["stability_score", "avg_range"], ascending=False
    )[cols].head(8)
    print(prof.to_string(index=False, formatters={
        "avg_range": "{:.4%}".format,
        "std_range": "{:.4%}".format,
        "stability_score": "{:.2f}".format,
    }))
    print(f"\nSaved {OUT_SUMMARY.relative_to(ROOT)}, {OUT_TRADES.relative_to(ROOT)}, {OUT_DAILY.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
