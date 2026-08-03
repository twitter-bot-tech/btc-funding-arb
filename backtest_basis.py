"""Basis trade backtest: long spot BTC + short BTCUSDT quarterly futures.

Mechanics
---------
  - On day a new quarterly contract is listed (becomes NEXT_QUARTER),
    buy 1 BTC spot + sell 1 BTC of that quarterly.
  - Hold to expiry. At expiry: futures price = spot price (contango collapses).
  - Locked-in PnL = (futures_sell_price - spot_buy_price) on the basis,
    EXCLUDING spot price movement (which is hedged).
  - Roll into next quarterly on its listing day.

We use 1d close prices, spot from data/spot_8h_btcusdt.csv (resampled),
quarterlies from data/quarterly_btc.csv.

Annualized basis on a given day = (F/S - 1) * (365 / days_to_expiry)
This is the "carry" you lock in if you trade today.

Trading strategy: hold the contract with HIGHEST annualized basis (typically
NEXT_QUARTER once listed; CURRENT_QUARTER as expiry approaches goes to 0).
"""
from pathlib import Path
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"
PERIODS_PER_YEAR = 365  # daily

FEE_BPS_PER_LEG = 4
SLIP_BPS_PER_LEG = 1


def load():
    spot = pd.read_csv(DATA / "spot_8h_btcusdt.csv")
    spot["ts"] = pd.to_datetime(spot["openTime"], utc=True, format="mixed")
    # Daily spot from 00:00 UTC close (which is the 16:00 prev day open's close in 8h grid;
    # easier: daily resample)
    spot["date"] = spot["ts"].dt.floor("1D")
    spot_daily = spot.groupby("date")["close"].last().reset_index()
    spot_daily = spot_daily.rename(columns={"close": "spot"})

    q = pd.read_csv(DATA / "quarterly_btc.csv")
    q["ts"] = pd.to_datetime(q["openTime"], utc=True, format="mixed")
    q["date"] = q["ts"].dt.floor("1D")
    q["expiry"] = pd.to_datetime(q["expiry"], utc=True, format="mixed")
    q = q[["date", "symbol", "expiry", "close", "volume"]].rename(columns={"close": "fut"})
    return spot_daily, q


def compute_basis(spot, q):
    df = q.merge(spot, on="date", how="left").dropna(subset=["fut", "spot"])
    df["days_to_expiry"] = (df["expiry"] - df["date"]).dt.days
    df = df[df["days_to_expiry"] > 0].copy()
    df["basis_abs"] = df["fut"] - df["spot"]
    df["basis_pct"] = df["basis_abs"] / df["spot"]
    df["ann_basis"] = df["basis_pct"] * (365.0 / df["days_to_expiry"])
    return df


def backtest_roll(basis):
    """Each day pick the contract with highest ann_basis (must have >=14 days
    to expiry to avoid spread blow-up at the very end). 'Hold' that contract
    until either: (a) it goes to <14 days (we roll), or (b) a better contract
    appears with materially higher ann_basis (we roll).

    For simplicity: roll only at contract handoffs (when current contract
    enters last 14 days). Don't switch mid-life.
    """
    basis = basis.sort_values(["date", "ann_basis"], ascending=[True, False])
    # Build daily timeline; pick the contract with highest ann_basis that has
    # >= 14 days to expiry. If none, fall back to whichever has most days.
    rows = []
    for date, g in basis.groupby("date"):
        valid = g[g["days_to_expiry"] >= 14]
        if len(valid) == 0:
            valid = g
        pick = valid.iloc[valid["ann_basis"].argmax()]
        rows.append({
            "date": date,
            "symbol": pick["symbol"],
            "ann_basis": pick["ann_basis"],
            "days_to_expiry": pick["days_to_expiry"],
            "fut": pick["fut"],
            "spot": pick["spot"],
            "basis_pct": pick["basis_pct"],
        })
    picks = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)

    # Detect rolls (symbol change day = roll happened the day before)
    picks["prev_symbol"] = picks["symbol"].shift(1)
    picks["roll"] = (picks["symbol"] != picks["prev_symbol"]) & picks["prev_symbol"].notna()
    n_rolls = picks["roll"].sum()

    # Daily PnL (delta-neutral): we earn the BASIS DECAY between today and tomorrow,
    # on the CURRENT (unchanged) contract.
    # day-to-day pnl_on_notional = (spot_t+1 - spot_t) - (fut_t+1 - fut_t)
    # = -delta_basis_abs  (since basis = fut - spot, delta_basis < 0 means we earn)
    picks = picks.merge(
        basis[["date", "symbol", "fut", "spot"]].rename(columns={"fut": "fut_now", "spot": "spot_now"}),
        on=["date", "symbol"], how="left"
    )
    # Compute next-day pnl using the same contract (no roll yet)
    pnls = []
    for i in range(len(picks) - 1):
        cur = picks.iloc[i]
        nxt = picks.iloc[i + 1]
        if nxt["symbol"] == cur["symbol"]:
            # No roll: pnl from price changes
            spot_chg = nxt["spot"] - cur["spot"]
            fut_chg = nxt["fut"] - cur["fut"]
            pnl_notional = (spot_chg - fut_chg) / cur["spot"]  # on notional
        else:
            # Roll: assume we close at today's prices and open new at today's prices
            # Capture all remaining basis on closing contract:
            # Close pnl = (cur.fut - cur.spot) close-out, but we're not at expiry,
            # so actual close uses today's mark prices, which we already capture.
            # Simplification: treat as zero P&L on roll day + pay fees.
            pnl_notional = 0.0
        pnls.append(pnl_notional)
    pnls.append(0.0)
    picks["pnl_on_notional"] = pnls
    # Capital = 2x notional (spot + futures margin), so divide by 2.
    picks["pnl_on_capital"] = picks["pnl_on_notional"] / 2

    # Fees: 1 roll = 2 legs closed + 2 legs opened = 4 leg-events, but in reality
    # spot leg is held (no need to sell spot, just roll the futures leg). So 1 roll
    # = 2 futures-leg events (close old, open new). Fee = 2 * (fee + slip) bps.
    fee_per_roll = (FEE_BPS_PER_LEG + SLIP_BPS_PER_LEG) * 2 / 10000  # on notional
    # Initial entry: 1 spot + 1 fut = 2 legs * (fee+slip)
    initial_cost = (FEE_BPS_PER_LEG + SLIP_BPS_PER_LEG) * 2 / 10000
    # Exit at end: same
    exit_cost = initial_cost

    picks["fees_on_notional"] = 0.0
    picks.loc[picks["roll"], "fees_on_notional"] = -fee_per_roll
    picks.loc[picks.index[0], "fees_on_notional"] += -initial_cost
    picks.loc[picks.index[-1], "fees_on_notional"] += -exit_cost
    picks["fees_on_capital"] = picks["fees_on_notional"] / 2

    picks["net_pnl"] = picks["pnl_on_capital"] + picks["fees_on_capital"]
    picks["equity"] = (1 + picks["net_pnl"]).cumprod()

    return picks, n_rolls


def summarize(picks, label="full"):
    eq = picks["equity"]
    n_days = len(picks)
    years = n_days / 365
    total_ret = eq.iloc[-1] - 1
    cagr = (1 + total_ret) ** (1 / max(years, 1e-9)) - 1
    vol = picks["net_pnl"].std() * np.sqrt(365)
    sharpe = (picks["net_pnl"].mean() * 365) / vol if vol > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    avg_ann_basis = picks["ann_basis"].mean()
    return {
        "label": label, "days": n_days, "years": round(years, 2),
        "CAGR": cagr, "vol": vol, "sharpe": sharpe, "max_dd": dd,
        "avg_ann_basis": avg_ann_basis,
    }


def main():
    spot, q = load()
    print(f"Spot daily: {len(spot)} rows, {spot['date'].min().date()} -> {spot['date'].max().date()}")
    print(f"Quarterly contracts: {q['symbol'].nunique()}, {len(q)} contract-days")

    basis = compute_basis(spot, q)

    print(f"\nBasis stats (annualized, on contract days):")
    print(f"  mean = {basis['ann_basis'].mean()*100:.2f}%")
    print(f"  median = {basis['ann_basis'].median()*100:.2f}%")
    print(f"  std = {basis['ann_basis'].std()*100:.2f}%")
    print(f"  positive (contango): {(basis['ann_basis']>0).mean()*100:.1f}% of days")
    print(f"  negative (backwardation): {(basis['ann_basis']<0).mean()*100:.1f}% of days")

    print(f"\nBasis by year (annualized %, mean across all contracts available):")
    basis["year"] = basis["date"].dt.year
    by_year = basis.groupby("year")["ann_basis"].agg(["mean", "median", "min", "max"])
    for yr, r in by_year.iterrows():
        print(f"  {yr}: mean={r['mean']*100:>6.2f}%  median={r['median']*100:>6.2f}%  "
              f"min={r['min']*100:>+7.2f}%  max={r['max']*100:>6.2f}%")

    print("\n=== Backtest (always hold contract with highest ann basis >=14 days TTE) ===")
    picks, n_rolls = backtest_roll(basis)
    full = summarize(picks)
    print(f"  Period: {picks['date'].min().date()} -> {picks['date'].max().date()}")
    print(f"  Days: {full['days']}, Rolls: {n_rolls}")
    print(f"  CAGR (on deployed capital = 2x notional): {full['CAGR']*100:.2f}%")
    print(f"  Annualized vol: {full['vol']*100:.2f}%")
    print(f"  Sharpe: {full['sharpe']:.2f}")
    print(f"  Max drawdown: {full['max_dd']*100:.2f}%")
    print(f"  Avg annualized basis captured: {full['avg_ann_basis']*100:.2f}%")

    # Yearly breakdown
    picks["year"] = picks["date"].dt.year
    print("\n  Yearly CAGR:")
    for yr, g in picks.groupby("year"):
        if len(g) < 30: continue
        g2 = g.copy()
        g2["equity"] = (1 + g2["net_pnl"]).cumprod()
        s = summarize(g2, str(yr))
        print(f"    {yr}: CAGR={s['CAGR']*100:>6.2f}%  MaxDD={s['max_dd']*100:>6.2f}%  "
              f"AvgBasis={s['avg_ann_basis']*100:>6.2f}%")

    # Best/worst rolls
    print("\nLowest 5 ann_basis days (where backwardation was deepest):")
    worst = basis.nsmallest(5, "ann_basis")[["date", "symbol", "ann_basis", "days_to_expiry", "spot"]]
    for _, r in worst.iterrows():
        print(f"  {r['date'].date()}  {r['symbol']}  basis={r['ann_basis']*100:+.2f}% "
              f"({r['days_to_expiry']} days)  spot=${r['spot']:,.0f}")

    print("\nHighest 5 ann_basis days (best entry):")
    best = basis.nlargest(5, "ann_basis")[["date", "symbol", "ann_basis", "days_to_expiry", "spot"]]
    for _, r in best.iterrows():
        print(f"  {r['date'].date()}  {r['symbol']}  basis={r['ann_basis']*100:+.2f}% "
              f"({r['days_to_expiry']} days)  spot=${r['spot']:,.0f}")

    # Save outputs
    picks.to_csv(DATA / "basis_equity.csv", index=False)
    basis.to_csv(DATA / "basis_curve.csv", index=False)
    print("\nSaved data/basis_equity.csv and data/basis_curve.csv")


if __name__ == "__main__":
    main()
