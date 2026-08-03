"""Backfill missing portfolio_ledger.csv rows for skipped days.

Reads historical CSVs (spot, funding, basis, F&G) and computes the same
decision logic as super_agent.py for each missing date, then appends.
"""
import json
import math
from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path(__file__).parent
DATA = ROOT / "data"
LEDGER = ROOT / "portfolio_ledger.csv"

CAPITAL = 10_000.0
DAILY_TARGET = 50.0


def load_all():
    # Spot 8h, resample to daily close
    spot = pd.read_csv(DATA / "spot_8h_btcusdt.csv")
    spot["ts"] = pd.to_datetime(spot["openTime"], utc=True, format="mixed")
    spot["date"] = spot["ts"].dt.floor("1D")
    daily = spot.groupby("date").agg(
        high=("high", "max"), low=("low", "min"), close=("close", "last"),
    ).reset_index()
    daily["d20_high"] = daily["high"].rolling(20).max().shift(1)
    daily["d20_low"] = daily["low"].rolling(20).min().shift(1)

    # 14d vol
    daily["ret"] = daily["close"].pct_change()
    daily["vol_14d"] = daily["ret"].rolling(14).std() * np.sqrt(365)

    # Funding (each exchange) → daily mean per 8h period, then 7d rolling APY
    def load_fund(path, col):
        df = pd.read_csv(path)
        # Different files have different schemas
        if "fundingTime" in df.columns:
            df["ts"] = pd.to_datetime(df["fundingTime"], utc=True, format="mixed")
            r = df["fundingRate"].astype(float)
        elif "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"], utc=True, format="mixed")
            r = df["fundingRate"].astype(float)
        else:
            raise ValueError(f"Unknown schema for {path}")
        df["date"] = df["ts"].dt.floor("1D")
        df["fr"] = r
        per_day = df.groupby("date")["fr"].sum().reset_index()  # sum of 3 8h periods = daily total
        # 7d rolling APY: average daily * 365 / 2 (on capital = 2x notional)
        per_day[col] = per_day["fr"].rolling(7).mean() * 365 / 2
        return per_day[["date", col]]

    fb = load_fund(DATA / "funding_btcusdt.csv", "binance_apy")
    fy = load_fund(DATA / "funding_bybit_btc.csv", "bybit_apy")
    fo = load_fund(DATA / "funding_okx_btc.csv", "okx_apy") if (DATA / "funding_okx_btc.csv").exists() else None

    df = daily.merge(fb, on="date", how="left").merge(fy, on="date", how="left")
    if fo is not None:
        df = df.merge(fo, on="date", how="left")

    # Quarterly basis: pick contract with >=14 days to expiry, max annualized basis
    qf = pd.read_csv(DATA / "quarterly_btc.csv")
    qf["date"] = pd.to_datetime(qf["openTime"], utc=True, format="mixed").dt.floor("1D")
    qf["expiry"] = pd.to_datetime(qf["expiry"], utc=True, format="mixed")
    qf["fut"] = qf["close"].astype(float)
    qf = qf[["date", "symbol", "expiry", "fut"]]
    spot_d = daily[["date", "close"]].rename(columns={"close": "spot"})
    qf = qf.merge(spot_d, on="date", how="left").dropna()
    qf["dte"] = (qf["expiry"] - qf["date"]).dt.days
    qf = qf[qf["dte"] >= 14].copy()
    qf["ann_basis"] = (qf["fut"] / qf["spot"] - 1) * 365 / qf["dte"]
    # For each date, pick contract with max ann_basis
    best = qf.sort_values("ann_basis", ascending=False).drop_duplicates("date", keep="first")
    df = df.merge(best[["date", "ann_basis"]], on="date", how="left")

    return df


def decide(row):
    """Reproduce super_agent decide_allocation."""
    alloc = {"btc": 0, "fund": 0, "basis": 0, "cash": 0}
    # Donchian
    close = row["close"]
    signal = "HOLD"
    if pd.notna(row["d20_high"]) and close > row["d20_high"]:
        signal = "BUY"
        alloc["btc"] = 60
    elif pd.notna(row["d20_low"]) and close < row["d20_low"]:
        signal = "SELL"

    # Best funding
    funds = []
    for v in ("binance_apy", "bybit_apy", "okx_apy"):
        if v in row and pd.notna(row[v]):
            funds.append((v.split("_")[0], float(row[v])))
    best_v, best_apy = ("binance", 0.0)
    if funds:
        best_v, best_apy = max(funds, key=lambda x: x[1])

    if best_apy > 0.08:
        alloc["fund"] = 30 if signal != "BUY" else 20
    elif best_apy > 0.04:
        alloc["fund"] = 10

    # Basis
    ab = float(row["ann_basis"]) if pd.notna(row.get("ann_basis")) else 0
    if ab > 0.10:
        alloc["basis"] = 20
    elif ab > 0.05:
        alloc["basis"] = 10

    alloc["cash"] = max(0, 100 - sum(v for v in alloc.values()))

    # Expected APY blended
    exp_apy = (
        alloc["btc"] / 100 * 0.40 +
        alloc["fund"] / 100 * max(best_apy, 0) +
        alloc["basis"] / 100 * max(ab, 0) +
        alloc["cash"] / 100 * 0.045
    )
    exp_daily = CAPITAL * exp_apy / 365

    regime = "TREND_UP" if signal == "BUY" else "TREND_DOWN" if signal == "SELL" else (
        "CHOP_LOW_VOL" if pd.notna(row["vol_14d"]) and row["vol_14d"] < 0.4 else
        "CHOP_HIGH_VOL" if pd.notna(row["vol_14d"]) and row["vol_14d"] > 0.8 else
        "CHOP_MID_VOL"
    )
    return {
        "regime": regime, "donchian": signal,
        "best_funding_venue": best_v, "best_funding_apy_7d": best_apy,
        "ann_basis": ab,
        "vol_14d": float(row["vol_14d"]) if pd.notna(row["vol_14d"]) else 0,
        "alloc_btc": alloc["btc"], "alloc_funding": alloc["fund"],
        "alloc_basis": alloc["basis"], "alloc_cash": alloc["cash"],
        "expected_apy": exp_apy, "expected_daily": exp_daily,
    }


def main():
    df = load_all()

    # Existing dates
    existing = pd.read_csv(LEDGER)
    existing_dates = set(existing["date"].astype(str).tolist())

    # Missing dates: any date with valid d20_high & vol_14d in df, in range
    df = df.dropna(subset=["d20_high", "vol_14d"]).copy()
    # Only past 30 days for backfill (don't blow up with 5 years of history)
    cutoff = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)).floor("1D")
    df = df[df["date"] >= cutoff]

    new_rows = []
    for _, row in df.iterrows():
        d = row["date"].strftime("%Y-%m-%d")
        if d in existing_dates:
            continue
        dec = decide(row)
        new_rows.append({
            "date": d,
            "btc_close": float(row["close"]),
            "regime": dec["regime"],
            "donchian": dec["donchian"],
            "best_funding_venue": dec["best_funding_venue"],
            "best_funding_apy_7d": dec["best_funding_apy_7d"],
            "ann_basis": dec["ann_basis"],
            "vol_14d": dec["vol_14d"],
            "alloc_btc": dec["alloc_btc"],
            "alloc_funding": dec["alloc_funding"],
            "alloc_basis": dec["alloc_basis"],
            "alloc_cash": dec["alloc_cash"],
            "expected_apy": dec["expected_apy"],
            "expected_daily": dec["expected_daily"],
        })

    if not new_rows:
        print("No missing dates to backfill.")
        return

    print(f"Backfilling {len(new_rows)} rows:")
    for r in new_rows:
        print(f"  {r['date']}  BTC=${r['btc_close']:,.2f}  {r['donchian']:<4}  "
              f"fund={r['best_funding_apy_7d']*100:>5.2f}%  basis={r['ann_basis']*100:>+6.2f}%  "
              f"exp_daily=${r['expected_daily']:.2f}")

    # Append + sort + dedupe
    new_df = pd.DataFrame(new_rows)
    combined = pd.concat([existing, new_df], ignore_index=True)
    combined = combined.drop_duplicates("date", keep="first").sort_values("date").reset_index(drop=True)
    combined.to_csv(LEDGER, index=False)
    print(f"\nLedger now has {len(combined)} rows.")


if __name__ == "__main__":
    main()
