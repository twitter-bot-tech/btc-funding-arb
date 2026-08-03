# Multi-Exchange Spot Execution

Supported adapters:

- OKX spot: `okx_trade.py`
- Bybit V5 spot: `bybit_trade.py`
- Gate API v4 spot: `gate_trade.py`
- Bitget UTA API v3 spot: `bitget_trade.py`

All scripts default to dry-run. No live order is sent unless the exchange-specific allow flag, `--live`, and confirmation text are all present.

## Unified Strategy Executor

Use the same BTC confluence strategy across exchanges:

```bash
/Users/coco/btc-funding-arb/.venv/bin/python strategy_executor.py --exchange bitget
/Users/coco/btc-funding-arb/.venv/bin/python strategy_executor.py --exchange bybit
/Users/coco/btc-funding-arb/.venv/bin/python strategy_executor.py --exchange gate
/Users/coco/btc-funding-arb/.venv/bin/python strategy_executor.py --exchange okx
```

Default behavior is dry-run. If the strategy signal is not ready, the status is `BLOCKED_BY_SIGNAL`.

For connectivity testing only, you can bypass the signal while staying dry-run:

```bash
/Users/coco/btc-funding-arb/.venv/bin/python strategy_executor.py --exchange bitget --ignore-signal
```

## Bybit

Environment:

```bash
BYBIT_API_KEY=
BYBIT_API_SECRET=
BYBIT_TESTNET=1
BYBIT_BASE_URL=https://api-testnet.bybit.com
BYBIT_ALLOW_LIVE=0
```

Dry-run:

```bash
/Users/coco/btc-funding-arb/.venv/bin/python bybit_trade.py buy-limit --quote-usdt 50 --price 63000
```

Live guard:

```bash
BYBIT_ALLOW_LIVE=1 /Users/coco/btc-funding-arb/.venv/bin/python bybit_trade.py buy-limit --quote-usdt 50 --price 63000 --live --confirm I_UNDERSTAND_LIVE_BYBIT_ORDER
```

Bybit V5 spot order endpoint: `POST /v5/order/create`, with `category=spot`.

## Gate

Environment:

```bash
GATE_API_KEY=
GATE_API_SECRET=
GATE_BASE_URL=https://api.gateio.ws
GATE_ALLOW_LIVE=0
```

Dry-run:

```bash
/Users/coco/btc-funding-arb/.venv/bin/python gate_trade.py buy-limit --quote-usdt 50 --price 63000
```

Live guard:

```bash
GATE_ALLOW_LIVE=1 /Users/coco/btc-funding-arb/.venv/bin/python gate_trade.py buy-limit --quote-usdt 50 --price 63000 --live --confirm I_UNDERSTAND_LIVE_GATE_ORDER
```

Gate spot order endpoint: `POST /api/v4/spot/orders`, with `currency_pair=BTC_USDT`.

## Bitget

Environment:

```bash
BITGET_API_KEY=
BITGET_API_SECRET=
BITGET_API_PASSPHRASE=
BITGET_BASE_URL=https://api.bitget.com
BITGET_ALLOW_LIVE=0
```

Dry-run:

```bash
/Users/coco/btc-funding-arb/.venv/bin/python bitget_trade.py buy-limit --quote-usdt 50 --price 63000
```

Live guard:

```bash
BITGET_ALLOW_LIVE=1 /Users/coco/btc-funding-arb/.venv/bin/python bitget_trade.py buy-limit --quote-usdt 50 --price 63000 --live --confirm I_UNDERSTAND_LIVE_BITGET_ORDER
```

Bitget UTA spot order endpoint: `POST /api/v3/trade/place-order`, with `category=SPOT` and `symbol=BTCUSDT`.

## Permissions

Use trade/read-only API keys. Never enable withdrawal permission.
