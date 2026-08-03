"""Fetch OKX + Bybit BTC perp funding history. Both use 4h funding cadence now
(OKX since 2023-09; Bybit since 2024-01). Pre-cutoff was 8h. We resample to 8h
buckets for apples-to-apples vs Binance.
"""
import time
from pathlib import Path
import requests
import pandas as pd

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)


def get(url, params, retries=8):
    last_exc = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code in (429, 418):
                time.sleep(2 ** i)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_exc = e
            time.sleep(1 + i * 2)
        except Exception as e:
            last_exc = e
            if i == retries - 1:
                raise
            time.sleep(1 + i)
    raise last_exc


def fetch_okx():
    """OKX paginates BACKWARDS via 'after' (timestamp upper bound). 100 rows/page."""
    out = []
    before_ts = None
    while True:
        params = {"instId": "BTC-USDT-SWAP", "limit": 100}
        if before_ts is not None:
            params["after"] = before_ts
        j = get("https://www.okx.com/api/v5/public/funding-rate-history", params)
        rows = j.get("data", [])
        if not rows:
            break
        out.extend(rows)
        oldest = int(rows[-1]["fundingTime"])
        if before_ts is not None and oldest >= before_ts:
            break
        before_ts = oldest
        if len(out) % 1000 == 0 or len(rows) < 100:
            print(f"  okx: {len(out)} rows, oldest={pd.Timestamp(oldest, unit='ms')}")
        if len(rows) < 100:
            break
        time.sleep(0.12)
    df = pd.DataFrame(out)
    df["ts"] = pd.to_datetime(df["fundingTime"].astype("int64"), unit="ms", utc=True)
    df["fundingRate"] = pd.to_numeric(df["realizedRate"], errors="coerce")
    df = df[["ts", "fundingRate"]].drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df


def fetch_bybit():
    """Bybit paginates BACKWARDS via 'endTime'. 200 rows/page."""
    out = []
    end_time = int(time.time() * 1000)
    while True:
        params = {
            "category": "linear",
            "symbol": "BTCUSDT",
            "limit": 200,
            "endTime": end_time,
        }
        j = get("https://api.bybit.com/v5/market/funding/history", params)
        rows = j.get("result", {}).get("list", [])
        if not rows:
            break
        out.extend(rows)
        oldest = int(rows[-1]["fundingRateTimestamp"])
        if oldest >= end_time:
            break
        end_time = oldest - 1
        if len(out) % 2000 == 0 or len(rows) < 200:
            print(f"  bybit: {len(out)} rows, oldest={pd.Timestamp(oldest, unit='ms')}")
        if len(rows) < 200:
            break
        time.sleep(0.12)
    df = pd.DataFrame(out)
    df["ts"] = pd.to_datetime(df["fundingRateTimestamp"].astype("int64"), unit="ms", utc=True)
    df["fundingRate"] = pd.to_numeric(df["fundingRate"], errors="coerce")
    df = df[["ts", "fundingRate"]].drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df


def main():
    okx_file = DATA / "funding_okx_btc.csv"
    if not okx_file.exists() or pd.read_csv(okx_file).empty:
        print("[OKX]")
        okx = fetch_okx()
        okx.to_csv(okx_file, index=False)
        print(f"  saved {len(okx)} rows, {okx['ts'].min()} -> {okx['ts'].max()}")
    else:
        print(f"[OKX] reusing cached file ({len(pd.read_csv(okx_file))} rows)")

    print("[Bybit]")
    bb = fetch_bybit()
    print(f"  saved {len(bb)} rows, {bb['ts'].min()} -> {bb['ts'].max()}")
    bb.to_csv(DATA / "funding_bybit_btc.csv", index=False)


if __name__ == "__main__":
    main()
