# OKX Spot Execution

This project now has a minimal OKX spot execution layer.

## Files

- `exchange/okx_spot.py`: OKX REST client, signing, ticker, balance, limit order, cancel, order query.
- `okx_trade.py`: manual CLI for ticker, balance, dry-run orders, and guarded live orders.
- `okx_strategy_executor.py`: turns the current confluence strategy signal into an OKX spot buy-limit intent.
- `data/okx_order_ledger.jsonl`: local order intent / result log.

## Environment

Copy `.env.example` values into `.env` and fill:

```bash
OKX_API_KEY=
OKX_API_SECRET=
OKX_API_PASSPHRASE=
OKX_BASE_URL=https://www.okx.com
OKX_SIMULATED=1
OKX_ALLOW_LIVE=0
OKX_MAX_ORDER_USDT=100
OKX_DEFAULT_ORDER_USDT=50
```

Use API keys with trade permission only. Do not enable withdrawal permission.

## Dry Run

Manual dry-run limit buy:

```bash
/Users/coco/btc-funding-arb/.venv/bin/python okx_trade.py buy-limit --quote-usdt 50 --price 63000
```

Strategy dry-run:

```bash
/Users/coco/btc-funding-arb/.venv/bin/python okx_strategy_executor.py
```

Dry-run orders are not sent to OKX. They are logged with `DRY_RUN_NOT_SENT` or `BLOCKED_BY_SIGNAL`.

## Demo / Live

OKX demo trading:

```bash
OKX_SIMULATED=1
```

Production trading:

```bash
OKX_SIMULATED=0
```

Live orders require all three:

```bash
OKX_ALLOW_LIVE=1
--live
--confirm I_UNDERSTAND_LIVE_OKX_ORDER
```

Example:

```bash
/Users/coco/btc-funding-arb/.venv/bin/python okx_strategy_executor.py --quote-usdt 50 --live --confirm I_UNDERSTAND_LIVE_OKX_ORDER
```

Start with OKX demo or very small live order size.
