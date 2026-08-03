"""Portfolio-level backtest for the BTC quant stack.

Combines the existing sleeves into one capital allocation simulation:
  - Vol-targeted BTC trend
  - Best-venue funding carry
  - Quarterly futures basis carry
  - Conservative fixed-yield sleeve
  - Cash

The point is not to overfit a new signal. It checks whether the daily
allocation policy is sane after fees and drawdowns, using the same data files
that feed the dashboard/super-agent.
"""
from pathlib import Path
import math
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"

CAPITAL = 100_000.0
CASH_APY = 0.05
YIELD_APY = 0.09
FUNDING_MIN_APY = 0.065
BASIS_MIN_APY = 0.065


def load_trend():
    p = DATA / "spot_VolTgtTrend.csv"
    if not p.exists():
        raise FileNotFoundError("Run backtest_spot.py first to create data/spot_VolTgtTrend.csv")
    df = pd.read_csv(p, parse_dates=["date"])
    return df[["date", "strategy_ret", "pos"]].rename(
        columns={"strategy_ret": "trend_ret", "pos": "trend_pos"}
    )


def load_funding():
    p = DATA / "funding_multi_exchange.csv"
    if not p.exists():
        p = DATA / "funding_btcusdt.csv"
    raw = pd.read_csv(p)
    ts_col = "ts" if "ts" in raw.columns else "fundingTime"
    raw["ts"] = pd.to_datetime(raw[ts_col], utc=True, format="mixed")
    raw["date"] = raw["ts"].dt.floor("1D")

    venue_cols = [c for c in raw.columns if c.startswith("fr_")]
    if not venue_cols and "fundingRate" in raw.columns:
        venue_cols = ["fundingRate"]

    rows = []
    hist = raw.sort_values("ts").reset_index(drop=True)
    for date, g in hist.groupby("date"):
        prior = hist[hist["ts"] <= g["ts"].max()].tail(21)
        best = None
        for col in venue_cols:
            vals = prior[col].dropna()
            if len(vals) < 9:
                continue
            apy = vals.mean() * 365 * 3 / 2
            day_rates = g[col].dropna()
            if len(day_rates) == 0:
                continue
            # Long spot + short perp earns positive funding. Capital is spot
            # notional plus perp margin, so returns are divided by 2.
            realized = day_rates.sum() / 2
            cand = (apy, col.replace("fr_", ""), realized)
            if best is None or cand[0] > best[0]:
                best = cand
        if best is None:
            rows.append({"date": date, "funding_apy": 0.0, "funding_ret": 0.0, "funding_venue": ""})
        else:
            rows.append({"date": date, "funding_apy": best[0], "funding_ret": best[2], "funding_venue": best[1]})
    return pd.DataFrame(rows)


def load_basis():
    p = DATA / "basis_equity.csv"
    if not p.exists():
        raise FileNotFoundError("Run backtest_basis.py first to create data/basis_equity.csv")
    df = pd.read_csv(p, parse_dates=["date"])
    return df[["date", "ann_basis", "net_pnl", "symbol"]].rename(
        columns={"net_pnl": "basis_ret", "symbol": "basis_symbol"}
    )


def max_drawdown(equity):
    return (equity / equity.cummax() - 1).min()


def summarize(df, ret_col, label):
    eq = CAPITAL * (1 + df[ret_col].fillna(0)).cumprod()
    years = len(df) / 365
    total = eq.iloc[-1] / CAPITAL - 1
    cagr = (1 + total) ** (1 / max(years, 1e-9)) - 1
    vol = df[ret_col].std() * math.sqrt(365)
    sharpe = df[ret_col].mean() * 365 / vol if vol > 1e-12 else np.nan
    dd = max_drawdown(eq)
    return {
        "label": label,
        "cagr": cagr,
        "vol": vol,
        "sharpe": sharpe,
        "max_dd": dd,
        "final": eq.iloc[-1],
    }


def fmt(s):
    return (
        f"  {s['label']:<22} CAGR={s['cagr']*100:>6.2f}% "
        f"Sharpe={s['sharpe']:>5.2f} Vol={s['vol']*100:>5.2f}% "
        f"MaxDD={s['max_dd']*100:>7.2f}% Final=${s['final']:>10,.0f}"
    )


def build_portfolio():
    trend = load_trend()
    funding = load_funding()
    basis = load_basis()

    df = trend.merge(funding, on="date", how="left").merge(basis, on="date", how="left")
    df = df[df["date"] >= pd.Timestamp("2021-02-03", tz="UTC")].copy()
    df = df.sort_values("date").reset_index(drop=True)

    df["cash_ret"] = CASH_APY / 365
    df["yield_ret"] = YIELD_APY / 365
    df[["funding_apy", "funding_ret", "ann_basis", "basis_ret"]] = (
        df[["funding_apy", "funding_ret", "ann_basis", "basis_ret"]].fillna(0)
    )

    # Conservative allocation policy. Keep directional BTC capped and only
    # deploy carry when it beats the cash floor by a real margin.
    df["w_trend"] = np.where(df["trend_pos"] > 0, 0.40, 0.00)
    df["w_funding"] = np.where(df["funding_apy"] >= FUNDING_MIN_APY, 0.25, 0.00)
    df["w_basis"] = np.where(df["ann_basis"] >= BASIS_MIN_APY, 0.20, 0.00)
    df["w_yield"] = 0.30

    risky_sum = df[["w_trend", "w_funding", "w_basis", "w_yield"]].sum(axis=1)
    scale = np.minimum(1.0, 1.0 / risky_sum.replace(0, np.nan)).fillna(1.0)
    for col in ["w_trend", "w_funding", "w_basis", "w_yield"]:
        df[col] *= scale
    df["w_cash"] = 1 - df[["w_trend", "w_funding", "w_basis", "w_yield"]].sum(axis=1)

    df["portfolio_ret"] = (
        df["w_trend"] * df["trend_ret"]
        + df["w_funding"] * df["funding_ret"]
        + df["w_basis"] * df["basis_ret"]
        + df["w_yield"] * df["yield_ret"]
        + df["w_cash"] * df["cash_ret"]
    )
    df["equity"] = CAPITAL * (1 + df["portfolio_ret"]).cumprod()
    df["daily_pnl"] = df["equity"].diff().fillna(0)
    return df


def main():
    df = build_portfolio()
    print(f"Period: {df['date'].min().date()} -> {df['date'].max().date()}, {len(df)} days")
    print(f"Cash APY={CASH_APY*100:.1f}%  Yield sleeve APY={YIELD_APY*100:.1f}%")
    print(f"Funding gate={FUNDING_MIN_APY*100:.1f}%  Basis gate={BASIS_MIN_APY*100:.1f}%\n")

    cash_only = df.assign(cash_only_ret=CASH_APY / 365)
    yield_cash = df.assign(yield_cash_ret=0.30 * (YIELD_APY / 365) + 0.70 * (CASH_APY / 365))
    print("Headline:")
    print(fmt(summarize(cash_only, "cash_only_ret", "Cash only")))
    print(fmt(summarize(yield_cash, "yield_cash_ret", "30% yield + cash")))
    print(fmt(summarize(df, "portfolio_ret", "Policy portfolio")))

    print("\nYearly breakdown (Policy portfolio):")
    print(f"  {'year':>6} {'P&L':>12} {'CAGR':>8} {'MaxDD':>8} {'Avg trend':>10} {'Avg fund':>9} {'Avg basis':>10} {'Avg cash':>9}")
    for yr, g in df.groupby(df["date"].dt.year):
        if len(g) < 30:
            continue
        eq = CAPITAL * (1 + g["portfolio_ret"]).cumprod()
        ret = eq.iloc[-1] / CAPITAL - 1
        years = len(g) / 365
        cagr = (1 + ret) ** (1 / years) - 1
        dd = max_drawdown(eq)
        pnl = eq.iloc[-1] - CAPITAL
        print(
            f"  {yr:>6d} ${pnl:>10,.0f} {cagr*100:>7.2f}% {dd*100:>7.2f}% "
            f"{g['w_trend'].mean()*100:>9.1f}% {g['w_funding'].mean()*100:>8.1f}% "
            f"{g['w_basis'].mean()*100:>9.1f}% {g['w_cash'].mean()*100:>8.1f}%"
        )

    latest = df.iloc[-1]
    print("\nLatest allocation:")
    print(f"  Date: {latest['date'].date()}  Equity=${latest['equity']:,.0f}")
    print(f"  BTC trend: {latest['w_trend']*100:.0f}%")
    print(f"  Funding:   {latest['w_funding']*100:.0f}% ({latest.get('funding_venue', '')}, trailing APY {latest['funding_apy']*100:.2f}%)")
    print(f"  Basis:     {latest['w_basis']*100:.0f}% ({latest.get('basis_symbol', '')}, ann {latest['ann_basis']*100:.2f}%)")
    print(f"  Yield PT:  {latest['w_yield']*100:.0f}%")
    print(f"  Cash:      {latest['w_cash']*100:.0f}%")

    out = DATA / "portfolio_equity.csv"
    df.to_csv(out, index=False)
    print(f"\nSaved {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
