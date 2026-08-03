# Bitget 观察模式刷新

观察模式只读取 Bitget 实时价格和 UTA 账户余额，不会下单。

## 刷新一次

```bash
/Users/coco/btc-funding-arb/.venv/bin/python /Users/coco/btc-funding-arb/run_bitget_observer_board.py --once --quote-usdt 5
```

这会更新：

- `data/bitget_observer_state.json`
- `data/bitget_observer_log.jsonl`
- `strategy_board.html`
- `deploy/strategy.html`

## 每 60 秒自动刷新

在本机终端里运行：

```bash
cd /Users/coco/btc-funding-arb
./start_bitget_observer_board.sh
```

停止：

```bash
cd /Users/coco/btc-funding-arb
./stop_bitget_observer_board.sh
```

日志：

```bash
tail -f /Users/coco/btc-funding-arb/data/bitget_observer_board.log
```

## 页面刷新

`strategy_board.html` 已加入：

- 顶部 `刷新` 按钮：手动刷新页面
- 60 秒倒计时：自动刷新页面
- BTC 现价：浏览器每 1 秒读取 Bitget ticker

注意：页面自动刷新只会重新加载 HTML。要让观察模块的数据也每分钟变新，需要同时运行上面的后台刷新脚本。
