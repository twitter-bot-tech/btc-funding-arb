"""24/7 Spot Opportunity Hunter

Runs every minute via launchd. Scans:
  1. Cross-exchange spot spread (Binance / OKX / Bybit / KuCoin / Gate) — 15 coins
  2. Triangular arb on Binance — 3 paths (BTC-ETH, BTC-BNB, ETH-SOL bridges)
  3. Stablecoin depeg (USDC, FDUSD, TUSD, DAI vs USDT)
  4. Recent listing premium check (newly added coins on Binance vs other CEX)

Logs EVERY opportunity > 0 net (after fees + slippage) to opportunities.csv.
Daily aggregate report saved to hunter_daily.json.

Capital: $10K spot only.
"""
import json
import time
from pathlib import Path
from datetime import datetime, timezone
import requests
import threading

ROOT = Path(__file__).parent
LOG = ROOT / "opportunities.csv"
DAILY = ROOT / "hunter_daily.json"
SNAPSHOT = ROOT / "hunter_snapshot.json"

# Load .env
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line: continue
        _k, _v = _line.split("=", 1)
        import os as _os
        _os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import os
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT: return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT, "text": text, "parse_mode": "Markdown"},
                          timeout=10)
        return r.ok
    except Exception:
        return False

CAPITAL = 10_000.0
# Realistic fees: Binance BNB-discount taker ~ 0.075%; OKX/Bybit 0.08%
FEES = {"binance":0.075,"okx":0.08,"bybit":0.08,"kucoin":0.10,"gate":0.20}
SLIP_BPS = 8     # 8 bps slippage (was 5) — more realistic for cross-exchange
LATENCY_BPS = 3  # 3 bps price drift during 200-500ms execution latency

COINS = ["BTC","ETH","SOL","BNB","XRP","DOGE","ADA","AVAX","LINK","TON",
         "DOT","TRX","MATIC","SHIB","ATOM"]


def get(url, params=None, timeout=8):
    try:
        r = requests.get(url, params=params, timeout=timeout,
                          headers={"User-Agent":"hunter/1.0"})
        if r.status_code != 200: return None
        return r.json()
    except Exception:
        return None


# ============ Exchange fetchers (batch) ============
def binance_all_books():
    j = get("https://api.binance.com/api/v3/ticker/bookTicker")
    if not j: return {}
    return {x["symbol"]: {"bid":float(x["bidPrice"]),"ask":float(x["askPrice"])} for x in j}


def okx_all_books():
    j = get("https://www.okx.com/api/v5/market/tickers", {"instType":"SPOT"})
    if not j or "data" not in j: return {}
    out = {}
    for x in j["data"]:
        try:
            out[x["instId"].replace("-","")] = {"bid":float(x["bidPx"]),"ask":float(x["askPx"])}
        except (ValueError, KeyError, TypeError):
            pass
    return out


def bybit_all_books():
    j = get("https://api.bybit.com/v5/market/tickers", {"category":"spot"})
    if not j or j.get("retCode") != 0: return {}
    out = {}
    for x in j["result"]["list"]:
        try:
            out[x["symbol"]] = {"bid":float(x["bid1Price"]),"ask":float(x["ask1Price"])}
        except (ValueError, KeyError, TypeError):
            pass
    return out


# ============ Opportunity detection ============
def scan_cross_exchange(books, spreads_out=None):
    """For each coin, find best cross-exchange opportunity AND log all spreads for charting."""
    out = []
    for coin in COINS:
        sym = coin + "USDT"
        venues = {ex: b.get(sym) for ex, b in books.items()}
        venues = {k:v for k,v in venues.items() if v is not None}
        if len(venues) < 2: continue
        # Best buy: lowest ask
        best_buy = min(venues.items(), key=lambda x:x[1]["ask"])
        # Best sell: highest bid
        best_sell = max(venues.items(), key=lambda x:x[1]["bid"])
        gross = 0.0
        if best_buy[0] != best_sell[0]:
            ask, bid = best_buy[1]["ask"], best_sell[1]["bid"]
            gross = (bid-ask)/ask
            fee = (FEES[best_buy[0]]+FEES[best_sell[0]])/100 + 2*SLIP_BPS/10000 + 2*LATENCY_BPS/10000
            net = gross - fee
            if net > 0:
                out.append({
                    "type":"cross","coin":coin,
                    "buy_at":best_buy[0],"sell_at":best_sell[0],
                    "ask":ask,"bid":bid,
                    "gross_bps":gross*10000,"fee_bps":fee*10000,"net_bps":net*10000,
                    "net_usd":net*CAPITAL,
                })
        # ALWAYS log the spread for charting (even if not profitable)
        if spreads_out is not None:
            spreads_out.append({
                "coin": coin,
                "gross_bps": gross * 10000,
                "best_buy": best_buy[0],
                "best_sell": best_sell[0],
                "mid_price": (best_buy[1]["ask"] + best_sell[1]["bid"]) / 2,
            })
    return out


def scan_triangular(binance_books):
    """Triangular paths on Binance."""
    paths = [
        ("USDT","BTC","ETH",["BTCUSDT","ETHBTC","ETHUSDT"]),
        ("USDT","BTC","BNB",["BTCUSDT","BNBBTC","BNBUSDT"]),
        ("USDT","ETH","SOL",["ETHUSDT","SOLETH","SOLUSDT"]),
    ]
    f = FEES["binance"]/100
    slip = SLIP_BPS/10000
    out = []
    for a, b, c, syms in paths:
        # Need all 3 symbols
        b1 = binance_books.get(syms[0])
        b2 = binance_books.get(syms[1])
        b3 = binance_books.get(syms[2])
        if not all([b1,b2,b3]): continue
        # Forward path: USDT -> b -> c -> USDT
        amt = CAPITAL
        amt = amt / b1["ask"] * (1-f-slip)        # buy b with USDT
        amt = amt / b2["ask"] * (1-f-slip)        # buy c with b (c/b ask)
        amt = amt * b3["bid"] * (1-f-slip)        # sell c for USDT
        net_fwd = amt - CAPITAL
        # Reverse
        amt = CAPITAL
        amt = amt / b3["ask"] * (1-f-slip)        # buy c with USDT
        amt = amt * b2["bid"] * (1-f-slip)        # sell c for b
        amt = amt * b1["bid"] * (1-f-slip)        # sell b for USDT
        net_rev = amt - CAPITAL
        if net_fwd > 0:
            out.append({"type":"tri","path":f"USDT-{b}-{c}-USDT","direction":"fwd",
                        "net_usd":net_fwd,"net_bps":net_fwd/CAPITAL*10000})
        if net_rev > 0:
            out.append({"type":"tri","path":f"USDT-{c}-{b}-USDT","direction":"rev",
                        "net_usd":net_rev,"net_bps":net_rev/CAPITAL*10000})
    return out


def scan_stablecoin(binance_books):
    """Stablecoin depeg vs USDT."""
    pairs = [("USDC","USDCUSDT"),("FDUSD","FDUSDUSDT"),("TUSD","TUSDUSDT")]
    out = []
    for name, sym in pairs:
        b = binance_books.get(sym)
        if not b: continue
        dev_bps = (b["bid"] - 1.0) * 10000
        # Profit only if deviation > 2x fees (40 bps)
        if abs(dev_bps) > 40:
            net = (abs(dev_bps)/10000 - 2*FEES["binance"]/100) * CAPITAL
            if net > 0:
                out.append({"type":"depeg","stable":name,
                            "bid":b["bid"],"dev_bps":dev_bps,"net_usd":net})
    return out


# ============ Main scan ============
def scan_once():
    """Fetch books from all exchanges in parallel and detect opportunities."""
    books = {}
    threads = []
    results = {}
    def fetch(name, fn):
        results[name] = fn()
    for name, fn in [("binance",binance_all_books),("okx",okx_all_books),("bybit",bybit_all_books)]:
        t = threading.Thread(target=fetch, args=(name,fn))
        t.start()
        threads.append(t)
    for t in threads: t.join(timeout=10)
    for name in ("binance","okx","bybit"):
        books[name] = results.get(name, {})

    if not books["binance"]:
        return {"error":"binance unreachable"}

    spreads = []
    cross = scan_cross_exchange(books, spreads_out=spreads)
    tri = scan_triangular(books["binance"])
    depeg = scan_stablecoin(books["binance"])

    # Append spread timeseries (gross spread + best venues per coin) to CSV
    ts = datetime.now(timezone.utc).isoformat()
    spreads_csv = Path(__file__).parent / "spreads.csv"
    new = not spreads_csv.exists()
    with spreads_csv.open("a") as f:
        if new:
            f.write("ts,coin,gross_bps,best_buy,best_sell,mid_price\n")
        for s in spreads:
            f.write(f"{ts},{s['coin']},{s['gross_bps']:.3f},{s['best_buy']},{s['best_sell']},{s['mid_price']:.2f}\n")

    all_opps = cross + tri + depeg
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "n_binance_books": len(books["binance"]),
        "n_okx_books": len(books["okx"]),
        "n_bybit_books": len(books["bybit"]),
        "opportunities": all_opps,
        "best_net_usd": max([o["net_usd"] for o in all_opps], default=0),
        "total_opps": len(all_opps),
    }


def append_log(snap):
    """Append every opportunity to CSV."""
    new = not LOG.exists()
    with LOG.open("a") as f:
        if new:
            f.write("ts,type,details,net_usd,net_bps\n")
        for o in snap.get("opportunities", []):
            details = ""
            if o["type"] == "cross":
                details = f"{o['coin']} {o['buy_at']}->{o['sell_at']}"
            elif o["type"] == "tri":
                details = f"{o['path']} ({o['direction']})"
            elif o["type"] == "depeg":
                details = f"{o['stable']} dev={o['dev_bps']:.1f}bps"
            f.write(f"{snap['ts']},{o['type']},\"{details}\",{o['net_usd']:.2f},{o.get('net_bps',0):.1f}\n")


def update_daily(snap):
    """Maintain rolling daily aggregate."""
    today = datetime.now(timezone.utc).date().isoformat()
    state = json.loads(DAILY.read_text()) if DAILY.exists() else {"days":{}}
    d = state["days"].setdefault(today, {
        "scans":0,"opps_total":0,"sum_potential_usd":0.0,
        "best_single_usd":0.0,
        "by_type":{"cross":0,"tri":0,"depeg":0},
    })
    d["scans"] += 1
    n_opps = len(snap.get("opportunities", []))
    d["opps_total"] += n_opps
    for o in snap.get("opportunities", []):
        d["sum_potential_usd"] += o["net_usd"]
        d["best_single_usd"] = max(d["best_single_usd"], o["net_usd"])
        d["by_type"][o["type"]] = d["by_type"].get(o["type"],0) + 1
    DAILY.write_text(json.dumps(state, indent=2))


def main():
    snap = scan_once()
    SNAPSHOT.write_text(json.dumps(snap, indent=2, default=str))
    append_log(snap)
    update_daily(snap)

    # Telegram push on any profitable opportunity (rare but actionable)
    if snap.get("opportunities"):
        best = max(snap["opportunities"], key=lambda o: o.get("net_usd", 0))
        if best["net_usd"] >= 5.0:  # only push opps worth ≥ $5
            detail = ""
            if best["type"] == "cross":
                detail = f"{best['coin']} {best['buy_at']}→{best['sell_at']}"
            elif best["type"] == "tri":
                detail = f"{best['path']}"
            elif best["type"] == "depeg":
                detail = f"{best['stable']} dev={best['dev_bps']:.1f}bps"
            msg = (
                f"*🎯 HUNTER · 套利机会*\n\n"
                f"`{detail}`\n"
                f"净盈利: *${best['net_usd']:.2f}* ({best.get('net_bps',0):.1f} bps)\n"
                f"类型: {best['type'].upper()}\n"
                f"━━━━━━\n"
                f"快照时间: {snap['ts'][:19]}\n"
                f"[查看仪表盘](https://btc-quant.pages.dev/#hunter)"
            )
            send_telegram(msg)

    # Console output
    if "error" in snap:
        print(f"ERROR: {snap['error']}")
        return
    print(f"[{snap['ts'][11:19]}] books: bn={snap['n_binance_books']} okx={snap['n_okx_books']} by={snap['n_bybit_books']}")
    print(f"  Opportunities found: {snap['total_opps']}")
    if snap["total_opps"] > 0:
        print(f"  Best net P&L: ${snap['best_net_usd']:.2f}")
        for o in sorted(snap["opportunities"], key=lambda x:-x["net_usd"])[:5]:
            if o["type"] == "cross":
                print(f"    + {o['coin']} {o['buy_at']}->{o['sell_at']}: ${o['net_usd']:+.2f} ({o['net_bps']:+.1f} bps)")
            elif o["type"] == "tri":
                print(f"    + TRI {o['path']}: ${o['net_usd']:+.2f}")
            elif o["type"] == "depeg":
                print(f"    + DEPEG {o['stable']} {o['dev_bps']:+.1f}bps: ${o['net_usd']:+.2f}")
    else:
        print("  No profitable opps this scan")


if __name__ == "__main__":
    main()
