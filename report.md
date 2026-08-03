# BTC Funding Arbitrage Backtest

Period: 2019-09-10 -> 2026-06-21  (6.78 years, 7429 8h periods)

## Headline (full period, fee=4bps + slip=1bp per leg)

| metric | value |
|---|---|
| CAGR (on deployed capital = spot notional + perp margin) | **6.01%** |
| Annualized vol | 0.92% |
| Sharpe | 6.35 |
| Max drawdown | -1.14% |
| % periods with positive funding (we earn) | 85.3% |
| Funding-only APY (no basis drift) | 5.88% |

## Yearly breakdown

| year | CAGR | MaxDD | Funding-only | PosFunding% |
|---|---|---|---|---|
| 2019 | 4.00% | -0.80% | 3.74% | 81.7% |
| 2020 | 8.72% | -1.14% | 8.60% | 85.7% |
| 2021 | 16.56% | -0.34% | 15.30% | 92.7% |
| 2022 | 2.09% | -0.23% | 2.08% | 77.9% |
| 2023 | 3.92% | -0.09% | 3.93% | 89.9% |
| 2024 | 6.18% | -0.10% | 5.96% | 91.6% |
| 2025 | 2.59% | -0.05% | 2.56% | 87.1% |
| 2026 | 0.37% | -0.22% | 0.49% | 60.4% |

## Caveats (real-world will be worse)

1. **No rebalance cost in mid-period**: a delta-neutral book diverges as price moves; in
   practice you re-hedge weekly to keep margin healthy. Each rebalance = 2 legs * fee.
2. **No funding flip exit logic**: a smart implementation withdraws when funding goes
   negative. The CAGR here is the *naive always-on* number; selective entry is higher.
3. **Spot fees > perp fees** on Binance for retail (10 vs 4 bps); we used uniform 4 bps.
4. **Liquidation tail risk**: short perp on 1:1 margin survives any move; but if you
   add leverage (3-5x typical), a 20% spike requires margin top-up or you blow up.
5. **2022 USDT depeg / FTX week**: funding flipped negative for days. See "Worst 10
   single 8h funding payments" in console output.
6. **Binance funding cap**: clamped to +/-0.75% per 8h. Real outliers got capped.

## Portfolio Policy Update

Generated from `backtest_portfolio.py` using existing strategy outputs through 2026-06-29.

| strategy | CAGR | Annualized vol | Sharpe | MaxDD | Final equity |
|---|---:|---:|---:|---:|---:|
| Cash only (5% APY) | 5.13% | 0.00% | N/A | 0.00% | $131,030 |
| 30% yield + 70% cash | 6.40% | 0.00% | N/A | 0.00% | $139,810 |
| Policy portfolio | **9.63%** | 6.18% | 1.52 | -4.70% | $164,416 |

Current historical-state allocation on latest local data:

| sleeve | allocation | trigger state |
|---|---:|---|
| BTC trend | 0% | Vol-targeted trend is flat |
| Funding carry | 0% | trailing best venue APY 1.51% < 6.5% gate |
| Quarterly basis | 0% | annualized basis 3.51% < 6.5% gate |
| Yield PT | 30% | conservative fixed-yield sleeve |
| Cash | 70% | reserve / dry powder |

Practical read: the range overlay lifted full-period returns but made the left tail worse
(-63.73% MaxDD and negative 2026 YTD). Keep it out of the default allocator until it has a
volatility or regime filter. The more defensible live posture is cash/yield heavy until trend,
funding, or basis clears its gate.
