"""Combined strategy: Donchian trend + Range mean-reversion overlay.

Hypothesis: When Donchian is flat (no breakout), price is range-bound.
Use a Bollinger Band mean-reversion to capture chop. When Donchian
fires (breakout), switch fully to trend mode.

Rules
-----
- DONCHIAN(20) trend signal: long when close > 20d high, exit when < 20d low.
- BB(20, 2) overlay (only active when Donchian is flat):
    - Enter long when close < lower BB (oversold in range)
    - Exit when close touches mid BB (20-day SMA)
    - Stop-loss: if Donchian fires SELL (close < 20d low), exit overlay too.

Two backtests compared:
  A) Donchian only (already in backtest_spot.py)
  B) Donchian + Range overlay (this script)

Capital: $100K. Fees: 10bps + 5bps slippage per leg.
"""
from pathlib import Path
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"
CAPITAL = 100_000.0
TARGET_DAILY = 100.0
COST_PER_LEG = (10 + 5) / 10000


def load_daily():
    spot = pd.read_csv(DATA / "spot_8h_btcusdt.csv")
    spot["ts"] = pd.to_datetime(spot["openTime"], utc=True, format="mixed")
    spot["date"] = spot["ts"].dt.floor("1D")
    d = spot.groupby("date").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
    ).reset_index()
    return d[d["date"] >= pd.Timestamp("2020-01-01", tz="UTC")].reset_index(drop=True)


def donchian_signal(df, window=20):
    h = df["high"].rolling(window).max().shift(1)
    l = df["low"].rolling(window).min().shift(1)
    pos = np.zeros(len(df))
    state = 0
    for i in range(len(df)):
        if np.isnan(h.iloc[i]):
            continue
        c = df["close"].iloc[i]
        if state == 0 and c > h.iloc[i]:
            state = 1
        elif state == 1 and c < l.iloc[i]:
            state = 0
        pos[i] = state
    return pd.Series(pos, index=df.index), h, l


def bb(df, window=20, k=2):
    mid = df["close"].rolling(window).mean()
    std = df["close"].rolling(window).std()
    return mid, mid - k * std, mid + k * std


def combo_signal(df):
    """Return position vector (0..1)."""
    don_pos, don_high, don_low = donchian_signal(df, 20)
    mid, lower, upper = bb(df, 20, 2)
    pos = np.zeros(len(df))
    state = "flat"
    for i in range(len(df)):
        if np.isnan(don_high.iloc[i]) or np.isnan(lower.iloc[i]):
            continue
        c = df["close"].iloc[i]
        # Priority: Donchian trend trumps overlay
        if don_pos.iloc[i] == 1:
            state = "trend"
            pos[i] = 1.0
            continue
        # Donchian flat -> consider overlay
        if state == "trend":
            # Donchian just exited; flatten and let overlay decide next bar
            state = "flat"
        if state == "flat" and c < lower.iloc[i]:
            state = "range_long"
        elif state == "range_long" and c >= mid.iloc[i]:
            state = "flat"
        elif state == "range_long" and c < don_low.iloc[i]:
            # Stop: hard breakdown means trend going down, exit
            state = "flat"
        pos[i] = 0.5 if state == "range_long" else 0.0  # 50% size on overlay (smaller, riskier)
    return pd.Series(pos, index=df.index)


def run(df, sig, label):
    df = df.copy()
    df["pos"] = sig.shift(1).fillna(0)
    df["ret"] = df["close"].pct_change().fillna(0)
    df["turnover"] = (df["pos"] - df["pos"].shift(1).fillna(0)).abs()
    df["cost"] = df["turnover"] * COST_PER_LEG
    df["pnl_pct"] = df["pos"] * df["ret"] - df["cost"]
    df["equity"] = CAPITAL * (1 + df["pnl_pct"]).cumprod()
    df["daily_pnl"] = df["equity"].diff().fillna(0)
    return df


def summarize(d, label):
    eq = d["equity"]
    years = len(d) / 365
    total = eq.iloc[-1] / CAPITAL - 1
    cagr = (1 + total) ** (1 / max(years, 1e-9)) - 1
    vol = d["pnl_pct"].std() * np.sqrt(365)
    sharpe = d["pnl_pct"].mean() * 365 / vol if vol > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    pct_in_market = (d["pos"] > 0).mean() * 100
    days_hit = (d["daily_pnl"] >= TARGET_DAILY).mean() * 100
    return dict(label=label, CAGR=cagr, sharpe=sharpe, vol=vol, max_dd=dd,
                final=eq.iloc[-1], pct_in_market=pct_in_market, days_hit_target=days_hit)


def fmt(s):
    return (f"  {s['label']:<18} CAGR={s['CAGR']*100:>6.2f}% Sharpe={s['sharpe']:>5.2f} "
            f"Vol={s['vol']*100:>5.2f}% MaxDD={s['max_dd']*100:>7.2f}% "
            f"Final=${s['final']:>10,.0f} InMkt={s['pct_in_market']:>4.1f}% "
            f"Days>=$100={s['days_hit_target']:>4.1f}%")


def main():
    df = load_daily()
    print(f"Period: {df['date'].min().date()} -> {df['date'].max().date()}, {len(df)} days\n")

    # A: Donchian only
    don_sig, _, _ = donchian_signal(df, 20)
    a = run(df, don_sig, "Donchian only")
    sa = summarize(a, "Donchian only")

    # B: Combo
    cb_sig = combo_signal(df)
    b = run(df, cb_sig, "Donchian+Range")
    sb = summarize(b, "Donchian+Range")

    # Baseline buy&hold
    bh = run(df, pd.Series(np.ones(len(df)), index=df.index), "BuyHold")
    sbh = summarize(bh, "BuyHold")

    print("Headline:")
    print(fmt(sbh))
    print(fmt(sa))
    print(fmt(sb))

    # Year by year for combo
    print(f"\nYearly breakdown (Donchian+Range):")
    b["year"] = b["date"].dt.year
    print(f"  {'year':>6} {'P&L':>14} {'CAGR':>7} {'MaxDD':>8} {'InMkt%':>8} {'Days>=$100':>11}")
    for yr, g in b.groupby("year"):
        if len(g) < 30: continue
        g2 = g.copy()
        g2["eq2"] = CAPITAL * (1 + g2["pnl_pct"]).cumprod()
        ret = g2["eq2"].iloc[-1] / CAPITAL - 1
        years = len(g2) / 365
        cagr = (1 + ret) ** (1 / years) - 1
        dd = (g2["eq2"] / g2["eq2"].cummax() - 1).min()
        pnl = g["daily_pnl"].sum()
        inmkt = (g["pos"] > 0).mean() * 100
        hit = (g["daily_pnl"] >= TARGET_DAILY).mean() * 100
        print(f"  {yr:>6d} ${pnl:>12,.0f} {cagr*100:>6.2f}% {dd*100:>7.2f}% {inmkt:>7.1f}% {hit:>10.1f}%")

    # 2026 YTD focus
    cur = b[b["date"] >= pd.Timestamp("2026-01-01", tz="UTC")]
    if len(cur) > 0:
        print(f"\n2026 YTD ({len(cur)} days):")
        print(f"  Donchian+Range P&L: ${cur['daily_pnl'].sum():+,.2f}")
        print(f"  Trades fired:       {(cur['turnover'] > 0).sum()}")

    # Save current signal state for the live system
    last = b.tail(5)[["date", "close", "pos", "pnl_pct"]]
    print("\nLast 5 days signal preview:")
    for _, r in last.iterrows():
        print(f"  {r['date'].date()}  close=${r['close']:,.0f}  pos={r['pos']:.2f}")

    b.to_csv(DATA / "combo_equity.csv", index=False)
    print("\nSaved data/combo_equity.csv")


if __name__ == "__main__":
    main()
