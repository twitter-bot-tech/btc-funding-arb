"""BTC spot session pullback backtest.

Strategy under test:
  - Spot long only, no leverage.
  - Trade only during Shanghai 20:00-24:00 by default.
  - Daily trend filter: close > 20MA and 20MA > 60MA.
  - 1h trend filter: EMA20 > EMA60.
  - Entry: price pulls back near 1h EMA20, then 15m closes back above EMA20.
  - Exit: partial-style target approximated as full exit at TP, stop, or session/time exit.

This is intentionally conservative and designed to test whether the user's
"stable daily volatility window" idea survives fees/slippage on spot.
"""
from pathlib import Path
import math
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SRC = DATA / "btcusdt_15m_futures.csv"
OUT_TRADES = DATA / "spot_session_pullback_trades.csv"
OUT_DAILY = DATA / "spot_session_pullback_daily.csv"

CAPITAL = 10_000.0
TARGET_DAILY = 50.0
FEE_SLIP_ROUNDTRIP = 0.0012
SESSION_HOURS_CST = {20, 21, 22, 23}


def load():
    df = pd.read_csv(SRC, parse_dates=["ts"]).sort_values("ts").reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def add_features(df):
    df = df.copy()
    df["ts_cst"] = df["ts"].dt.tz_convert("Asia/Shanghai")
    df["date_cst"] = df["ts_cst"].dt.floor("1D")
    df["hour_cst"] = df["ts_cst"].dt.hour

    daily = df.set_index("ts").resample("1D").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).dropna()
    daily["ma20"] = daily["close"].rolling(20).mean()
    daily["ma60"] = daily["close"].rolling(60).mean()
    daily["daily_trend"] = (daily["close"] > daily["ma20"]) & (daily["ma20"] > daily["ma60"])
    daily_signal = daily["daily_trend"].shift(1).fillna(False).rename("daily_trend_ok")
    df["day_utc"] = df["ts"].dt.floor("1D")
    df = df.merge(daily_signal, left_on="day_utc", right_index=True, how="left")

    hourly = df.set_index("ts").resample("1h").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    ).dropna()
    hourly["ema20_1h"] = hourly["close"].ewm(span=20, adjust=False).mean()
    hourly["ema60_1h"] = hourly["close"].ewm(span=60, adjust=False).mean()
    hourly["trend_1h_ok"] = hourly["ema20_1h"] > hourly["ema60_1h"]
    hfeat = hourly[["ema20_1h", "ema60_1h", "trend_1h_ok"]].shift(1)
    df["hour_utc"] = df["ts"].dt.floor("1h")
    df = df.merge(hfeat, left_on="hour_utc", right_index=True, how="left")

    df["ema20_15m"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema60_15m"] = df["close"].ewm(span=60, adjust=False).mean()
    df["ret_1h"] = df["close"].pct_change(4)
    df["ret_4h"] = df["close"].pct_change(16)
    df["low_4h"] = df["low"].rolling(16).min().shift(1)
    df["vol_ma96"] = df["volume"].rolling(96).mean()
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr_15m"] = tr.rolling(96).mean()
    return df


def backtest(df, days=30, pullback_pct=0.003, reclaim_buffer=0.0005,
             tp_pct=0.009, sl_pct=0.006, max_hold_bars=12, alloc_pct=0.5,
             enable_rebound=True, rebound_drop_4h=0.012, rebound_tp=0.010,
             rebound_sl=0.005, rebound_alloc=0.4):
    cutoff = df["ts"] >= df["ts"].max() - pd.Timedelta(days=days)
    df = df[cutoff].copy().reset_index(drop=True)

    cash = CAPITAL
    btc = 0.0
    entry = 0.0
    entry_ts = None
    entry_mode = None
    hold = 0
    pulled_back = False
    rebound_armed = False
    trades = []
    equity_rows = []

    for r in df.itertuples(index=False):
        price = float(r.close)
        equity = cash + btc * price
        action = "HOLD"

        if btc > 0:
            hold += 1
            exit_price = None
            reason = None
            cur_tp = rebound_tp if entry_mode == "OVERSOLD_REBOUND" else tp_pct
            cur_sl = rebound_sl if entry_mode == "OVERSOLD_REBOUND" else sl_pct
            if float(r.low) <= entry * (1 - cur_sl):
                exit_price = entry * (1 - cur_sl)
                reason = "STOP"
            elif float(r.high) >= entry * (1 + cur_tp):
                exit_price = entry * (1 + cur_tp)
                reason = "TAKE"
            elif hold >= max_hold_bars or int(r.hour_cst) not in SESSION_HOURS_CST:
                exit_price = price
                reason = "TIME"

            if exit_price is not None:
                gross_cash = btc * exit_price
                fee = gross_cash * FEE_SLIP_ROUNDTRIP / 2
                cash = gross_cash - fee + cash
                pnl = cash + 0 * price - equity_rows[-1]["equity"] if equity_rows else 0.0
                # Easier and more accurate trade PnL from notional.
                trade_pnl = btc * (exit_price - entry) - (btc * entry + btc * exit_price) * FEE_SLIP_ROUNDTRIP / 2
                trades.append({
                    "entry_ts": entry_ts,
                    "exit_ts": r.ts,
                    "entry": entry,
                    "exit": exit_price,
                    "reason": reason,
                    "mode": entry_mode,
                    "pnl": trade_pnl,
                    "ret_pct": trade_pnl / (btc * entry) * 100,
                })
                btc = 0.0
                entry = 0.0
                entry_ts = None
                entry_mode = None
                hold = 0
                pulled_back = False
                rebound_armed = False
                action = reason

        if btc == 0:
            in_session = int(r.hour_cst) in SESSION_HOURS_CST
            filters_ok = bool(r.daily_trend_ok) and bool(r.trend_1h_ok)
            if in_session and filters_ok and np.isfinite(r.ema20_1h) and np.isfinite(r.ema20_15m):
                if float(r.low) <= float(r.ema20_1h) * (1 + pullback_pct):
                    pulled_back = True
                reclaim = price > float(r.ema20_15m) * (1 + reclaim_buffer)
                if pulled_back and reclaim:
                    notional = cash * alloc_pct
                    fee = notional * FEE_SLIP_ROUNDTRIP / 2
                    btc = (notional - fee) / price
                    cash -= notional
                    entry = price
                    entry_ts = r.ts
                    entry_mode = "TREND_PULLBACK"
                    hold = 0
                    action = "BUY"
            if (
                enable_rebound and btc == 0 and in_session
                and np.isfinite(r.ret_4h) and np.isfinite(r.ema20_15m)
                and np.isfinite(r.ema60_15m) and np.isfinite(r.low_4h)
            ):
                sharp_drop = float(r.ret_4h) <= -rebound_drop_4h
                fresh_low = float(r.low) <= float(r.low_4h) * 1.001
                if sharp_drop and fresh_low:
                    rebound_armed = True
                reclaim = price > float(r.ema20_15m) * (1 + reclaim_buffer)
                no_deep_downtrend = price > float(r.ema60_15m) * 0.985
                if rebound_armed and reclaim and no_deep_downtrend:
                    notional = cash * rebound_alloc
                    fee = notional * FEE_SLIP_ROUNDTRIP / 2
                    btc = (notional - fee) / price
                    cash -= notional
                    entry = price
                    entry_ts = r.ts
                    entry_mode = "OVERSOLD_REBOUND"
                    hold = 0
                    action = "BUY_REBOUND"

        equity = cash + btc * price
        equity_rows.append({"ts": r.ts, "equity": equity, "action": action, "btc": btc, "cash": cash})

    if btc > 0:
        r = df.iloc[-1]
        gross_cash = btc * float(r["close"])
        fee = gross_cash * FEE_SLIP_ROUNDTRIP / 2
        trade_pnl = btc * (float(r["close"]) - entry) - (btc * entry + btc * float(r["close"])) * FEE_SLIP_ROUNDTRIP / 2
        cash += gross_cash - fee
        trades.append({
            "entry_ts": entry_ts,
            "exit_ts": r["ts"],
            "entry": entry,
            "exit": float(r["close"]),
            "reason": "FINAL",
            "mode": entry_mode,
            "pnl": trade_pnl,
            "ret_pct": trade_pnl / (btc * entry) * 100,
        })

    eq = pd.DataFrame(equity_rows)
    trd = pd.DataFrame(trades)
    if len(eq):
        eq["date_cst"] = pd.to_datetime(eq["ts"], utc=True).dt.tz_convert("Asia/Shanghai").dt.date
    if len(trd):
        trd["entry_ts"] = pd.to_datetime(trd["entry_ts"], utc=True)
        trd["exit_ts"] = pd.to_datetime(trd["exit_ts"], utc=True)
        trd["date_cst"] = trd["exit_ts"].dt.tz_convert("Asia/Shanghai").dt.date
    return eq, trd


def summarize(eq, trades):
    daily = eq.groupby("date_cst").agg(equity=("equity", "last")).reset_index()
    daily["pnl"] = daily["equity"].diff().fillna(daily["equity"] - CAPITAL)
    ret = daily["equity"].pct_change().fillna(0)
    dd = (daily["equity"] / daily["equity"].cummax() - 1).min() if len(daily) else 0
    return {
        "days": len(daily),
        "trades": len(trades),
        "final": daily["equity"].iloc[-1] if len(daily) else CAPITAL,
        "total_pnl": (daily["equity"].iloc[-1] - CAPITAL) if len(daily) else 0,
        "avg_daily": daily["pnl"].mean() if len(daily) else 0,
        "median_daily": daily["pnl"].median() if len(daily) else 0,
        "hit_50_pct": (daily["pnl"] >= TARGET_DAILY).mean() * 100 if len(daily) else 0,
        "loss_50_pct": (daily["pnl"] <= -TARGET_DAILY).mean() * 100 if len(daily) else 0,
        "win_rate": (trades["pnl"] > 0).mean() * 100 if len(trades) else 0,
        "avg_trade": trades["pnl"].mean() if len(trades) else 0,
        "best_trade": trades["pnl"].max() if len(trades) else 0,
        "worst_trade": trades["pnl"].min() if len(trades) else 0,
        "max_dd": dd,
        "sharpe": ret.mean() * 365 / (ret.std() * math.sqrt(365)) if ret.std() > 1e-12 else np.nan,
    }, daily


def main():
    df = add_features(load())
    eq, trades = backtest(df)
    stats, daily = summarize(eq, trades)
    trades.to_csv(OUT_TRADES, index=False)
    daily.to_csv(OUT_DAILY, index=False)

    print(f"Period: {daily['date_cst'].iloc[0]} -> {daily['date_cst'].iloc[-1]} ({stats['days']} CST days)")
    print("Strategy: spot long pullback, CST 20:00-24:00, 50% capital per trade")
    print(f"Trades: {stats['trades']}  Win rate: {stats['win_rate']:.1f}%")
    print(f"Total PnL: ${stats['total_pnl']:+.2f}  Final equity: ${stats['final']:,.2f}")
    print(f"Avg daily: ${stats['avg_daily']:+.2f}  Median daily: ${stats['median_daily']:+.2f}")
    print(f"Days >= $50: {stats['hit_50_pct']:.1f}%  Days <= -$50: {stats['loss_50_pct']:.1f}%")
    print(f"Avg trade: ${stats['avg_trade']:+.2f}  Best: ${stats['best_trade']:+.2f}  Worst: ${stats['worst_trade']:+.2f}")
    print(f"MaxDD: {stats['max_dd']*100:.2f}%  Sharpe: {stats['sharpe']:.2f}")

    print("\nRecent trades:")
    if len(trades):
        cols = ["entry_ts", "exit_ts", "entry", "exit", "reason", "pnl", "ret_pct"]
        print(trades[cols].tail(12).to_string(index=False, formatters={
            "entry": "{:.2f}".format,
            "exit": "{:.2f}".format,
            "pnl": "{:+.2f}".format,
            "ret_pct": "{:+.2f}".format,
        }))
    else:
        print("  No trades.")

    print("\nDaily PnL:")
    print(daily.tail(30).to_string(index=False, formatters={
        "equity": "{:.2f}".format,
        "pnl": "{:+.2f}".format,
    }))
    print(f"\nSaved {OUT_TRADES.relative_to(ROOT)} and {OUT_DAILY.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
