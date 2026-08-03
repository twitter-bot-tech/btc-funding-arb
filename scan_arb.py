"""Real-time spot arbitrage scanner.

Scans 4 types of CURRENT opportunities (no futures):
  1. Cross-exchange spot spread: Binance vs OKX vs Bybit for BTC, ETH, SOL, etc.
  2. Triangular arb on Binance: BTC/USDT -> ETH/BTC -> ETH/USDT -> USDT
  3. Stablecoin depeg: USDT/USDC, FDUSD/USDT, DAI/USDT
  4. New listing premium: smaller exchange (KuCoin/Gate) vs Binance

For each opportunity: compute net profit after fees + slippage.
Output: arb_opportunities.json + console summary.
"""
import json
import time
from pathlib import Path
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

CAPITAL = 10_000.0
DAILY_TARGET = 100.0

# Fees per leg (taker, no VIP)
FEES = {
    "binance": 0.10,  # 0.10% spot taker (BNB discount can reduce to 0.075%)
    "okx": 0.10,
    "bybit": 0.10,
    "kucoin": 0.10,
    "gate": 0.20,
}
SLIPPAGE_BPS = 5  # 5bps slippage assumption per leg


def get(url, params=None, timeout=10):
    try:
        r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "arb-scan/1.0"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_error": str(e)}


# ============ Cross-exchange spot prices ============
def binance_book(symbol):
    j = get("https://api.binance.com/api/v3/ticker/bookTicker", {"symbol": symbol})
    if "_error" in j or "bidPrice" not in j: return None
    return {"bid": float(j["bidPrice"]), "ask": float(j["askPrice"])}


def okx_book(inst):
    j = get("https://www.okx.com/api/v5/market/ticker", {"instId": inst})
    if "_error" in j or not j.get("data"): return None
    d = j["data"][0]
    return {"bid": float(d["bidPx"]), "ask": float(d["askPx"])}


def bybit_book(symbol):
    j = get("https://api.bybit.com/v5/market/tickers", {"category": "spot", "symbol": symbol})
    if "_error" in j or j.get("retCode") != 0: return None
    d = j["result"]["list"][0]
    return {"bid": float(d["bid1Price"]), "ask": float(d["ask1Price"])}


def scan_cross_exchange(coins=("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE")):
    """For each coin, fetch bid/ask on 3 exchanges, find best cross-exchange opportunity."""
    results = []
    for coin in coins:
        b = binance_book(coin + "USDT")
        o = okx_book(coin + "-USDT")
        y = bybit_book(coin + "USDT")
        venues = {"binance": b, "okx": o, "bybit": y}
        venues = {k: v for k, v in venues.items() if v is not None}
        if len(venues) < 2:
            continue
        # Find: buy at lowest ask, sell at highest bid
        best_buy = min(venues.items(), key=lambda x: x[1]["ask"])
        best_sell = max(venues.items(), key=lambda x: x[1]["bid"])
        if best_buy[0] == best_sell[0]:
            continue
        ask = best_buy[1]["ask"]
        bid = best_sell[1]["bid"]
        gross_pct = (bid - ask) / ask
        # Fees: buy fee + sell fee + 2x slippage
        fee_pct = (FEES[best_buy[0]] + FEES[best_sell[0]]) / 100 + 2 * SLIPPAGE_BPS / 10000
        net_pct = gross_pct - fee_pct
        net_usd_per_round = net_pct * CAPITAL
        results.append({
            "coin": coin,
            "buy_at": best_buy[0],
            "sell_at": best_sell[0],
            "ask": ask,
            "bid": bid,
            "gross_pct": gross_pct * 100,
            "fee_pct": fee_pct * 100,
            "net_pct": net_pct * 100,
            "net_usd_per_round": net_usd_per_round,
            "all_venues": {k: v for k, v in venues.items()},
        })
    return sorted(results, key=lambda x: x["net_pct"], reverse=True)


# ============ Triangular arb on Binance ============
def scan_triangular_binance():
    """BTC/USDT -> ETH/BTC -> ETH/USDT cycle."""
    btc = binance_book("BTCUSDT")
    eth_btc = binance_book("ETHBTC")
    eth = binance_book("ETHUSDT")
    if not all([btc, eth_btc, eth]):
        return None
    # Path 1: USDT -> BTC -> ETH -> USDT
    # Buy BTC with USDT (use ask), buy ETH with BTC (use ask of ETHBTC), sell ETH for USDT (use bid)
    f = FEES["binance"] / 100
    slip = SLIPPAGE_BPS / 10000
    # Start with $10000 USDT
    btc_bought = CAPITAL / btc["ask"] * (1 - f - slip)
    eth_bought = btc_bought / eth_btc["ask"] * (1 - f - slip)
    usdt_out = eth_bought * eth["bid"] * (1 - f - slip)
    net1 = usdt_out - CAPITAL
    # Path 2: USDT -> ETH -> BTC -> USDT (reverse)
    eth_bought2 = CAPITAL / eth["ask"] * (1 - f - slip)
    btc_bought2 = eth_bought2 * eth_btc["bid"] * (1 - f - slip)
    usdt_out2 = btc_bought2 * btc["bid"] * (1 - f - slip)
    net2 = usdt_out2 - CAPITAL
    return {
        "path_usdt_btc_eth_usdt": {"net_usd": net1, "net_pct": net1 / CAPITAL * 100},
        "path_usdt_eth_btc_usdt": {"net_usd": net2, "net_pct": net2 / CAPITAL * 100},
        "btc_usdt": btc, "eth_usdt": eth, "eth_btc": eth_btc,
    }


# ============ Stablecoin depeg ============
def scan_stablecoin():
    """Check USDC/USDT, FDUSD/USDT, DAI/USDT deviations from 1.0."""
    pairs = [("USDC", "USDCUSDT"), ("FDUSD", "FDUSDUSDT"), ("DAI", "DAIUSDT"), ("TUSD", "TUSDUSDT")]
    out = []
    for name, sym in pairs:
        b = binance_book(sym)
        if not b: continue
        deviation_pct = (b["bid"] - 1.0) * 100  # selling 1.0 to USDT
        # Profitable: buy at <1.0, redeem at peg (assume off-platform redemption peg holds)
        # Or: buy at <1.0, sell when it returns to 1.0 (mean reversion)
        if abs(deviation_pct) > 0.10:  # > 10bps deviation
            net_per_round = abs(deviation_pct) / 100 * CAPITAL - 2 * FEES["binance"] / 100 * CAPITAL
            out.append({
                "stable": name,
                "bid": b["bid"], "ask": b["ask"],
                "deviation_bps": deviation_pct * 100,
                "potential_profit_usd": net_per_round,
            })
    return out


# ============ Volatility (proxy for grid trading opportunity) ============
def scan_volatility():
    """Check 24h volatility for top pairs - grid trading works on high-vol low-trend markets."""
    j = get("https://api.binance.com/api/v3/ticker/24hr")
    if "_error" in j: return []
    pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "PEPEUSDT", "WIFUSDT"]
    out = []
    for p in pairs:
        rec = next((x for x in j if x["symbol"] == p), None)
        if not rec: continue
        high = float(rec["highPrice"])
        low = float(rec["lowPrice"])
        last = float(rec["lastPrice"])
        chg_pct = float(rec["priceChangePercent"])
        if low > 0:
            range_pct = (high - low) / low * 100
            # Grid trading profit estimate: range / 2 * num_trades (assume 4 trades per day in range)
            # Net per round trip = range * 0.5 - 2*fees
            grid_potential = (range_pct / 2 - 0.2) * 0.04  # conservative: capture 4% of theoretical
            out.append({
                "symbol": p,
                "last": last,
                "range_24h_pct": range_pct,
                "chg_24h_pct": chg_pct,
                "trend_strength": abs(chg_pct) / range_pct if range_pct > 0 else 0,
                "grid_potential_pct": grid_potential,
                "grid_potential_usd": grid_potential / 100 * CAPITAL,
            })
    return sorted(out, key=lambda x: x["grid_potential_usd"], reverse=True)


# ============ Main ============
def main():
    print("Scanning real-time arbitrage market...")
    t0 = time.time()
    cross = scan_cross_exchange()
    tri = scan_triangular_binance()
    stable = scan_stablecoin()
    vol = scan_volatility()
    elapsed = time.time() - t0
    print(f"Scanned in {elapsed:.1f}s")

    print("\n" + "="*100)
    print("1) CROSS-EXCHANGE SPOT ARB (Binance / OKX / Bybit)")
    print("="*100)
    print(f"  {'Coin':<6} {'Buy@':<8} {'Sell@':<8} {'Gross%':<8} {'Fee%':<8} {'Net%':<10} {'Net $':<12}")
    for r in cross[:10]:
        cls = "✅" if r["net_pct"] > 0 else "❌"
        print(f"  {r['coin']:<6} {r['buy_at']:<8} {r['sell_at']:<8} "
              f"{r['gross_pct']:+.4f}% {r['fee_pct']:.3f}% {r['net_pct']:+.4f}% "
              f"${r['net_usd_per_round']:+.2f}  {cls}")

    print("\n" + "="*100)
    print("2) TRIANGULAR ARB on BINANCE (USDT->BTC->ETH->USDT)")
    print("="*100)
    if tri:
        for path_name, p in tri.items():
            if path_name.startswith("path"):
                cls = "✅" if p["net_pct"] > 0 else "❌"
                direction = "USDT→BTC→ETH→USDT" if "btc_eth" in path_name else "USDT→ETH→BTC→USDT"
                print(f"  {direction}: net=${p['net_usd']:+.2f} ({p['net_pct']:+.4f}%) {cls}")

    print("\n" + "="*100)
    print("3) STABLECOIN DEPEG (|deviation| > 10 bps)")
    print("="*100)
    if stable:
        for s in stable:
            print(f"  {s['stable']:<5} bid=${s['bid']:.5f} dev={s['deviation_bps']:+.1f} bps -> potential ${s['potential_profit_usd']:+.2f}")
    else:
        print("  No depeg events > 10 bps. Stables are all near $1.00.")

    print("\n" + "="*100)
    print("4) GRID TRADING POTENTIAL (24h range / volatility)")
    print("="*100)
    print(f"  {'Symbol':<12} {'24h Range%':<12} {'24h Chg%':<10} {'Trend':<8} {'Grid Est%':<10} {'Daily $ est':<12}")
    for v in vol:
        trend_lbl = "STRONG" if v["trend_strength"] > 0.7 else "RANGE" if v["trend_strength"] < 0.3 else "MIX"
        print(f"  {v['symbol']:<12} {v['range_24h_pct']:<12.2f} {v['chg_24h_pct']:+<10.2f} {trend_lbl:<8} "
              f"{v['grid_potential_pct']:<10.3f} ${v['grid_potential_usd']:<12.2f}")

    # === Verdict ===
    print("\n" + "="*100)
    print("VERDICT — $100/day on $10K spot arbitrage feasibility")
    print("="*100)
    profitable_cross = [r for r in cross if r["net_pct"] > 0]
    profitable_tri = []
    if tri:
        profitable_tri = [p for k, p in tri.items() if k.startswith("path") and p["net_pct"] > 0]
    grid_top = vol[0]["grid_potential_usd"] if vol else 0

    print(f"  Profitable cross-exchange opps (single snapshot): {len(profitable_cross)}")
    print(f"  Profitable triangular paths:                       {len(profitable_tri)}")
    print(f"  Stablecoin depeg opps:                              {len(stable)}")
    print(f"  Best grid candidate daily estimate:                ${grid_top:.2f}")

    # Save snapshot
    snapshot = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "cross_exchange": cross,
        "triangular": tri,
        "stablecoin": stable,
        "volatility": vol,
    }
    Path("arb_snapshot.json").write_text(json.dumps(snapshot, indent=2, default=str))
    print("\nSaved arb_snapshot.json")


if __name__ == "__main__":
    main()
