"""Delta-neutral BTC funding arbitrage backtest.

Strategy
--------
  - Long 1 BTC spot, Short 1 BTC perpetual (USDT-M).
  - Position re-entered/maintained continuously across the whole history.
  - PnL per 8h period =
        funding_received_on_short                       (positive funding = we earn)
      - spot_price_drift  +  perp_price_drift           (offset; not exactly zero due to basis)
      - rebalance_fees_when_basis_drifts_too_much
  - Capital model: 1x notional on each leg, no leverage. Margin assumed 1:1 (conservative).
    Capital deployed = spot_notional + perp_margin = 2 * spot_price * qty.
    All returns reported on this deployed capital (the real "cost" of running the strategy).

Cost model
----------
  - Taker fee per leg per turnover = 4 bps default.
  - Initial entry: 2 legs * fee (one-time).
  - We assume HOLD throughout, no intra-period rebalance (8h is the funding cadence).
    Net fee drag = entry_fee, ignore exit until horizon end.
  - Slippage: 1 bp per leg, applied on entry/exit.

Outputs: prints summary + writes report.md + equity curve CSV.
"""
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"

FEE_TAKER_BPS = 4.0      # 0.04% per leg
SLIPPAGE_BPS = 1.0       # 0.01% per leg
LEGS = 2                 # spot + perp
HOURS_PER_PERIOD = 8
PERIODS_PER_YEAR = 365 * 24 / HOURS_PER_PERIOD   # 1095


def load():
    fr = pd.read_csv(DATA / "funding_btcusdt.csv")
    perp = pd.read_csv(DATA / "perp_8h_btcusdt.csv")
    spot = pd.read_csv(DATA / "spot_8h_btcusdt.csv")
    fr["ts"] = pd.to_datetime(fr["fundingTime"], utc=True, format="mixed").dt.floor("8h")
    perp["ts"] = pd.to_datetime(perp["openTime"], utc=True, format="mixed").dt.floor("8h")
    spot["ts"] = pd.to_datetime(spot["openTime"], utc=True, format="mixed").dt.floor("8h")
    fr = fr[["ts", "fundingRate", "markPrice"]]
    perp = perp.rename(columns={"close": "perp_close"})[["ts", "perp_close"]]
    spot = spot.rename(columns={"close": "spot_close"})[["ts", "spot_close"]]
    df = fr.merge(perp, on="ts", how="inner").merge(spot, on="ts", how="inner")
    df = df.dropna(subset=["fundingRate", "perp_close", "spot_close"]).reset_index(drop=True)
    return df


def backtest(df, fee_bps=FEE_TAKER_BPS, slip_bps=SLIPPAGE_BPS):
    """
    Returns per-period:
      basis_pnl_pct   -- (spot_ret - perp_ret), captures basis drift on a delta-neutral book
      funding_pnl_pct -- funding received on the short perp leg (on notional)
      net_pnl_pct     -- basis + funding, expressed on DEPLOYED CAPITAL (2x notional)
    """
    out = df.copy()
    out["spot_ret"] = out["spot_close"].pct_change().fillna(0.0)
    out["perp_ret"] = out["perp_close"].pct_change().fillna(0.0)
    # Delta-neutral leg: long spot earns spot_ret, short perp earns -perp_ret on notional.
    out["leg_pnl_on_notional"] = out["spot_ret"] - out["perp_ret"]
    # Funding: short perp RECEIVES funding when funding_rate > 0.
    out["funding_pnl_on_notional"] = out["fundingRate"]
    out["gross_pnl_on_notional"] = out["leg_pnl_on_notional"] + out["funding_pnl_on_notional"]
    # Deployed capital = 2x notional (spot $ + perp margin $).
    out["gross_pnl_on_capital"] = out["gross_pnl_on_notional"] / 2.0

    # One-time entry/exit cost on capital: (fee+slip) bps * 2 legs * 2 (entry+exit) / 2 (capital)
    entry_exit_cost = (fee_bps + slip_bps) * LEGS * 2 / 10000 / 2  # on capital
    # Apply at first and last row.
    out["fee_pnl"] = 0.0
    out.loc[out.index[0], "fee_pnl"] = -entry_exit_cost / 2
    out.loc[out.index[-1], "fee_pnl"] = -entry_exit_cost / 2

    out["net_pnl"] = out["gross_pnl_on_capital"] + out["fee_pnl"]
    out["equity"] = (1 + out["net_pnl"]).cumprod()
    return out


def summarize(bt, label="full"):
    n = len(bt)
    years = n / PERIODS_PER_YEAR
    total_ret = bt["equity"].iloc[-1] - 1
    cagr = (1 + total_ret) ** (1 / max(years, 1e-9)) - 1
    vol = bt["net_pnl"].std() * np.sqrt(PERIODS_PER_YEAR)
    sharpe = (bt["net_pnl"].mean() * PERIODS_PER_YEAR) / vol if vol > 0 else np.nan
    eq = bt["equity"]
    dd = (eq / eq.cummax() - 1).min()
    pos_funding_share = (bt["fundingRate"] > 0).mean()
    neg_funding_share = (bt["fundingRate"] < 0).mean()
    funding_only_apy = bt["fundingRate"].mean() * PERIODS_PER_YEAR / 2  # on capital
    return {
        "label": label,
        "periods": n,
        "years": round(years, 2),
        "total_return": total_ret,
        "CAGR": cagr,
        "vol_ann": vol,
        "sharpe": sharpe,
        "max_drawdown": dd,
        "pos_funding_share": pos_funding_share,
        "neg_funding_share": neg_funding_share,
        "funding_only_apy_on_capital": funding_only_apy,
        "avg_funding_rate_per_8h": bt["fundingRate"].mean(),
        "median_funding_rate_per_8h": bt["fundingRate"].median(),
    }


def fmt(s):
    return (
        f"{s['label']:>10s} | yrs={s['years']:>4.1f} | "
        f"CAGR={s['CAGR']*100:>6.2f}% | vol={s['vol_ann']*100:>5.2f}% | "
        f"Sharpe={s['sharpe']:>5.2f} | MaxDD={s['max_drawdown']*100:>6.2f}% | "
        f"FundOnly={s['funding_only_apy_on_capital']*100:>6.2f}% | "
        f"PosFunding={s['pos_funding_share']*100:>5.1f}%"
    )


def main():
    df = load()
    print(f"Loaded {len(df)} aligned 8h periods. Range: {df['ts'].min()} -> {df['ts'].max()}")
    bt = backtest(df)

    summaries = [summarize(bt, "full")]

    # Yearly breakdown
    bt["year"] = bt["ts"].dt.year
    for yr, g in bt.groupby("year"):
        if len(g) < 30:
            continue
        # rebase equity per year
        g2 = g.copy()
        g2["equity"] = (1 + g2["net_pnl"]).cumprod()
        summaries.append(summarize(g2, str(yr)))

    print()
    print("                  | yrs  | CAGR     | vol    | Sharpe | MaxDD    | FundOnly | PosFunding")
    print("-" * 100)
    for s in summaries:
        print(fmt(s))

    # Sensitivity: funding-only APY at fee variations
    print()
    print("Fee/slip sensitivity (full period, includes basis drift + funding - costs):")
    print("  fee_bps  slip_bps  CAGR")
    for fee, slip in [(2, 0.5), (4, 1), (6, 2), (10, 3)]:
        bt2 = backtest(df, fee, slip)
        s2 = summarize(bt2, f"{fee}/{slip}")
        print(f"  {fee:>5.1f}    {slip:>5.1f}    {s2['CAGR']*100:>6.2f}%")

    # Smart-entry variant: only hold when funding > threshold; flat otherwise
    print()
    print("Smart entry (only short perp when funding rate > threshold, else flat):")
    print("  threshold_per_8h  CAGR     PctTimeInMarket")
    for th in [0.0, 0.0001, 0.0002, 0.0003, 0.0005]:
        active = bt["fundingRate"] > th
        # When active: earn the period's net_pnl. When flat: earn 0 (cash, no risk).
        pnl = np.where(active, bt["net_pnl"], 0.0)
        eq = (1 + pnl).cumprod()
        years = len(bt) / PERIODS_PER_YEAR
        cagr = eq[-1] ** (1 / years) - 1
        print(f"  {th*100:>6.3f}%/8h    {cagr*100:>6.2f}%   {active.mean()*100:>5.1f}%")

    # Worst funding streaks
    print()
    print("Worst 10 single 8h funding payments (negative = we paid):")
    worst = bt.nsmallest(10, "fundingRate")[["ts", "fundingRate", "spot_close"]]
    for _, r in worst.iterrows():
        print(f"  {r['ts']}  funding={r['fundingRate']*100:>+.4f}%  spot=${r['spot_close']:,.0f}")

    # Save outputs
    bt[["ts", "fundingRate", "spot_close", "perp_close",
        "leg_pnl_on_notional", "funding_pnl_on_notional",
        "net_pnl", "equity"]].to_csv(ROOT / "data/equity_curve.csv", index=False)

    # Write report
    full = summaries[0]
    report = f"""# BTC Funding Arbitrage Backtest

Period: {df['ts'].min().date()} -> {df['ts'].max().date()}  ({full['years']} years, {full['periods']} 8h periods)

## Headline (full period, fee=4bps + slip=1bp per leg)

| metric | value |
|---|---|
| CAGR (on deployed capital = spot notional + perp margin) | **{full['CAGR']*100:.2f}%** |
| Annualized vol | {full['vol_ann']*100:.2f}% |
| Sharpe | {full['sharpe']:.2f} |
| Max drawdown | {full['max_drawdown']*100:.2f}% |
| % periods with positive funding (we earn) | {full['pos_funding_share']*100:.1f}% |
| Funding-only APY (no basis drift) | {full['funding_only_apy_on_capital']*100:.2f}% |

## Yearly breakdown

| year | CAGR | MaxDD | Funding-only | PosFunding% |
|---|---|---|---|---|
""" + "\n".join(
        f"| {s['label']} | {s['CAGR']*100:.2f}% | {s['max_drawdown']*100:.2f}% | "
        f"{s['funding_only_apy_on_capital']*100:.2f}% | {s['pos_funding_share']*100:.1f}% |"
        for s in summaries[1:]
    ) + """

## Caveats (real-world will be worse)

1. **No rebalance cost in mid-period**: a delta-neutral book diverges as price moves; in
   practice you re-hedge weekly to keep margin healthy. Each rebalance = 2 legs * fee.
2. **No funding flip exit logic**: a smart implementation withdraws when funding goes
   negative. The CAGR here is the *naive always-on* number; selective entry is higher.
3. **Spot fees > perp fees** on Binance for retail (10 vs 4 bps); we used uniform 4 bps.
4. **Liquidation tail risk**: short perp on 1:1 margin survives any move; but if you
   add leverage (3-5x typical), a 20% spike requires margin top-up or you blow up.
5. **2022 USDT depeg / FTX week**: funding flipped negative for days. See "Worst 10
   single 8h funding payments" in console output.
6. **Binance funding cap**: clamped to +/-0.75% per 8h. Real outliers got capped.
"""
    (ROOT / "report.md").write_text(report)
    print("\nWrote report.md and data/equity_curve.csv")


if __name__ == "__main__":
    main()
