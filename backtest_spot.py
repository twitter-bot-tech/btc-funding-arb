"""Spot-only BTC strategy backtest, with $100/day target framing.

Capital assumption: $100,000 starting capital.
Target: $36,500/year average = $100/day.
Required APY: 36.5%.

Strategies tested (all spot, no leverage, no shorting):
  1. Buy & Hold (baseline)
  2. SMA(50,200) Cross (golden cross / death cross, classic trend)
  3. RSI(14) Mean Reversion (buy <30, sell >70)
  4. Donchian(20) Breakout (buy on N-day high, exit on N-day low)
  5. Volatility-Targeted Trend (200d MA filter + ATR sizing)

For each: CAGR, MaxDD, Sharpe, daily P&L distribution on $100K, days >=$100, days <= -$100.

Fees: 0.10% taker per round trip leg (Binance retail spot).
Slippage: 5 bps per leg.
"""
from pathlib import Path
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"

CAPITAL = 100_000.0
DAILY_TARGET = 100.0
FEE_BPS = 10  # 0.10% per leg
SLIP_BPS = 5
COST_PER_LEG = (FEE_BPS + SLIP_BPS) / 10000


def load_daily():
    """Resample 8h spot data to daily close."""
    spot = pd.read_csv(DATA / "spot_8h_btcusdt.csv")
    spot["ts"] = pd.to_datetime(spot["openTime"], utc=True, format="mixed")
    spot["date"] = spot["ts"].dt.floor("1D")
    daily = spot.groupby("date").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index()
    return daily


def apply_signal(df, signal):
    """signal: 0 or 1 per day, applied at close. Position held overnight.
    pnl = position[t-1] * ret[t] - cost on position change.
    """
    df = df.copy()
    df["signal"] = signal
    df["ret"] = df["close"].pct_change().fillna(0)
    df["pos"] = df["signal"].shift(1).fillna(0)
    df["turnover"] = (df["pos"] - df["pos"].shift(1).fillna(0)).abs()
    df["cost"] = df["turnover"] * COST_PER_LEG
    df["strategy_ret"] = df["pos"] * df["ret"] - df["cost"]
    df["equity"] = CAPITAL * (1 + df["strategy_ret"]).cumprod()
    df["daily_pnl"] = df["equity"].diff().fillna(0)
    return df


def buy_and_hold(df):
    return apply_signal(df, np.ones(len(df)))


def sma_cross(df, fast=50, slow=200):
    f = df["close"].rolling(fast).mean()
    s = df["close"].rolling(slow).mean()
    sig = (f > s).astype(int).fillna(0).values
    return apply_signal(df, sig)


def rsi_mean_reversion(df, period=14, buy_th=30, sell_th=70):
    delta = df["close"].diff()
    up = delta.clip(lower=0).rolling(period).mean()
    dn = (-delta.clip(upper=0)).rolling(period).mean()
    rs = up / dn.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    # State machine: buy when RSI<30, sell when RSI>70
    pos = np.zeros(len(df))
    state = 0
    for i, r in enumerate(rsi.values):
        if np.isnan(r):
            pos[i] = 0
            continue
        if state == 0 and r < buy_th:
            state = 1
        elif state == 1 and r > sell_th:
            state = 0
        pos[i] = state
    return apply_signal(df, pos)


def donchian_breakout(df, window=20):
    high_n = df["high"].rolling(window).max().shift(1)
    low_n = df["low"].rolling(window).min().shift(1)
    pos = np.zeros(len(df))
    state = 0
    for i in range(len(df)):
        if np.isnan(high_n.iloc[i]):
            continue
        c = df["close"].iloc[i]
        if state == 0 and c > high_n.iloc[i]:
            state = 1
        elif state == 1 and c < low_n.iloc[i]:
            state = 0
        pos[i] = state
    return apply_signal(df, pos)


def vol_targeted_trend(df, ma=200, atr_window=20, target_vol=0.30):
    """Long only when above 200d MA. Position size scaled to hit target_vol annualized."""
    ma_v = df["close"].rolling(ma).mean()
    in_trend = (df["close"] > ma_v).astype(int)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(atr_window).mean()
    realized_vol_ann = (atr / df["close"]) * np.sqrt(365)
    # cap at 1.0 (no leverage on spot)
    size = (target_vol / realized_vol_ann).clip(0, 1).fillna(0)
    sig = (in_trend * size).fillna(0).values
    return apply_signal(df, sig)


def summarize(df, label):
    n_days = len(df)
    years = n_days / 365
    eq = df["equity"]
    total = eq.iloc[-1] / CAPITAL - 1
    cagr = (1 + total) ** (1 / max(years, 1e-9)) - 1
    daily = df["strategy_ret"]
    vol = daily.std() * np.sqrt(365)
    sharpe = daily.mean() * 365 / vol if vol > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    days_above_target = (df["daily_pnl"] >= DAILY_TARGET).sum()
    days_below_target = (df["daily_pnl"] <= -DAILY_TARGET).sum()
    avg_daily = df["daily_pnl"].mean()
    median_daily = df["daily_pnl"].median()
    worst_day = df["daily_pnl"].min()
    best_day = df["daily_pnl"].max()
    return {
        "label": label, "CAGR": cagr, "vol": vol, "sharpe": sharpe,
        "max_dd": dd, "avg_daily": avg_daily, "median_daily": median_daily,
        "best_day": best_day, "worst_day": worst_day,
        "days_above_target_pct": days_above_target / n_days * 100,
        "days_below_target_pct": days_below_target / n_days * 100,
        "final_equity": eq.iloc[-1],
        "total_return": total,
    }


def main():
    df = load_daily()
    # Use only data from 2020-01 onwards (skip thin 2019 data)
    df = df[df["date"] >= pd.Timestamp("2020-01-01", tz="UTC")].reset_index(drop=True)
    print(f"Data: {len(df)} days, {df['date'].min().date()} -> {df['date'].max().date()}")
    print(f"Capital: ${CAPITAL:,.0f}, daily target: ${DAILY_TARGET}\n")

    strategies = {
        "Buy&Hold": buy_and_hold(df),
        "SMA(50,200)": sma_cross(df),
        "RSI MeanRev": rsi_mean_reversion(df),
        "Donchian(20)": donchian_breakout(df),
        "VolTgtTrend": vol_targeted_trend(df),
    }

    print(f"  {'strategy':<15} {'CAGR':>7} {'Sharpe':>7} {'MaxDD':>8} "
          f"{'AvgDailyPnL':>12} {'MedDailyPnL':>12} {'%Days>=$100':>12} {'%Days<=-$100':>13}")
    print("-" * 110)
    rows = []
    for name, bt in strategies.items():
        s = summarize(bt, name)
        rows.append(s)
        print(f"  {s['label']:<15} {s['CAGR']*100:>6.2f}% {s['sharpe']:>7.2f} "
              f"{s['max_dd']*100:>7.2f}% "
              f"${s['avg_daily']:>10.2f} ${s['median_daily']:>10.2f} "
              f"{s['days_above_target_pct']:>11.1f}% {s['days_below_target_pct']:>12.1f}%")

    print(f"\n  Final equity from ${CAPITAL:,.0f}:")
    for s in rows:
        print(f"    {s['label']:<15} ${s['final_equity']:>14,.0f}  (best day=${s['best_day']:>10,.0f}, worst=${s['worst_day']:>10,.0f})")

    # Year-by-year breakdown of best strategy
    best = max(rows, key=lambda x: x["sharpe"])
    print(f"\n=== Best by Sharpe: {best['label']} — yearly breakdown ===")
    bt = strategies[best["label"]].copy()
    bt["year"] = bt["date"].dt.year
    print(f"  {'year':>6} {'P&L':>14} {'CAGR':>7} {'MaxDD':>8} {'%Days>=$100':>13}")
    for yr, g in bt.groupby("year"):
        if len(g) < 30: continue
        ret = (1 + g["strategy_ret"]).prod() - 1
        years = len(g) / 365
        cagr = (1 + ret) ** (1 / years) - 1
        eq = CAPITAL * (1 + g["strategy_ret"]).cumprod()
        dd = (eq / eq.cummax() - 1).min()
        pnl_total = g["daily_pnl"].sum()
        days_above = (g["daily_pnl"] >= DAILY_TARGET).mean() * 100
        print(f"  {yr:>6d} ${pnl_total:>12,.0f} {cagr*100:>6.2f}% {dd*100:>7.2f}% {days_above:>11.1f}%")

    # Save winning strategy curve
    bt.to_csv(DATA / f"spot_{best['label'].replace('(','').replace(')','').replace(',','_')}.csv", index=False)
    print("\nSaved winning strategy equity curve.")

    # Verdict
    print("\n=== VERDICT ===")
    feasible = [s for s in rows if s["CAGR"] > 0.365]
    if feasible:
        print(f"  Strategies meeting 36.5% CAGR target (= $100/day on $100K):")
        for s in feasible:
            print(f"    {s['label']}: CAGR={s['CAGR']*100:.1f}%, MaxDD={s['max_dd']*100:.1f}%")
    else:
        print(f"  NO spot strategy met 36.5% CAGR. Best was {max(rows, key=lambda x: x['CAGR'])['label']} "
              f"at {max(rows, key=lambda x: x['CAGR'])['CAGR']*100:.1f}%")
        print(f"  -> Either: increase capital, lower target, OR add leverage/derivatives.")


if __name__ == "__main__":
    main()
