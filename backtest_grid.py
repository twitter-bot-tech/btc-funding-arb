"""Spot grid trading backtest for $10K capital, $100/day target.

Strategy
--------
Multi-pair concurrent grids on high-vol altcoins:
  - Split $10K across N coins (default 5: PEPE, WIF, DOGE, SOL, ETH).
  - For each: divide allocated capital into grid_levels buy/sell orders.
  - Buy at each lower grid line, sell at each upper line.
  - Grid spacing = volatility-adaptive (ATR-based).
  - Hard stop: if price breaks 20d high or 20d low, EXIT all positions (no whipsaw).

Backtest uses Binance 1h klines, 90 days.
Fees 0.1% taker, slippage 5bps.
"""
import json
import time
import math
from pathlib import Path
import requests
import numpy as np
import pandas as pd

DATA = Path(__file__).parent / "data" / "grid"
DATA.mkdir(parents=True, exist_ok=True)

CAPITAL = 10_000.0
TARGET_DAILY = 100.0
FEE_BPS = 10
SLIP_BPS = 5
COST_PER_LEG = (FEE_BPS + SLIP_BPS) / 10000

PAIRS = ["PEPEUSDT", "WIFUSDT", "DOGEUSDT", "SOLUSDT", "ETHUSDT"]


def fetch_klines(symbol, days=90, interval="1h"):
    """Fetch up to `days` 1h klines from Binance."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    out = []
    cur = start_ms
    while cur < end_ms:
        r = requests.get("https://api.binance.com/api/v3/klines",
                          params={"symbol": symbol, "interval": interval,
                                  "startTime": cur, "limit": 1000},
                          timeout=15)
        r.raise_for_status()
        rows = r.json()
        if not rows: break
        out.extend(rows)
        last = rows[-1][0]
        if last <= cur: break
        cur = last + 1
        if len(rows) < 1000: break
        time.sleep(0.1)
    df = pd.DataFrame(out, columns=["openTime","open","high","low","close","volume",
                                     "closeTime","qav","trades","tbav","tqav","ignore"])
    df["ts"] = pd.to_datetime(df["openTime"], unit="ms", utc=True)
    for c in ("open","high","low","close","volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df[["ts","open","high","low","close","volume"]].dropna().reset_index(drop=True)


def backtest_grid_single(df, capital, n_levels=10, atr_periods=24, breakout_window=20*24):
    """Backtest grid on a single pair.

    Mechanics
    ---------
    - Compute rolling ATR(24h) for grid spacing.
    - Center grid on current price, spread = 1.5x ATR up/down with n_levels.
    - As price moves through grid lines, fill orders:
        - Price crosses DOWN a line: BUY (allocate capital/n_levels per level)
        - Price crosses UP a line: SELL (if we own units bought at lower line)
    - Track inventory FIFO.
    - Breakout exit: if price > 20d high or < 20d low, dump everything and pause until back in range.
    """
    # Pre-compute ATR
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(atr_periods).mean()
    bb_hi = df["high"].rolling(breakout_window).max().shift(1)
    bb_lo = df["low"].rolling(breakout_window).min().shift(1)

    cash = capital
    units = []  # FIFO list of (qty, buy_price)
    trades = 0
    realized = 0.0
    equity_curve = []
    grid_center = None
    grid_lines = []
    state = "active"  # 'active' or 'paused' (during breakout)

    for i in range(len(df)):
        row = df.iloc[i]
        price = row["close"]
        a = atr.iloc[i] if not pd.isna(atr.iloc[i]) else None
        hi = bb_hi.iloc[i] if not pd.isna(bb_hi.iloc[i]) else None
        lo = bb_lo.iloc[i] if not pd.isna(bb_lo.iloc[i]) else None

        if a is None or hi is None or lo is None:
            equity_curve.append(cash + sum(q * price for q, _ in units))
            continue

        # Breakout: dump positions
        if (hi and price > hi) or (lo and price < lo):
            if state != "paused" and units:
                # Liquidate FIFO at current price
                for q, bp in units:
                    proceeds = q * price * (1 - COST_PER_LEG)
                    realized += proceeds - q * bp
                    cash += proceeds
                    trades += 1
                units = []
                state = "paused"
                grid_lines = []
                grid_center = None
            equity_curve.append(cash)
            continue

        # Re-arm grid when back in range
        if state == "paused":
            state = "active"
            grid_center = price
            spread = 1.5 * a
            step = spread / (n_levels / 2)
            grid_lines = [grid_center + step * (k - n_levels // 2) for k in range(n_levels + 1)]
            grid_lines.sort()

        # Build grid on first bar
        if not grid_lines:
            grid_center = price
            spread = 1.5 * a
            step = spread / (n_levels / 2)
            grid_lines = [grid_center + step * (k - n_levels // 2) for k in range(n_levels + 1)]
            grid_lines.sort()

        # Crossing detection: which grid lines does this bar's [low, high] cross?
        for line in grid_lines:
            if row["low"] <= line <= row["high"]:
                if price <= line:
                    # Downward cross -> BUY
                    if cash > capital / n_levels:
                        size_usd = capital / n_levels
                        qty = size_usd / line * (1 - COST_PER_LEG)
                        cost = size_usd
                        if cash >= cost:
                            cash -= cost
                            units.append((qty, line))
                            trades += 1
                else:
                    # Upward cross -> SELL oldest unit bought below this line
                    sell_idx = next((j for j, (q, bp) in enumerate(units) if bp < line), None)
                    if sell_idx is not None:
                        q, bp = units.pop(sell_idx)
                        proceeds = q * line * (1 - COST_PER_LEG)
                        realized += proceeds - q * bp
                        cash += proceeds
                        trades += 1

        # Mark-to-market equity
        equity = cash + sum(q * price for q, _ in units)
        equity_curve.append(equity)

    eq = pd.Series(equity_curve, index=df["ts"])
    return {
        "equity_curve": eq,
        "final_equity": eq.iloc[-1],
        "realized": realized,
        "unrealized": eq.iloc[-1] - capital - realized,
        "trades": trades,
        "max_dd": float((eq / eq.cummax() - 1).min()),
        "n_bars": len(df),
        "days": (df["ts"].iloc[-1] - df["ts"].iloc[0]).total_seconds() / 86400,
    }


def main():
    print(f"Fetching 90d 1h data for {len(PAIRS)} pairs...")
    dfs = {}
    for p in PAIRS:
        try:
            df = fetch_klines(p, days=90, interval="1h")
            dfs[p] = df
            print(f"  {p}: {len(df)} bars, ${df['close'].iloc[-1]:.6f}, "
                  f"range_pct {(df['high'].max()/df['low'].min()-1)*100:.1f}%")
        except Exception as e:
            print(f"  {p}: FAILED ({e})")

    if not dfs:
        return
    capital_per_pair = CAPITAL / len(dfs)
    print(f"\nBacktesting grid on each pair · ${capital_per_pair:.0f} per pair...\n")

    results = []
    total_eq = None
    for p, df in dfs.items():
        r = backtest_grid_single(df, capital_per_pair, n_levels=10)
        days = r["days"]
        cagr = (r["final_equity"] / capital_per_pair) ** (365 / max(days, 1)) - 1
        daily_pnl = (r["final_equity"] - capital_per_pair) / max(days, 1)
        print(f"  {p:<12} final=${r['final_equity']:>8.2f}  "
              f"realized=${r['realized']:>+7.2f}  unrealized=${r['unrealized']:>+7.2f}  "
              f"trades={r['trades']:>4d}  MaxDD={r['max_dd']*100:>6.2f}%  "
              f"CAGR={cagr*100:>7.2f}%  daily=${daily_pnl:>+6.2f}")
        results.append({"pair": p, **{k: v for k, v in r.items() if k != "equity_curve"},
                        "cagr": cagr, "daily_pnl": daily_pnl,
                        "capital_per_pair": capital_per_pair})
        if total_eq is None:
            total_eq = r["equity_curve"].copy()
        else:
            total_eq = total_eq.add(r["equity_curve"], fill_value=capital_per_pair)

    # Portfolio summary
    print("\n" + "="*100)
    print("PORTFOLIO ($10,000 split equally)")
    print("="*100)
    total_final = sum(r["final_equity"] for r in results)
    total_realized = sum(r["realized"] for r in results)
    total_unrealized = sum(r["unrealized"] for r in results)
    avg_days = sum(r["days"] for r in results) / len(results)
    total_trades = sum(r["trades"] for r in results)
    portfolio_cagr = (total_final / CAPITAL) ** (365 / max(avg_days, 1)) - 1
    portfolio_daily = (total_final - CAPITAL) / max(avg_days, 1)
    # Worst portfolio drawdown
    port_dd = float((total_eq / total_eq.cummax() - 1).min()) if total_eq is not None else 0
    print(f"  Final equity:        ${total_final:,.2f}  (from ${CAPITAL:,.0f})")
    print(f"  Realized P&L:        ${total_realized:+,.2f}")
    print(f"  Unrealized P&L:      ${total_unrealized:+,.2f}")
    print(f"  Total trades:        {total_trades}")
    print(f"  Days backtested:     {avg_days:.1f}")
    print(f"  Portfolio CAGR:      {portfolio_cagr*100:+.2f}%")
    print(f"  **Daily avg P&L:    ${portfolio_daily:+.2f}**")
    print(f"  Portfolio MaxDD:    {port_dd*100:.2f}%")
    print(f"  Target gap ($100):   {portfolio_daily - TARGET_DAILY:+.2f}")

    # Save
    out = {
        "capital": CAPITAL, "target_daily": TARGET_DAILY,
        "pairs": PAIRS, "results": results,
        "portfolio": {
            "final_equity": total_final, "realized": total_realized,
            "unrealized": total_unrealized, "trades": total_trades,
            "days": avg_days, "cagr": portfolio_cagr,
            "daily_pnl": portfolio_daily, "max_dd": port_dd,
        },
        "equity_curve": [(str(t), float(v)) for t, v in total_eq.items()] if total_eq is not None else [],
    }
    Path("/Users/coco/btc-funding-arb/grid_backtest.json").write_text(json.dumps(out, indent=2, default=str))
    print("\nSaved grid_backtest.json")


if __name__ == "__main__":
    main()
