"""Fetch macro + sentiment data for correlation/regime analysis.

Sources (all free):
  - alternative.me/fng (Fear & Greed Index, 730d)
  - Yahoo Finance v8 chart API (SPX/DXY/Gold/VIX, no auth)
  - Binance (ETH 1d klines)
  - CoinGecko /api/v3/global (BTC dominance)
"""
import json
import time
from pathlib import Path
import requests
import pandas as pd

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)


def get(url, params=None, headers=None, retries=5):
    h = headers or {"User-Agent": "Mozilla/5.0"}
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=h, timeout=20)
            r.raise_for_status()
            return r.json() if r.headers.get("content-type", "").startswith("application/json") or url.endswith("json") else r.text
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(1 + i)


def fetch_fng():
    j = get("https://api.alternative.me/fng/", {"limit": 730, "format": "json"})
    rows = j["data"]
    df = pd.DataFrame([{
        "date": pd.to_datetime(int(r["timestamp"]), unit="s", utc=True).date(),
        "fng": int(r["value"]),
        "classification": r["value_classification"],
    } for r in rows])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def fetch_yahoo(symbol, label):
    """v8 chart API - returns daily close for 2y."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    j = get(url, {"range": "2y", "interval": "1d"})
    res = j["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    df = pd.DataFrame({
        "date": [pd.to_datetime(t, unit="s", utc=True).date() for t in ts],
        label: closes,
    })
    df = df.dropna().reset_index(drop=True)
    return df


def fetch_eth():
    j = get("https://api.binance.com/api/v3/klines",
            {"symbol": "ETHUSDT", "interval": "1d", "limit": 730})
    rows = j if isinstance(j, list) else []
    df = pd.DataFrame([{
        "date": pd.to_datetime(r[0], unit="ms", utc=True).date(),
        "eth_close": float(r[4]),
    } for r in rows])
    return df


def fetch_btc_dominance():
    """CoinGecko global - current snapshot only."""
    try:
        j = get("https://api.coingecko.com/api/v3/global")
        dom = j["data"]["market_cap_percentage"]
        return {
            "btc_dominance": dom.get("btc", 0),
            "eth_dominance": dom.get("eth", 0),
            "stablecoin_share": dom.get("usdt", 0) + dom.get("usdc", 0),
            "total_mcap_usd": j["data"]["total_market_cap"]["usd"],
            "total_volume_usd": j["data"]["total_volume"]["usd"],
            "active_cryptos": j["data"]["active_cryptocurrencies"],
        }
    except Exception as e:
        print(f"CoinGecko dominance skipped: {e}")
        return None


def fetch_etf_flows():
    """Farside ETF flow CSV scrape — fallback to None if blocked."""
    try:
        txt = get("https://farside.co.uk/wp-content/uploads/2024/03/btc-etf-flows.csv")
        # parsing varies; just return None for now (graceful degrade)
        return None
    except Exception:
        return None


def main():
    print("[1/5] Fear & Greed history (730d)...")
    fng = fetch_fng()
    fng.to_csv(DATA / "fng.csv", index=False)
    print(f"  saved {len(fng)} rows, latest={fng.iloc[-1]['fng']} ({fng.iloc[-1]['classification']})")

    print("[2/5] ETH/USDT history (Binance)...")
    eth = fetch_eth()
    eth.to_csv(DATA / "macro_eth.csv", index=False)
    print(f"  saved {len(eth)} rows")

    macro_frames = [eth]
    print("[3/5] SPX / DXY / Gold / VIX (Yahoo)...")
    for sym, label in [("^GSPC", "spx_close"), ("DX-Y.NYB", "dxy_close"),
                        ("GC=F", "gold_close"), ("^VIX", "vix_close")]:
        try:
            df = fetch_yahoo(sym, label)
            df.to_csv(DATA / f"macro_{label.split('_')[0]}.csv", index=False)
            macro_frames.append(df)
            print(f"  {label}: {len(df)} rows")
            time.sleep(0.3)
        except Exception as e:
            print(f"  {label}: SKIP ({e})")

    print("[4/5] BTC dominance snapshot...")
    dom = fetch_btc_dominance()
    if dom:
        (DATA / "btc_dominance.json").write_text(json.dumps(dom, indent=2))
        print(f"  BTC.D = {dom['btc_dominance']:.2f}%")
    else:
        print("  skipped")

    print("[5/5] Merging macro into single frame...")
    merged = macro_frames[0]
    for df in macro_frames[1:]:
        merged = merged.merge(df, on="date", how="outer")
    merged = merged.sort_values("date").reset_index(drop=True)
    merged.to_csv(DATA / "macro_all.csv", index=False)
    print(f"  merged: {len(merged)} rows, cols={list(merged.columns)}")


if __name__ == "__main__":
    main()
