"""BTC intraday swing strategy search.

Goal framing: try to find whether $10,000 can target roughly $50/day
using BTCUSDT intraday swings. This is an aggressive 0.5% daily target,
so the script tests leveraged futures-style long/short strategies with
explicit stops, fees, slippage, and drawdown reporting.

Data: Binance USDT-M futures 15m candles.
"""
from pathlib import Path
import itertools
import math
import time
import requests
import numpy as np
import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

SYMBOL = "BTCUSDT"
FAPI = "https://fapi.binance.com"
CSV = DATA / "btcusdt_15m_futures.csv"

CAPITAL = 10_000.0
TARGET_DAILY = 50.0
FEE_BPS = 4
SLIP_BPS = 2
COST_PER_SIDE = (FEE_BPS + SLIP_BPS) / 10_000
INTERVAL_MS = 15 * 60 * 1000


def get(url, params, retries=4):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code in (418, 429):
                time.sleep(2 ** (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(min(20, 2 ** i))
    raise last


def fetch_klines(start="2024-01-01", interval="15m"):
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    now_ms = int(time.time() * 1000)
    out = []
    cursor = start_ms
    while cursor < now_ms:
        rows = get(
            f"{FAPI}/fapi/v1/klines",
            {"symbol": SYMBOL, "interval": interval, "startTime": cursor, "limit": 1500},
        )
        if not rows:
            break
        out.extend(rows)
        last = rows[-1][0]
        if last <= cursor:
            break
        cursor = last + INTERVAL_MS
        print(f"  fetched {len(out):>6d} rows, last={pd.Timestamp(last, unit='ms', tz='UTC')}")
        time.sleep(0.08)

    df = pd.DataFrame(out, columns=[
        "openTime", "open", "high", "low", "close", "volume",
        "closeTime", "quoteVolume", "trades", "takerBase", "takerQuote", "ignore",
    ])
    df["ts"] = pd.to_datetime(df["openTime"], unit="ms", utc=True)
    for c in ["open", "high", "low", "close", "volume", "quoteVolume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df = df[["ts", "open", "high", "low", "close", "volume", "quoteVolume", "trades"]]
    df.to_csv(CSV, index=False)
    return df


def load_data():
    if CSV.exists():
        df = pd.read_csv(CSV, parse_dates=["ts"])
        if len(df) > 1000 and df["ts"].max() > pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=3):
            return df
    return fetch_klines()


def add_features(df):
    df = df.copy()
    df["ret"] = df["close"].pct_change()
    df["ema_fast"] = df["close"].ewm(span=48, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=192, adjust=False).mean()
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(96).mean()
    df["atr_pct"] = df["atr"] / df["close"]
    df["vol_rank"] = df["atr_pct"].rolling(96 * 30).rank(pct=True)
    delta = df["close"].diff()
    up = delta.clip(lower=0).rolling(14).mean()
    down = (-delta.clip(upper=0)).rolling(14).mean()
    rs = up / down.replace(0, np.nan)
    df["rsi"] = 100 - 100 / (1 + rs)
    df["bb_mid"] = df["close"].rolling(96).mean()
    df["bb_std"] = df["close"].rolling(96).std()
    df["z"] = (df["close"] - df["bb_mid"]) / df["bb_std"]
    return df


def run_strategy(df, breakout=48, atr_stop=1.6, rr=1.8, risk_pct=0.006,
                 leverage_cap=3.0, max_hold=64, vol_min=0.35):
    df = df.copy().reset_index(drop=True)
    df["hi"] = df["high"].rolling(breakout).max().shift(1)
    df["lo"] = df["low"].rolling(breakout).min().shift(1)

    equity = CAPITAL
    peak = CAPITAL
    pos = 0
    entry = stop = take = size_notional = 0.0
    hold = 0
    rows = []

    for r in df.itertuples(index=False):
        ts = r.ts
        c = float(r.close)
        h = float(r.high)
        l = float(r.low)
        pnl = 0.0
        action = "HOLD"

        if not np.isfinite(r.atr) or not np.isfinite(r.hi) or not np.isfinite(r.vol_rank):
            rows.append({"ts": ts, "equity": equity, "pnl": 0.0, "pos": pos, "action": "WARMUP"})
            continue

        if pos != 0:
            hold += 1
            exit_price = None
            if pos == 1:
                if l <= stop:
                    exit_price = stop
                    action = "STOP_LONG"
                elif h >= take:
                    exit_price = take
                    action = "TAKE_LONG"
                elif hold >= max_hold:
                    exit_price = c
                    action = "TIME_LONG"
            else:
                if h >= stop:
                    exit_price = stop
                    action = "STOP_SHORT"
                elif l <= take:
                    exit_price = take
                    action = "TAKE_SHORT"
                elif hold >= max_hold:
                    exit_price = c
                    action = "TIME_SHORT"

            if exit_price is not None:
                gross = pos * (exit_price / entry - 1) * size_notional
                fees = size_notional * COST_PER_SIDE
                pnl = gross - fees
                equity += pnl
                pos = 0
                entry = stop = take = size_notional = 0.0
                hold = 0

        if pos == 0 and equity > 0:
            trend_up = r.ema_fast > r.ema_slow
            trend_down = r.ema_fast < r.ema_slow
            active_vol = r.vol_rank >= vol_min
            risk_usd = equity * risk_pct
            if active_vol and trend_up and c > r.hi:
                entry = c
                stop = entry - atr_stop * r.atr
                risk_per_notional = max((entry - stop) / entry, 1e-6)
                size_notional = min(equity * leverage_cap, risk_usd / risk_per_notional)
                take = entry + rr * (entry - stop)
                equity -= size_notional * COST_PER_SIDE
                pos = 1
                hold = 0
                action = "BUY"
            elif active_vol and trend_down and c < r.lo:
                entry = c
                stop = entry + atr_stop * r.atr
                risk_per_notional = max((stop - entry) / entry, 1e-6)
                size_notional = min(equity * leverage_cap, risk_usd / risk_per_notional)
                take = entry - rr * (stop - entry)
                equity -= size_notional * COST_PER_SIDE
                pos = -1
                hold = 0
                action = "SHORT"

        peak = max(peak, equity)
        rows.append({"ts": ts, "equity": equity, "pnl": pnl, "pos": pos, "action": action})

    bt = pd.DataFrame(rows)
    bt["date"] = pd.to_datetime(bt["ts"], utc=True).dt.floor("1D")
    bt["ret"] = bt["equity"].pct_change().fillna(0)
    return bt


def run_mean_reversion(df, z_entry=2.0, atr_stop=1.5, rr=1.2, risk_pct=0.004,
                       leverage_cap=2.0, max_hold=32, vol_max=0.75):
    df = df.copy().reset_index(drop=True)
    equity = CAPITAL
    peak = CAPITAL
    pos = 0
    entry = stop = take = size_notional = 0.0
    hold = 0
    rows = []

    for r in df.itertuples(index=False):
        ts = r.ts
        c = float(r.close)
        h = float(r.high)
        l = float(r.low)
        pnl = 0.0
        action = "HOLD"

        if not np.isfinite(r.atr) or not np.isfinite(r.z) or not np.isfinite(r.rsi) or not np.isfinite(r.vol_rank):
            rows.append({"ts": ts, "equity": equity, "pnl": 0.0, "pos": pos, "action": "WARMUP"})
            continue

        if pos != 0:
            hold += 1
            exit_price = None
            if pos == 1:
                if l <= stop:
                    exit_price = stop
                    action = "STOP_LONG"
                elif h >= take:
                    exit_price = take
                    action = "TAKE_LONG"
                elif c >= r.bb_mid:
                    exit_price = c
                    action = "MEAN_LONG"
                elif hold >= max_hold:
                    exit_price = c
                    action = "TIME_LONG"
            else:
                if h >= stop:
                    exit_price = stop
                    action = "STOP_SHORT"
                elif l <= take:
                    exit_price = take
                    action = "TAKE_SHORT"
                elif c <= r.bb_mid:
                    exit_price = c
                    action = "MEAN_SHORT"
                elif hold >= max_hold:
                    exit_price = c
                    action = "TIME_SHORT"

            if exit_price is not None:
                gross = pos * (exit_price / entry - 1) * size_notional
                fees = size_notional * COST_PER_SIDE
                pnl = gross - fees
                equity += pnl
                pos = 0
                entry = stop = take = size_notional = 0.0
                hold = 0

        if pos == 0 and equity > 0:
            active_vol = r.vol_rank <= vol_max
            risk_usd = equity * risk_pct
            if active_vol and r.z <= -z_entry and r.rsi <= 35:
                entry = c
                stop = entry - atr_stop * r.atr
                risk_per_notional = max((entry - stop) / entry, 1e-6)
                size_notional = min(equity * leverage_cap, risk_usd / risk_per_notional)
                take = entry + rr * (entry - stop)
                equity -= size_notional * COST_PER_SIDE
                pos = 1
                hold = 0
                action = "BUY_MR"
            elif active_vol and r.z >= z_entry and r.rsi >= 65:
                entry = c
                stop = entry + atr_stop * r.atr
                risk_per_notional = max((stop - entry) / entry, 1e-6)
                size_notional = min(equity * leverage_cap, risk_usd / risk_per_notional)
                take = entry - rr * (stop - entry)
                equity -= size_notional * COST_PER_SIDE
                pos = -1
                hold = 0
                action = "SHORT_MR"

        peak = max(peak, equity)
        rows.append({"ts": ts, "equity": equity, "pnl": pnl, "pos": pos, "action": action})

    bt = pd.DataFrame(rows)
    bt["date"] = pd.to_datetime(bt["ts"], utc=True).dt.floor("1D")
    bt["ret"] = bt["equity"].pct_change().fillna(0)
    return bt


def summarize(bt, label, params):
    daily = bt.groupby("date").agg(equity=("equity", "last"), pnl=("pnl", "sum")).reset_index()
    years = len(daily) / 365
    total = daily["equity"].iloc[-1] / CAPITAL - 1
    cagr = (1 + total) ** (1 / max(years, 1e-9)) - 1 if total > -0.999 else -1
    daily_ret = daily["equity"].pct_change().fillna(0)
    vol = daily_ret.std() * math.sqrt(365)
    sharpe = daily_ret.mean() * 365 / vol if vol > 1e-12 else np.nan
    dd = (daily["equity"] / daily["equity"].cummax() - 1).min()
    trades = int((bt["action"].isin(["BUY", "SHORT", "BUY_MR", "SHORT_MR"])).sum())
    return {
        "label": label,
        "params": params,
        "days": len(daily),
        "trades": trades,
        "avg_daily": daily["pnl"].mean(),
        "median_daily": daily["pnl"].median(),
        "hit_50_pct": (daily["pnl"] >= TARGET_DAILY).mean() * 100,
        "loss_50_pct": (daily["pnl"] <= -TARGET_DAILY).mean() * 100,
        "worst_day": daily["pnl"].min(),
        "best_day": daily["pnl"].max(),
        "cagr": cagr,
        "sharpe": sharpe,
        "max_dd": dd,
        "final": daily["equity"].iloc[-1],
    }


def fmt(s):
    p = s["params"]
    if "breakout" in p:
        tail = (
            f"b={p['breakout']} stop={p['atr_stop']} rr={p['rr']} "
            f"risk={p['risk_pct']*100:.1f}% lev={p['leverage_cap']:.1f}"
        )
    else:
        tail = (
            f"z={p['z_entry']} stop={p['atr_stop']} rr={p['rr']} "
            f"risk={p['risk_pct']*100:.1f}% lev={p['leverage_cap']:.1f} "
            f"volmax={p['vol_max']}"
        )
    return (
        f"  {s['label']:<18} avg=${s['avg_daily']:>7.2f} hit50={s['hit_50_pct']:>5.1f}% "
        f"CAGR={s['cagr']*100:>7.1f}% Sharpe={s['sharpe']:>5.2f} "
        f"MaxDD={s['max_dd']*100:>7.1f}% Final=${s['final']:>9,.0f} "
        f"trades={s['trades']:>4d} {tail}"
    )


def main():
    df = add_features(load_data())
    print(f"Data: {len(df)} 15m bars, {df['ts'].min()} -> {df['ts'].max()}")
    print(f"Target: ${TARGET_DAILY:.0f}/day on ${CAPITAL:,.0f} = {TARGET_DAILY/CAPITAL*100:.2f}% per day\n")

    train_end = df["ts"].max() - pd.Timedelta(days=90)
    train = df[df["ts"] < train_end].reset_index(drop=True)
    test = df[df["ts"] >= train_end].reset_index(drop=True)

    trend_grid = list(itertools.product(
        [32, 48, 96],
        [1.2, 1.6],
        [1.4, 1.8, 2.4],
        [0.004, 0.006],
        [2.0, 3.0],
        [0.35],
    ))
    mr_grid = list(itertools.product(
        [1.8, 2.0, 2.3],
        [1.2, 1.5, 1.8],
        [0.8, 1.2, 1.6],
        [0.003, 0.004, 0.006],
        [1.5, 2.0, 3.0],
        [0.55, 0.75],
    ))
    results = []
    survivors = []
    for breakout, atr_stop, rr, risk_pct, leverage_cap, vol_min in trend_grid:
        params = {
            "breakout": breakout,
            "atr_stop": atr_stop,
            "rr": rr,
            "risk_pct": risk_pct,
            "leverage_cap": leverage_cap,
            "vol_min": vol_min,
        }
        bt = run_strategy(train, **params)
        s = summarize(bt, "trend_train", params)
        results.append(s)
        if s["trades"] >= 40 and s["max_dd"] > -0.35:
            survivors.append(s)

    for z_entry, atr_stop, rr, risk_pct, leverage_cap, vol_max in mr_grid:
        params = {
            "z_entry": z_entry,
            "atr_stop": atr_stop,
            "rr": rr,
            "risk_pct": risk_pct,
            "leverage_cap": leverage_cap,
            "max_hold": 32,
            "vol_max": vol_max,
        }
        bt = run_mean_reversion(train, **params)
        s = summarize(bt, "mr_train", params)
        results.append(s)
        if s["trades"] >= 40 and s["max_dd"] > -0.35:
            survivors.append(s)

    results.sort(key=lambda x: (x["avg_daily"], x["sharpe"]), reverse=True)
    survivors.sort(key=lambda x: (x["avg_daily"], x["sharpe"]), reverse=True)
    print("Top train candidates before safety filter:")
    for s in results[:8]:
        print(fmt(s))

    top = survivors[:8]
    print("\nTop train candidates after trade-count/drawdown filter:")
    for s in top:
        print(fmt(s))

    if not top:
        print("\nNo candidate survived trade-count and drawdown filters.")
        return

    print("\nForward test on most recent 90 days:")
    test_rows = []
    for cand in top[:5]:
        runner = run_mean_reversion if cand["label"].startswith("mr_") else run_strategy
        bt = runner(test, **cand["params"])
        s = summarize(bt, "test", cand["params"])
        test_rows.append(s)
        print(fmt(s))

    best = max(test_rows, key=lambda x: (x["avg_daily"], x["max_dd"]))
    best_train = top[test_rows.index(best)]
    runner = run_mean_reversion if best_train["label"].startswith("mr_") else run_strategy
    full_bt = runner(df, **best["params"])
    full_s = summarize(full_bt, "full", best["params"])
    print("\nSelected candidate full-period result:")
    print(fmt(full_s))

    daily = full_bt.groupby(pd.to_datetime(full_bt["ts"], utc=True).dt.floor("1D")).agg(
        equity=("equity", "last"),
        pnl=("pnl", "sum"),
    ).reset_index().rename(columns={"ts": "date"})
    full_bt.to_csv(DATA / "intraday_swing_trades.csv", index=False)
    daily.to_csv(DATA / "intraday_swing_daily.csv", index=False)
    print("\nSaved data/intraday_swing_trades.csv and data/intraday_swing_daily.csv")

    last = full_bt.tail(1).iloc[0]
    print(f"\nCurrent model state: pos={int(last['pos'])} equity=${last['equity']:,.2f} as of {last['ts']}")
    print("pos: 1=long, -1=short, 0=flat")


if __name__ == "__main__":
    main()
