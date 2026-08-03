"""Pendle PT 固定收益监控器.

抓 Pendle V2 活跃市场 → 过滤稳定币 PT (流动性 ≥ $500K) → 按 implied APY 排序 →
写 yield_snapshot.json 给 dashboard 用，并按 $10K 底仓推荐配置。

为什么：现有 Donchian/套利策略都是方向性/事件驱动，没有"无风险"现金底仓。
Pendle PT 锁定到期日收益率，等同于链上 zero-coupon 票据，可作为闲置资金停泊。

跑法：launchd 每日 1 次（或手动 python yield_monitor.py）。
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

ROOT = Path(__file__).parent
SNAPSHOT = ROOT / "yield_snapshot.json"

CAPITAL = float(os.environ.get("CAPITAL", "10000"))
MIN_LIQUIDITY = 500_000
MIN_APY = 0.04
TOP_N = 12
SAFE_PICK_MAX_APY = 0.10
SAFE_PICK_MIN_LIQ = 3_000_000

PENDLE_API = "https://api-v2.pendle.finance/core/v1/1/markets/active"

_env_file = ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def get(url, retries=6):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.SSLError,
                requests.exceptions.HTTPError) as e:
            last = e
            time.sleep(min(60, 2 ** (i + 1)))
    raise last


def days_to_expiry(expiry_iso):
    try:
        exp = datetime.fromisoformat(expiry_iso.replace("Z", "+00:00"))
        delta = exp - datetime.now(timezone.utc)
        return max(0, int(delta.total_seconds() // 86400))
    except Exception:
        return None


def fetch_pt_stables():
    data = get(PENDLE_API)
    markets = data.get("markets", [])
    out = []
    for m in markets:
        if "stables" not in m.get("categoryIds", []):
            continue
        d = m.get("details") or {}
        liq = d.get("liquidity") or 0
        apy = d.get("impliedApy") or 0
        if liq < MIN_LIQUIDITY or apy < MIN_APY:
            continue
        out.append({
            "name": m.get("name"),
            "address": m.get("address"),
            "expiry": m.get("expiry"),
            "days_to_expiry": days_to_expiry(m.get("expiry", "")),
            "implied_apy": round(apy, 4),
            "underlying_apy": round(d.get("underlyingApy") or 0, 4),
            "aggregated_apy": round(d.get("aggregatedApy") or 0, 4),
            "liquidity_usd": round(liq, 0),
            "categories": m.get("categoryIds", []),
        })
    out.sort(key=lambda x: -x["implied_apy"])
    return out[:TOP_N]


def pick_safe(markets):
    """选 APY ≤ 10% 且流动性 ≥ $3M 的最高 APY 标的作为'保守底仓'."""
    safe = [m for m in markets
            if m["implied_apy"] <= SAFE_PICK_MAX_APY
            and m["liquidity_usd"] >= SAFE_PICK_MIN_LIQ]
    if not safe:
        return None
    return max(safe, key=lambda x: x["implied_apy"])


def build_recommendation(markets):
    """50% 底仓 PT + 50% 现金/策略 → 给出预估日收益."""
    safe = pick_safe(markets)
    if not safe:
        return {"available": False, "reason": "无合格保守底仓（需 APY≤10% & 流动性≥$3M）"}
    alloc_usd = CAPITAL * 0.5
    daily_yield = alloc_usd * safe["implied_apy"] / 365
    return {
        "available": True,
        "pick": safe["name"],
        "expiry": safe["expiry"],
        "days_to_expiry": safe["days_to_expiry"],
        "apy_pct": round(safe["implied_apy"] * 100, 2),
        "alloc_usd": round(alloc_usd, 2),
        "daily_usd": round(daily_yield, 2),
        "annual_usd": round(alloc_usd * safe["implied_apy"], 2),
        "liquidity_usd": safe["liquidity_usd"],
    }


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


def maybe_alert(snap, prev):
    """新机会推送：APY > 9% 且上次快照里不存在的新标的."""
    if not prev:
        return
    prev_names = {m["name"] for m in prev.get("markets", [])}
    new_high = [m for m in snap["markets"]
                if m["implied_apy"] > 0.09 and m["name"] not in prev_names]
    if not new_high:
        return
    lines = ["*🆕 Pendle PT 新机会*"]
    for m in new_high[:3]:
        lines.append(f"- `{m['name']}` APY *{m['implied_apy']*100:.2f}%* · liq ${m['liquidity_usd']/1e6:.1f}M · {m['days_to_expiry']}d")
    send_telegram("\n".join(lines))


def main():
    markets = fetch_pt_stables()
    prev = None
    if SNAPSHOT.exists():
        try:
            prev = json.loads(SNAPSHOT.read_text())
        except Exception:
            prev = None

    snap = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "Pendle V2 · Ethereum mainnet",
        "capital": CAPITAL,
        "filters": {
            "min_liquidity_usd": MIN_LIQUIDITY,
            "min_apy": MIN_APY,
            "top_n": TOP_N,
        },
        "markets": markets,
        "recommendation": build_recommendation(markets),
    }

    SNAPSHOT.write_text(json.dumps(snap, ensure_ascii=False, indent=2))
    print(f"Wrote {SNAPSHOT.name}: {len(markets)} PT markets")
    rec = snap["recommendation"]
    if rec.get("available"):
        print(f"  推荐底仓: {rec['pick']} @ {rec['apy_pct']}% APY")
        print(f"  分配: ${rec['alloc_usd']:,.0f}  日收益: ${rec['daily_usd']:.2f}  年收益: ${rec['annual_usd']:,.0f}")
    print(f"  Top 3:")
    for m in markets[:3]:
        print(f"    {m['name']:20s} APY={m['implied_apy']*100:5.2f}%  liq=${m['liquidity_usd']/1e6:5.1f}M  exp={m['days_to_expiry']}d")

    maybe_alert(snap, prev)


if __name__ == "__main__":
    main()
