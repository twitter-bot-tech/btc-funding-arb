"""Fetch all historical Binance BTCUSDT quarterly futures contracts.

Quarterly contracts expire last Friday of Mar/Jun/Sep/Dec (08:00 UTC).
Symbol format: BTCUSDT_YYMMDD where YYMMDD is the expiry date.

We enumerate expected expiries 2020-09 -> 2026-12 and probe each.
For each contract, fetch 1d klines (which gives us OHLC), and the listing
date is typically ~6 months before expiry (CURRENT_QUARTER and NEXT_QUARTER
overlap, so a contract is listed when promoted from NEXT_QUARTER status,
roughly 1 quarter before delivery).
"""
import time
from pathlib import Path
import requests
import pandas as pd

DATA = Path(__file__).parent / "data"
FAPI = "https://fapi.binance.com"

# Last Fridays of Mar/Jun/Sep/Dec 2020-2026
EXPIRIES = [
    "200925", "201225",
    "210326", "210625", "210924", "211231",
    "220325", "220624", "220930", "221230",
    "230331", "230630", "230929", "231229",
    "240329", "240628", "240927", "241227",
    "250328", "250627", "250926", "251226",
    "260327", "260626", "260925", "261225",
]


def get(url, params, retries=6):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code in (429, 418):
                time.sleep(2 ** i)
                continue
            if r.status_code == 400:
                return None  # symbol doesn't exist
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(1 + i * 2)


def fetch_contract(expiry):
    symbol = f"BTCUSDT_{expiry}"
    out = []
    start = 0
    # Probe first
    first = get(f"{FAPI}/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": 1000})
    if first is None or not first:
        return None
    out.extend(first)
    while True:
        last_open = out[-1][0]
        nxt = get(f"{FAPI}/fapi/v1/klines",
                  {"symbol": symbol, "interval": "1d", "startTime": last_open + 1, "limit": 1000})
        if not nxt:
            break
        out.extend(nxt)
        if len(nxt) < 1000:
            break
        time.sleep(0.1)
    df = pd.DataFrame(out, columns=[
        "openTime", "open", "high", "low", "close", "volume",
        "closeTime", "qav", "trades", "tbav", "tqav", "ignore",
    ])
    df["openTime"] = pd.to_datetime(df["openTime"], unit="ms", utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["symbol"] = symbol
    df["expiry"] = pd.to_datetime("20" + expiry, format="%Y%m%d", utc=True)
    return df[["openTime", "symbol", "expiry", "open", "high", "low", "close", "volume"]]


def main():
    all_df = []
    for exp in EXPIRIES:
        print(f"  fetching BTCUSDT_{exp}...", end=" ")
        df = fetch_contract(exp)
        if df is None or len(df) == 0:
            print("(no data)")
            continue
        print(f"{len(df)} days, {df['openTime'].min().date()} -> {df['openTime'].max().date()}")
        all_df.append(df)
        time.sleep(0.15)
    if not all_df:
        print("No contracts retrieved")
        return
    full = pd.concat(all_df, ignore_index=True)
    full.to_csv(DATA / "quarterly_btc.csv", index=False)
    print(f"\nSaved {len(full)} rows across {len(all_df)} contracts to data/quarterly_btc.csv")


if __name__ == "__main__":
    main()
