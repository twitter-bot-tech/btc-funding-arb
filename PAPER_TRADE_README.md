# BTC Donchian(20) Paper Trader — 落地手册

## 是什么

每天检查 BTC 现货 20 日突破信号，模拟交易（不动真钱），记录账本。30 天后对比纸面 PnL 和回测预期，验证策略是否在生产环境里成立。

## 文件

| 文件 | 作用 |
|---|---|
| `paper_trade.py` | 主脚本，每日跑一次 |
| `state.json` | 当前仓位状态（自动生成/更新） |
| `ledger.csv` | 每日决策账本，可用 Excel 打开复盘 |
| `backtest_spot.py` | 历史回测，预期 36.2% CAGR / -49% MaxDD |

## 启动 — 三步闭环

### 1. 单次手动跑一次，确认 OK

```bash
cd /Users/coco/btc-funding-arb
source .venv/bin/activate
python paper_trade.py
```

### 2. （可选）配 Telegram 推送

a. 找 [@BotFather](https://t.me/BotFather) 创建一个 bot → 拿 `BOT_TOKEN`
b. 给 bot 发一条消息 → 访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` → 拿 `chat_id`
c. 在 `~/.zshrc` 加：

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="123456789"
```

只在 BUY/SELL 信号触发时推送，平时不打扰。

### 3. 上 cron，每天 UTC 00:05 自动跑

```bash
crontab -e
# 加入这一行：
5 0 * * * cd /Users/coco/btc-funding-arb && /Users/coco/btc-funding-arb/.venv/bin/python paper_trade.py >> /Users/coco/btc-funding-arb/paper_trade.log 2>&1
```

## 30 天验收清单

| 检查项 | 通过标准 |
|---|---|
| 脚本每天跑通无报错 | `ledger.csv` 行数 = 已跑天数 |
| 信号合理 | BUY/SELL 出现时回查 K 线，确认是真突破 |
| 滑点假设 | 真实下单时挂限价，实际 fill 价 vs 信号价差距 < 5bps |
| 心理验证 | 看到 -10%~-15% 回撤时能不能扛住（必跌至少一次） |

## 现在的状态

```
日期:    2026-06-20
BTC:     $64,298
20d 高:  $74,275
20d 低:  $59,131
仓位:    flat（在等突破）
模拟本金: $100,000
```

> BTC 当前在 20 日区间内震荡，没信号。下次信号可能是：
> - 突破 $74,275 → BUY 入场
> - 跌破 $59,131 → 暂无意义（本来就空仓）

## 真实下单前的红线

1. **纸面单跑满 30 个交易日**，期间至少出现 1 次 BUY + 1 次 SELL
2. **用 1% 资金（$1,000）小额试水**一个月，确认 API、滑点、手续费实测匹配模型
3. **再上 10% 资金**跑一个季度，确认能扛 -15% 回撤
4. **永远不要全仓**：上限 30% 资金给单一策略
