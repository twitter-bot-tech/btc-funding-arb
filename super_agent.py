"""BTC Super Agent — Unified daily opportunity scanner.

Aggregates real-time signals from all viable strategies into ONE decision sheet:
  1. Donchian(20) trend regime
  2. Multi-exchange funding rate carry (Binance + Bybit + OKX)
  3. CME-equivalent basis (Binance quarterly futures)
  4. Realized vol regime
  5. Capital allocation recommendation

Output: scorecard.md + scorecard.json + telegram (optional)
Run daily via launchd 08:05 alongside paper_trade.py.
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SCORECARD_MD = ROOT / "scorecard.md"
SCORECARD_JSON = ROOT / "scorecard.json"
PORTFOLIO_LEDGER = ROOT / "portfolio_ledger.csv"
YIELD_SNAPSHOT = ROOT / "yield_snapshot.json"

# Load .env (since launchd doesn't inherit shell env)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line: continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

CAPITAL = float(os.environ.get("CAPITAL", "10000"))
DAILY_TARGET = float(os.environ.get("DAILY_TARGET", "50"))
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

FEE_BPS_SPOT = 10
FEE_BPS_PERP = 4
SLIP_BPS = 5


def get(url, params=None, retries=8):
    """Robust GET — handles DNS failures and slow startup after Mac wake."""
    last_exc = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params or {}, timeout=20)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.SSLError) as e:
            last_exc = e
            # Exponential backoff: 2, 4, 8, 16, 32, 60, 60, 60
            wait = min(60, 2 ** (i + 1))
            time.sleep(wait)
        except Exception as e:
            last_exc = e
            if i == retries - 1:
                raise
            time.sleep(2 + i)
    raise last_exc


# ============ Data fetchers ============

def fetch_binance_spot():
    r = get("https://api.binance.com/api/v3/klines",
            {"symbol": "BTCUSDT", "interval": "1d", "limit": 30})
    candles = [{"time": c[0], "high": float(c[2]), "low": float(c[3]),
                "close": float(c[4])} for c in r]
    return candles


def fetch_binance_funding():
    """Latest funding + projected for current 8h period."""
    hist = get("https://fapi.binance.com/fapi/v1/fundingRate",
               {"symbol": "BTCUSDT", "limit": 30}, retries=2)
    return [{"time": h["fundingTime"], "rate": float(h["fundingRate"])} for h in hist]


def fetch_bybit_funding():
    j = get("https://api.bybit.com/v5/market/funding/history",
            {"category": "linear", "symbol": "BTCUSDT", "limit": 30}, retries=2)
    rows = j["result"]["list"]
    return [{"time": int(r["fundingRateTimestamp"]), "rate": float(r["fundingRate"])}
            for r in rows]


def fetch_okx_funding():
    j = get("https://www.okx.com/api/v5/public/funding-rate-history",
            {"instId": "BTC-USDT-SWAP", "limit": 30}, retries=2)
    rows = j["data"]
    return [{"time": int(r["fundingTime"]), "rate": float(r["realizedRate"])}
            for r in rows]


def fetch_quarterly_basis():
    """Get current quarterly futures price for active contract."""
    info = get("https://fapi.binance.com/fapi/v1/exchangeInfo", retries=2)
    quarterlies = [s for s in info["symbols"]
                   if "BTC" in s["symbol"] and s.get("contractType") == "CURRENT_QUARTER"]
    if not quarterlies:
        return None
    sym = quarterlies[0]
    expiry_ms = sym.get("deliveryDate")
    expiry = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)
    ticker = get("https://fapi.binance.com/fapi/v1/ticker/price",
                 {"symbol": sym["symbol"]}, retries=2)
    spot_ticker = get("https://api.binance.com/api/v3/ticker/price",
                      {"symbol": "BTCUSDT"}, retries=2)
    fut_price = float(ticker["price"])
    spot_price = float(spot_ticker["price"])
    days = max((expiry - datetime.now(tz=timezone.utc)).total_seconds() / 86400, 0.1)
    basis_pct = (fut_price / spot_price - 1)
    ann_basis = basis_pct * 365 / days
    return {
        "symbol": sym["symbol"],
        "expiry": expiry.isoformat(),
        "days_to_expiry": round(days, 1),
        "fut_price": fut_price,
        "spot_price": spot_price,
        "basis_pct": basis_pct,
        "ann_basis": ann_basis,
    }


def safe_fetch(label, fn, fallback=None):
    try:
        return fn()
    except Exception as e:
        print(f"{label} fetch failed: {e}")
        return fallback


# ============ Signal computation ============

def donchian_signal(candles, window=20):
    if len(candles) < window + 1:
        return {"signal": "INSUFFICIENT_DATA"}
    closed = candles[:-1] if candles[-1]["time"] / 1000 > time.time() - 300 else candles
    lookback = closed[-(window + 1):-1] if len(closed) > window else closed[-window:]
    today = closed[-1]
    h = max(c["high"] for c in lookback)
    l = min(c["low"] for c in lookback)
    c = today["close"]
    if c > h:
        sig = "BUY"
    elif c < l:
        sig = "SELL"
    else:
        sig = "HOLD"
    # Distance to triggers
    to_buy = (h - c) / c
    to_sell = (c - l) / c
    return {
        "signal": sig,
        "close": c,
        "20d_high": h,
        "20d_low": l,
        "pct_to_breakout": to_buy,
        "pct_to_breakdown": to_sell,
    }


def _percentiles(values, qs=(0.25, 0.50, 0.75, 0.90)):
    if not values:
        return {f"p{int(q*100)}": 0.0 for q in qs}
    s = sorted(values)
    out = {}
    for q in qs:
        idx = int(q * (len(s) - 1))
        out[f"p{int(q*100)}"] = s[idx]
    return out


def historical_funding_apys(rates_list, lookback_days=30):
    """Compute distribution of 7d rolling APYs over the past lookback_days.
    Each 7d APY = mean(funding_rate over 7d) * 1095 / 2.
    """
    if not rates_list or len(rates_list) < 24:
        return {"p25": 0, "p50": 0, "p75": 0.04, "p90": 0.08}  # safe default
    # 8h funding → 21 entries per 7d window
    window = 21
    apys = []
    n_max = min(len(rates_list), lookback_days * 3 + window)
    recent = rates_list[-n_max:]
    for i in range(window, len(recent)):
        seg = [r["rate"] for r in recent[i - window:i]]
        avg = sum(seg) / len(seg)
        apys.append(avg * 365 * 3 / 2)
    return _percentiles(apys)


def historical_basis_dist():
    """Read data/basis_curve.csv (already maintained by backtest) for ann_basis distribution past 30d."""
    p = Path(__file__).parent / "data" / "basis_curve.csv"
    if not p.exists():
        return {"p25": 0, "p50": 0.02, "p75": 0.05, "p90": 0.10}
    try:
        import csv
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
        vals = []
        with p.open() as f:
            reader = csv.DictReader(f)
            for row in reader:
                d = row.get("date")
                if not d: continue
                try:
                    dt = datetime.fromisoformat(d.replace("+00:00", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if dt < cutoff: continue
                ab = row.get("ann_basis")
                if ab:
                    try: vals.append(float(ab))
                    except: pass
        return _percentiles(vals)
    except Exception:
        return {"p25": 0, "p50": 0.02, "p75": 0.05, "p90": 0.10}


def funding_score(rates_list):
    """Annualized funding APY on capital (2x notional needed)."""
    if not rates_list:
        return None
    # Last 7d average vs last 30d average
    rates_7d = rates_list[-21:]   # 3 per day * 7d
    rates_30d = rates_list[-90:]
    rates_3d = rates_list[-9:]
    avg_7d = sum(r["rate"] for r in rates_7d) / len(rates_7d)
    avg_30d = sum(r["rate"] for r in rates_30d) / len(rates_30d)
    avg_3d = sum(r["rate"] for r in rates_3d) / len(rates_3d)
    apy_7d = avg_7d * (365 * 3) / 2  # on capital (2x notional)
    apy_30d = avg_30d * (365 * 3) / 2
    apy_3d = avg_3d * (365 * 3) / 2
    return {
        "latest": rates_list[-1]["rate"],
        "apy_3d": apy_3d,
        "apy_7d": apy_7d,
        "apy_30d": apy_30d,
        "samples_30d": len(rates_30d),
    }


def realized_vol(candles, window=14):
    if len(candles) < window + 1:
        return None
    closes = [c["close"] for c in candles[-(window + 1):]]
    rets = [(closes[i] / closes[i-1] - 1) for i in range(1, len(closes))]
    import math
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    std = math.sqrt(var)
    return std * math.sqrt(365)


def regime(vol_ann, donchian_sig):
    """Classify market regime."""
    if vol_ann is None:
        return "UNKNOWN"
    if donchian_sig == "BUY":
        return "TREND_UP"
    if donchian_sig == "SELL":
        return "TREND_DOWN"
    if vol_ann > 0.80:
        return "CHOP_HIGH_VOL"
    if vol_ann < 0.40:
        return "CHOP_LOW_VOL"
    return "CHOP_MID_VOL"


def load_yield_recommendation():
    """Read the latest Pendle PT recommendation if yield_monitor.py has produced one."""
    if not YIELD_SNAPSHOT.exists():
        return None
    try:
        snap = json.loads(YIELD_SNAPSHOT.read_text())
        rec = snap.get("recommendation") or {}
        if not rec.get("available"):
            return None
        return {
            "pick": rec.get("pick", "N/A"),
            "apy": float(rec.get("apy_pct", 0)) / 100,
            "days_to_expiry": rec.get("days_to_expiry"),
            "liquidity_usd": rec.get("liquidity_usd"),
            "alloc_usd": rec.get("alloc_usd"),
            "source_ts": snap.get("ts"),
        }
    except Exception:
        return None


# ============ Decision engine ============

def decide_allocation(donchian, funding, basis, vol, regime_label,
                       funding_history_by_venue=None, basis_dist=None, yield_rec=None):
    """Adaptive allocation:
      - Relative tiers (funding > P75 of last 30d OR > 8% absolute)
      - Regime gate (high vol → reduce Donchian; trend_down → exit all risk)
      - ATR-style sizing for Donchian (signal strength scales position)
    """
    alloc = {"cash": 0, "btc_spot": 0, "funding_arb": 0, "basis_trade": 0, "yield_pt": 0}
    reasons = []

    # ===== 1. Donchian with breakout strength + vol regime gate =====
    if donchian["signal"] == "BUY":
        # Signal strength: how far above 20d high
        # (close - high) / high. > 0 means real breakout. Bigger = stronger.
        strength = max(0, (donchian["close"] - donchian["20d_high"]) / donchian["20d_high"])
        base_alloc = 60
        # Vol-target sizing: target 30% annualized vol exposure
        if vol and vol > 0:
            vol_scale = min(1.5, max(0.3, 0.30 / vol))
            sized = base_alloc * vol_scale
        else:
            sized = base_alloc
        # Stronger breakout → up to 1.2x size; weak → 0.8x
        sized *= (1 + min(0.2, strength * 5))
        alloc["btc_spot"] = int(round(min(70, sized)))
        reasons.append(f"Donchian BUY · strength={strength*100:.2f}% · vol={vol*100:.1f}% "
                       f"→ BTC {alloc['btc_spot']}%")
    elif donchian["signal"] == "SELL":
        reasons.append("Donchian SELL → 0% BTC (avoid downtrend)")

    # ===== 2. Funding: relative + absolute thresholds =====
    best_funding_apy = -1
    best_venue = None
    for venue, fund in funding.items():
        if fund and fund["apy_7d"] > best_funding_apy:
            best_funding_apy = fund["apy_7d"]
            best_venue = venue

    # Get per-venue P75 from history
    p75_funding = 0.08  # absolute floor
    p50_funding = 0.04
    p90_funding = 0.15
    if funding_history_by_venue and best_venue in funding_history_by_venue:
        h = funding_history_by_venue[best_venue]
        p50_funding = h.get("p50", p50_funding)
        p75_funding = max(0.04, h.get("p75", p75_funding))  # never below 4% to filter noise
        p90_funding = max(0.10, h.get("p90", p90_funding))

    if best_funding_apy >= p90_funding or best_funding_apy >= 0.12:
        alloc["funding_arb"] = 30 if donchian["signal"] != "BUY" else 20
        reasons.append(f"Funding TOP DECILE: {best_venue} 7d={best_funding_apy*100:.2f}% "
                       f"(≥P90={p90_funding*100:.2f}%) → {alloc['funding_arb']}%")
    elif best_funding_apy >= p75_funding:
        alloc["funding_arb"] = 20
        reasons.append(f"Funding UPPER Q: {best_venue} 7d={best_funding_apy*100:.2f}% "
                       f"(≥P75={p75_funding*100:.2f}%) → 20%")
    elif best_funding_apy >= p50_funding and best_funding_apy > 0.045:
        # Only allocate if beats cash (4.5%)
        alloc["funding_arb"] = 10
        reasons.append(f"Funding ABOVE MEDIAN+CASH: {best_funding_apy*100:.2f}% "
                       f"(≥P50={p50_funding*100:.2f}% & ≥4.5%) → 10%")
    elif best_funding_apy < 0.045:
        reasons.append(f"Funding {best_funding_apy*100:.2f}% < cash floor 4.5% → skip (cash dominates)")
    else:
        reasons.append(f"Funding {best_funding_apy*100:.2f}% < P50({p50_funding*100:.2f}%) → skip")

    # ===== 3. Basis: relative + absolute thresholds =====
    ann_basis = basis["ann_basis"] if basis else 0
    p75_basis = 0.10
    p50_basis = 0.05
    p90_basis = 0.15
    if basis_dist:
        p50_basis = basis_dist.get("p50", p50_basis)
        p75_basis = max(0.04, basis_dist.get("p75", p75_basis))
        p90_basis = max(0.08, basis_dist.get("p90", p90_basis))

    if ann_basis >= p90_basis or ann_basis >= 0.15:
        alloc["basis_trade"] = 20
        reasons.append(f"Basis TOP DECILE: {ann_basis*100:.2f}% (≥P90={p90_basis*100:.2f}%) → 20%")
    elif ann_basis >= p75_basis:
        alloc["basis_trade"] = 15
        reasons.append(f"Basis UPPER Q: {ann_basis*100:.2f}% (≥P75={p75_basis*100:.2f}%) → 15%")
    elif ann_basis >= p50_basis and ann_basis > 0.045:
        # Must beat cash
        alloc["basis_trade"] = 8
        reasons.append(f"Basis ABOVE MEDIAN+CASH: {ann_basis*100:.2f}% "
                       f"(≥P50={p50_basis*100:.2f}% & ≥4.5%) → 8%")
    elif ann_basis < 0.045:
        reasons.append(f"Basis {ann_basis*100:.2f}% < cash floor 4.5% → skip (cash dominates)")
    else:
        reasons.append(f"Basis {ann_basis*100:.2f}% < P50({p50_basis*100:.2f}%) → skip")

    # ===== 4. Yield PT: cash enhancement, not BTC risk =====
    yield_apy = yield_rec["apy"] if yield_rec else 0
    if yield_rec and yield_apy >= 0.09:
        alloc["yield_pt"] = 30
        reasons.append(f"Yield PT: {yield_rec['pick']} {yield_apy*100:.2f}% APY → 30% cash-enhancement sleeve")
    elif yield_rec and yield_apy >= 0.065:
        alloc["yield_pt"] = 20
        reasons.append(f"Yield PT: {yield_rec['pick']} {yield_apy*100:.2f}% APY → 20% cash-enhancement sleeve")
    elif yield_rec and yield_apy > 0.045:
        alloc["yield_pt"] = 10
        reasons.append(f"Yield PT: {yield_rec['pick']} {yield_apy*100:.2f}% APY beats cash → 10%")
    elif yield_rec:
        reasons.append(f"Yield PT {yield_rec['pick']} {yield_apy*100:.2f}% < cash floor 4.5% → skip")
    else:
        reasons.append("Yield PT snapshot unavailable → skip")

    # ===== 5. Regime gate: high vol or trend_down de-risk =====
    if regime_label == "CHOP_HIGH_VOL":
        scale = 0.6
        for k in ("btc_spot", "funding_arb", "basis_trade"):
            alloc[k] = int(round(alloc[k] * scale))
        reasons.append(f"HIGH_VOL regime · ALL risk allocations × {scale}")
    elif regime_label == "TREND_DOWN":
        # Already 0 BTC; cap others
        alloc["funding_arb"] = min(alloc["funding_arb"], 15)
        alloc["basis_trade"] = min(alloc["basis_trade"], 8)
        reasons.append("TREND_DOWN regime · arb caps tightened")

    # ===== 6. Total exposure cap =====
    total_risk = sum(v for k, v in alloc.items() if k not in ("cash", "yield_pt"))
    if total_risk > 80:
        # Scale down to 80% max risk
        scale = 80 / total_risk
        for k in ("btc_spot", "funding_arb", "basis_trade"):
            alloc[k] = int(round(alloc[k] * scale))
        reasons.append(f"Total risk capped at 80% (was {total_risk}%)")

    total_alloc = sum(v for k, v in alloc.items() if k != "cash")
    if total_alloc > 100:
        scale = 100 / total_alloc
        for k in ("btc_spot", "funding_arb", "basis_trade", "yield_pt"):
            alloc[k] = int(round(alloc[k] * scale))
        reasons.append(f"Total allocation capped at 100% (was {total_alloc}%)")

    alloc["cash"] = max(0, 100 - sum(v for k, v in alloc.items() if k != "cash"))
    reasons.append(f"Residual {alloc['cash']}% in USDT (~5% APY)")

    expected_apy = (
        alloc["btc_spot"] / 100 * 0.40 +
        alloc["funding_arb"] / 100 * max(best_funding_apy, 0) +
        alloc["basis_trade"] / 100 * max(ann_basis, 0) +
        alloc["yield_pt"] / 100 * max(yield_apy, 0) +
        alloc["cash"] / 100 * 0.045
    )
    expected_daily = CAPITAL * expected_apy / 365

    return {
        "allocation_pct": alloc,
        "best_funding_venue": best_venue,
        "best_funding_apy": best_funding_apy,
        "yield_pick": yield_rec["pick"] if yield_rec else None,
        "yield_apy": yield_apy,
        "regime": regime_label,
        "reasons": reasons,
        "expected_apy": expected_apy,
        "expected_daily_pnl": expected_daily,
        "thresholds": {
            "funding_p50": p50_funding, "funding_p75": p75_funding, "funding_p90": p90_funding,
            "basis_p50": p50_basis, "basis_p75": p75_basis, "basis_p90": p90_basis,
        },
    }


# ============ Render ============

def render_md(scorecard):
    s = scorecard
    b = s.get("basis") or {
        "symbol": "N/A",
        "expiry": "N/A",
        "days_to_expiry": 0.0,
        "spot_price": 0.0,
        "fut_price": 0.0,
        "basis_pct": 0.0,
        "ann_basis": 0.0,
    }
    md = f"""# BTC Super Agent — Daily Scorecard

**Date**: {s['date']}  ·  **BTC**: ${s['donchian']['close']:,.2f}  ·  **Regime**: `{s['decision']['regime']}`

## Decision Today

| Allocation | % | Notes |
|---|---|---|
| BTC spot (Donchian) | {s['decision']['allocation_pct']['btc_spot']}% | signal=`{s['donchian']['signal']}` |
| Funding arb | {s['decision']['allocation_pct']['funding_arb']}% | best venue: {s['decision']['best_funding_venue']} ({s['decision']['best_funding_apy']*100:+.2f}% APY 7d) |
| Basis trade | {s['decision']['allocation_pct']['basis_trade']}% | {b['ann_basis']*100:.2f}% ann ({b['days_to_expiry']:.0f} days TTE) |
| Yield PT | {s['decision']['allocation_pct']['yield_pt']}% | {s['decision']['yield_pick'] or 'N/A'} ({s['decision']['yield_apy']*100:.2f}% APY) |
| USDT cash | {s['decision']['allocation_pct']['cash']}% | ~5% APY (money market) |

**Expected APY (blended)**: **{s['decision']['expected_apy']*100:.2f}%**
**Expected daily P&L on ${CAPITAL:,.0f}**: **${s['decision']['expected_daily_pnl']:.2f}**
**$50/day target gap**: {(s['decision']['expected_daily_pnl'] - DAILY_TARGET):+.2f}

## Donchian Trend Layer

- Close: ${s['donchian']['close']:,.2f}
- 20d high: ${s['donchian']['20d_high']:,.2f}  → {s['donchian']['pct_to_breakout']*100:.2f}% to BUY trigger
- 20d low:  ${s['donchian']['20d_low']:,.2f}   → {s['donchian']['pct_to_breakdown']*100:.2f}% to SELL trigger
- Signal: **{s['donchian']['signal']}**

## Funding Arb Layer (annualized APY on capital, last 7d)

| Venue | Latest | 3d APY | 7d APY | 30d APY |
|---|---|---|---|---|
"""
    for venue in ("binance", "bybit", "okx"):
        f = s["funding"][venue]
        if f is None:
            md += f"| {venue.title()} | N/A | N/A | N/A | N/A |\n"
            continue
        md += (f"| {venue.title()} | {f['latest']*100:+.4f}% | "
               f"{f['apy_3d']*100:+.2f}% | {f['apy_7d']*100:+.2f}% | "
               f"{f['apy_30d']*100:+.2f}% |\n")

    md += f"""
## Basis Layer

- Contract: `{b['symbol']}` expires {b['expiry'][:10]} ({b['days_to_expiry']:.1f} days)
- Spot: ${b['spot_price']:,.2f}  ·  Futures: ${b['fut_price']:,.2f}
- Basis: {b['basis_pct']*100:+.3f}%  →  **annualized {b['ann_basis']*100:+.2f}%**

## Realized Vol

- 14d realized: {s['vol_ann']*100:.1f}% annualized

## Reasoning Log

"""
    for r in s["decision"]["reasons"]:
        md += f"- {r}\n"

    md += f"\n_Generated: {s['generated_at']}_\n"
    return md


def append_portfolio_ledger(s):
    basis = s.get("basis") or {"ann_basis": 0.0}
    if PORTFOLIO_LEDGER.exists():
        lines = PORTFOLIO_LEDGER.read_text().splitlines()
        if lines and "alloc_yield" not in lines[0]:
            header = lines[0].split(",")
            try:
                cash_idx = header.index("alloc_cash")
                header.insert(cash_idx, "alloc_yield")
                migrated = [",".join(header)]
                for line in lines[1:]:
                    if not line.strip():
                        continue
                    parts = line.split(",")
                    if len(parts) == len(header) - 1:
                        parts.insert(cash_idx, "0")
                    migrated.append(",".join(parts))
                PORTFOLIO_LEDGER.write_text("\n".join(migrated) + "\n")
            except ValueError:
                pass
    new = not PORTFOLIO_LEDGER.exists()
    with PORTFOLIO_LEDGER.open("a") as f:
        if new:
            f.write("date,btc_close,regime,donchian,best_funding_venue,best_funding_apy_7d,"
                    "ann_basis,vol_14d,alloc_btc,alloc_funding,alloc_basis,alloc_yield,alloc_cash,"
                    "expected_apy,expected_daily\n")
        f.write(f"{s['date']},{s['donchian']['close']:.2f},{s['decision']['regime']},"
                f"{s['donchian']['signal']},{s['decision']['best_funding_venue']},"
                f"{s['decision']['best_funding_apy']:.4f},"
                f"{basis['ann_basis']:.4f},{s['vol_ann']:.4f},"
                f"{s['decision']['allocation_pct']['btc_spot']},"
                f"{s['decision']['allocation_pct']['funding_arb']},"
                f"{s['decision']['allocation_pct']['basis_trade']},"
                f"{s['decision']['allocation_pct']['yield_pt']},"
                f"{s['decision']['allocation_pct']['cash']},"
                f"{s['decision']['expected_apy']:.4f},"
                f"{s['decision']['expected_daily_pnl']:.2f}\n")


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        return r.ok
    except Exception:
        return False


# ============ Main ============

def main():
    print("Fetching all signals...")
    candles = fetch_binance_spot()
    funding_data = {
        "binance": safe_fetch("binance funding", fetch_binance_funding, []),
        "bybit": safe_fetch("bybit funding", fetch_bybit_funding, []),
        "okx": safe_fetch("okx funding", fetch_okx_funding, []),
    }
    basis = safe_fetch("quarterly basis", fetch_quarterly_basis)

    donchian = donchian_signal(candles)
    funding_scores = {k: funding_score(v) for k, v in funding_data.items()}
    vol = realized_vol(candles)
    regime_label = regime(vol, donchian["signal"])

    # Compute relative thresholds from history
    funding_hist = {v: historical_funding_apys(rates_list) for v, rates_list in funding_data.items()}
    basis_dist = historical_basis_dist()
    yield_rec = load_yield_recommendation()
    decision = decide_allocation(donchian, funding_scores, basis, vol, regime_label,
                                 funding_history_by_venue=funding_hist, basis_dist=basis_dist,
                                 yield_rec=yield_rec)

    scorecard = {
        "date": datetime.now(tz=timezone.utc).date().isoformat(),
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "donchian": donchian,
        "funding": funding_scores,
        "basis": basis,
        "yield": yield_rec,
        "vol_ann": vol,
        "decision": decision,
    }

    SCORECARD_JSON.write_text(json.dumps(scorecard, indent=2, default=str))
    SCORECARD_MD.write_text(render_md(scorecard))
    append_portfolio_ledger(scorecard)

    # Print to stdout for cron log
    print(SCORECARD_MD.read_text())

    # Telegram triggers — multi-event
    state_file = ROOT / "agent_state.json"
    prev = json.loads(state_file.read_text()) if state_file.exists() else {}
    alerts = []

    # 1. Donchian signal change (most important — actionable)
    if prev.get("donchian_signal") and prev["donchian_signal"] != donchian["signal"]:
        emoji = "🟢" if donchian["signal"] == "BUY" else "🔴" if donchian["signal"] == "SELL" else "⚪"
        alerts.append(f"{emoji} *Donchian 信号变化*: {prev['donchian_signal']} → *{donchian['signal']}*")

    # 2. Funding broke into upper quartile
    p75_f = decision["thresholds"]["funding_p75"]
    if decision["best_funding_apy"] >= p75_f and prev.get("best_funding_apy", 0) < p75_f:
        alerts.append(f"🟢 *Funding 突破 P75 入场窗口*: {decision['best_funding_apy']*100:.2f}% APY @ {decision['best_funding_venue']}")

    # 3. Basis broke into upper quartile
    cur_basis = basis["ann_basis"] if basis else 0
    p75_b = decision["thresholds"]["basis_p75"]
    if cur_basis >= p75_b and prev.get("ann_basis", 0) < p75_b:
        alerts.append(f"🟢 *Basis 突破 P75*: {cur_basis*100:.2f}% ann · {basis['symbol']}")

    # 4. Approaching Donchian BUY (within 3%)
    pct_to_buy = donchian.get("pct_to_breakout", 1)
    if pct_to_buy < 0.03 and prev.get("pct_to_breakout", 1) >= 0.03:
        alerts.append(f"🟡 *接近 Donchian BUY*: BTC=${donchian['close']:,.0f} 距 BUY 仅 {pct_to_buy*100:.2f}%")

    # 5. Total allocation changed (system rebalance)
    cur_alloc = decision["allocation_pct"]
    prev_alloc = prev.get("allocation", {})
    if prev_alloc:
        total_change = sum(abs(cur_alloc.get(k, 0) - prev_alloc.get(k, 0)) for k in ("btc_spot", "funding_arb", "basis_trade", "yield_pt"))
        if total_change >= 20:  # significant rebalance
            alerts.append(f"⚡️ *组合大幅调仓 ({total_change}%)*: BTC={cur_alloc['btc_spot']}% Fund={cur_alloc['funding_arb']}% Basis={cur_alloc['basis_trade']}% Yield={cur_alloc['yield_pt']}% Cash={cur_alloc['cash']}%")

    if alerts:
        msg = (
            f"*🤖 BTC Agent · {scorecard['date']}*\n\n"
            + "\n\n".join(alerts)
            + f"\n\n━━━━━━\nBTC: `${donchian['close']:,.2f}` · Regime: `{decision['regime']}`"
            + f"\n预期年化: *{decision['expected_apy']*100:.2f}%* · 日盈亏: *${decision['expected_daily_pnl']:.2f}*"
            + f"\n[Dashboard](https://btc-quant.pages.dev/)"
        )
        send_telegram(msg)

    state_file.write_text(json.dumps({
        "donchian_signal": donchian["signal"],
        "best_funding_apy": decision["best_funding_apy"],
        "ann_basis": cur_basis,
        "pct_to_breakout": pct_to_buy,
        "allocation": cur_alloc,
    }))

    # Rebuild dashboard.html with fresh data
    try:
        import subprocess
        subprocess.run([
            "/Users/coco/btc-funding-arb/.venv/bin/python",
            "/Users/coco/btc-funding-arb/build_dashboard.py"
        ], check=False, timeout=60)
    except Exception as e:
        print(f"Dashboard rebuild skipped: {e}")


if __name__ == "__main__":
    main()
