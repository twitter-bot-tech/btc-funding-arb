"""Fetch Binance USDT-M BTC funding rate history + perp/spot klines.

Outputs:
  data/funding_btcusdt.csv   columns: fundingTime, fundingRate, markPrice
  data/perp_8h_btcusdt.csv   columns: openTime, open, high, low, close, volume
  data/spot_8h_btcusdt.csv   columns: openTime, open, high, low, close, volume
"""
import time
import sys
from pathlib import Path
import requests
import pandas as pd

FAPI = "https://fapi.binance.com"
SAPI = "https://api.binance.com"
SYMBOL = "BTCUSDT"
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# BTCUSDT perp launched 2019-09-08
START_MS = int(pd.Timestamp("2019-09-08", tz="UTC").timestamp() * 1000)
NOW_MS = int(time.time() * 1000)


def get(url, params, retries=5):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429 or r.status_code == 418:
                time.sleep(2 ** i)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(1 + i)


def fetch_funding():
    out = []
    start = START_MS
    while start < NOW_MS:
        rows = get(
            f"{FAPI}/fapi/v1/fundingRate",
            {"symbol": SYMBOL, "startTime": start, "limit": 1000},
        )
        if not rows:
            break
        out.extend(rows)
        last = rows[-1]["fundingTime"]
        if last <= start:
            break
        start = last + 1
        print(f"  funding: {len(out):>6d} rows, last={pd.Timestamp(last, unit='ms')}")
        time.sleep(0.15)
    df = pd.DataFrame(out)
    df["fundingTime"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    df["fundingRate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    df["markPrice"] = pd.to_numeric(df["markPrice"], errors="coerce")
    df = df.drop_duplicates("fundingTime").sort_values("fundingTime").reset_index(drop=True)
    return df


def fetch_klines(base_url, endpoint, interval="8h"):
    out = []
    start = START_MS
    while start < NOW_MS:
        rows = get(
            f"{base_url}{endpoint}",
            {"symbol": SYMBOL, "interval": interval, "startTime": start, "limit": 1000},
        )
        if not rows:
            break
        out.extend(rows)
        last = rows[-1][0]
        if last <= start:
            break
        start = last + 1
        print(f"  klines {endpoint}: {len(out):>6d} rows, last={pd.Timestamp(last, unit='ms')}")
        time.sleep(0.15)
    df = pd.DataFrame(out, columns=[
        "openTime", "open", "high", "low", "close", "volume",
        "closeTime", "qav", "trades", "tbav", "tqav", "ignore",
    ])
    df["openTime"] = pd.to_datetime(df["openTime"], unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df = df.drop_duplicates("openTime").sort_values("openTime").reset_index(drop=True)
    return df[["openTime", "open", "high", "low", "close", "volume"]]


def main():
    print("[1/3] funding rate history...")
    fr = fetch_funding()
    fr.to_csv(DATA_DIR / "funding_btcusdt.csv", index=False)
    print(f"  saved {len(fr)} funding rows -> data/funding_btcusdt.csv")

    print("[2/3] perp 8h klines...")
    perp = fetch_klines(FAPI, "/fapi/v1/klines", "8h")
    perp.to_csv(DATA_DIR / "perp_8h_btcusdt.csv", index=False)
    print(f"  saved {len(perp)} perp rows")

    print("[3/3] spot 8h klines...")
    spot = fetch_klines(SAPI, "/api/v3/klines", "8h")
    spot.to_csv(DATA_DIR / "spot_8h_btcusdt.csv", index=False)
    print(f"  saved {len(spot)} spot rows")

    print("\nDone.")
    print(f"  funding range: {fr['fundingTime'].min()} -> {fr['fundingTime'].max()}")
    print(f"  perp    range: {perp['openTime'].min()} -> {perp['openTime'].max()}")
    print(f"  spot    range: {spot['openTime'].min()} -> {spot['openTime'].max()}")


if __name__ == "__main__":
    main()
