"""Multi-exchange funding rate comparison + 'best-of' delta-neutral backtest.

Key questions:
  1. Does OKX or Bybit pay materially higher funding than Binance?
  2. Does a 'short on whichever exchange pays most this period' strategy
     boost CAGR vs Binance-only?

Notes on cadence:
  - Binance: 8h funding throughout history.
  - Bybit:   8h until 2024-01-08, then 4h.
  - OKX:     8h until 2023-09-13, then 4h.
We aggregate 4h funding into 8h sums (sum of 2 consecutive 4h rates) for fair
comparison. Equivalent total daily cost is preserved.
"""
from pathlib import Path
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data"
PERIODS_PER_YEAR_8H = 365 * 24 / 8  # 1095


def load_8h(path, label):
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="mixed")
    df = df[["ts", "fundingRate"]].dropna()
    # Bucket to 8h grid; if exchange now uses 4h, sum the 2 sub-periods.
    df["bucket"] = df["ts"].dt.floor("8h")
    agg = df.groupby("bucket")["fundingRate"].sum().reset_index()
    agg = agg.rename(columns={"bucket": "ts", "fundingRate": f"fr_{label}"})
    return agg


def main():
    bn = pd.read_csv(DATA / "funding_btcusdt.csv")
    bn["ts"] = pd.to_datetime(bn["fundingTime"], utc=True, format="mixed").dt.floor("8h")
    bn = bn[["ts", "fundingRate"]].rename(columns={"fundingRate": "fr_binance"})

    okx = load_8h(DATA / "funding_okx_btc.csv", "okx")
    by = load_8h(DATA / "funding_bybit_btc.csv", "bybit")

    df = bn.merge(by, on="ts", how="outer").merge(okx, on="ts", how="outer").sort_values("ts")
    df = df.reset_index(drop=True)

    print("Coverage:")
    for col in ["fr_binance", "fr_bybit", "fr_okx"]:
        nz = df[col].notna().sum()
        if nz > 0:
            sub = df.dropna(subset=[col])
            print(f"  {col:>11s}: {nz} obs, {sub['ts'].min()} -> {sub['ts'].max()}")

    # === Overlap window: where ALL 3 exchanges have data ===
    overlap = df.dropna(subset=["fr_binance", "fr_bybit", "fr_okx"]).copy()
    print(f"\n3-way overlap: {len(overlap)} periods, "
          f"{overlap['ts'].min()} -> {overlap['ts'].max()}")

    print("\nFunding rate stats per 8h (in basis points, on overlap window):")
    print(f"  {'':>10s} {'mean_bps':>10s} {'median_bps':>11s} {'pos_share':>10s} {'apy_funding':>12s}")
    for col, name in [("fr_binance", "Binance"), ("fr_bybit", "Bybit"), ("fr_okx", "OKX")]:
        x = overlap[col]
        apy = x.mean() * PERIODS_PER_YEAR_8H * 100  # on notional
        print(f"  {name:>10s} {x.mean()*10000:>10.3f} {x.median()*10000:>11.3f}"
              f" {(x > 0).mean()*100:>9.1f}% {apy:>11.2f}%")

    # === 2-way overlap Binance vs Bybit (much longer history) ===
    bb_overlap = df.dropna(subset=["fr_binance", "fr_bybit"]).copy()
    print(f"\nBinance vs Bybit overlap: {len(bb_overlap)} periods, "
          f"{bb_overlap['ts'].min()} -> {bb_overlap['ts'].max()}")

    print("\nBy year (mean funding rate per 8h, bps, on notional):")
    print(f"  {'year':>6s} {'Binance':>10s} {'Bybit':>10s} {'best_of_2':>11s} {'lift_pct':>10s}")
    bb_overlap["year"] = bb_overlap["ts"].dt.year
    for yr, g in bb_overlap.groupby("year"):
        if len(g) < 30:
            continue
        bn_m = g["fr_binance"].mean() * 10000
        by_m = g["fr_bybit"].mean() * 10000
        best = g[["fr_binance", "fr_bybit"]].max(axis=1).mean() * 10000
        lift = (best / bn_m - 1) * 100 if bn_m > 0 else float("nan")
        print(f"  {yr:>6d} {bn_m:>10.3f} {by_m:>10.3f} {best:>11.3f} {lift:>9.1f}%")

    # === Best-of-2 strategy: each period, short on the exchange paying highest funding ===
    print("\n=== Best-of-2 (Binance / Bybit) delta-neutral backtest ===")
    bb = bb_overlap.copy()
    bb["best_of_2"] = bb[["fr_binance", "fr_bybit"]].max(axis=1)
    fee_per_switch = (4 + 1) * 2 / 10000  # 4bps fee + 1bp slip, 2 legs, when we switch perp venue
    bb["best_choice"] = np.where(bb["fr_bybit"] > bb["fr_binance"], "bybit", "binance")
    bb["switched"] = bb["best_choice"] != bb["best_choice"].shift(1)
    n_switches = bb["switched"].sum()
    switch_cost_total = n_switches * fee_per_switch
    years = len(bb) / PERIODS_PER_YEAR_8H

    # Naive (no switch cost): just take max funding each period
    naive_pnl_per_period = bb["best_of_2"] / 2  # on deployed capital (notional*2)
    naive_apy = naive_pnl_per_period.mean() * PERIODS_PER_YEAR_8H

    # Realistic: subtract switching cost prorated
    cost_per_period = switch_cost_total / 2 / len(bb)  # spread across period, on capital
    realistic_apy = naive_apy - cost_per_period * PERIODS_PER_YEAR_8H

    binance_only_apy = bb["fr_binance"].mean() * PERIODS_PER_YEAR_8H / 2
    bybit_only_apy = bb["fr_bybit"].mean() * PERIODS_PER_YEAR_8H / 2

    print(f"  Period: {bb['ts'].min().date()} -> {bb['ts'].max().date()} ({years:.1f} years, {len(bb)} obs)")
    print(f"  Switches: {n_switches} ({n_switches/len(bb)*100:.1f}% of periods)")
    print(f"  APY (funding-only, on capital):")
    print(f"    Binance only:               {binance_only_apy*100:>6.2f}%")
    print(f"    Bybit only:                 {bybit_only_apy*100:>6.2f}%")
    print(f"    Best-of-2 naive:            {naive_apy*100:>6.2f}%")
    print(f"    Best-of-2 net of switching: {realistic_apy*100:>6.2f}%")
    lift = (realistic_apy / binance_only_apy - 1) * 100
    print(f"  Lift vs Binance-only: {lift:+.1f}%")

    # 2026 YTD focused
    bb2026 = bb[bb["ts"].dt.year == 2026]
    if len(bb2026) > 0:
        print(f"\n=== 2026 YTD ({len(bb2026)} periods) ===")
        for col, name in [("fr_binance", "Binance"), ("fr_bybit", "Bybit"), ("best_of_2", "Best-of-2")]:
            apy = bb2026[col].mean() * PERIODS_PER_YEAR_8H / 2 * 100
            print(f"  {name:>14s}: {apy:>6.2f}% APY")

    # OKX overlap (3-way) snapshot
    if len(overlap) > 0:
        print(f"\n=== 3-way overlap ({len(overlap)} periods, {overlap['ts'].min().date()} -> {overlap['ts'].max().date()}) ===")
        for col, name in [("fr_binance", "Binance"), ("fr_bybit", "Bybit"), ("fr_okx", "OKX")]:
            apy = overlap[col].mean() * PERIODS_PER_YEAR_8H / 2 * 100
            print(f"  {name:>10s}-only: {apy:>6.2f}% APY")
        overlap["best_of_3"] = overlap[["fr_binance", "fr_bybit", "fr_okx"]].max(axis=1)
        best3_apy = overlap["best_of_3"].mean() * PERIODS_PER_YEAR_8H / 2 * 100
        print(f"  Best-of-3:    {best3_apy:>6.2f}% APY (naive, ignoring switching)")

    df.to_csv(DATA / "funding_multi_exchange.csv", index=False)
    print("\nSaved data/funding_multi_exchange.csv")


if __name__ == "__main__":
    main()
