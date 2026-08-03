# Telegram 推送 · 3 分钟落地

## 一步配置

```bash
cd /Users/coco/btc-funding-arb
source .venv/bin/activate
python setup_telegram.py
```

脚本会引导你：
1. 创建 bot（提示你联系 @BotFather → `/newbot` → 拿 token）
2. 自动发现你的 chat_id（让你给 bot 发一条任意消息后自动抓取）
3. 写入 `.env`（chmod 600，仅自己可读）
4. 发测试消息验证

## 推送触发器（5 类事件）

| 事件 | 触发条件 | 优先级 |
|---|---|---|
| 🟢/🔴 **Donchian 信号变化** | BUY/SELL/HOLD 切换 | 🔥 最高 |
| 🟢 **Funding 突破 P75** | 7d APY 进入入场窗口 | 高 |
| 🟢 **Basis 突破 P75** | 年化基差进入上四分位 | 高 |
| 🟡 **接近 Donchian BUY** | 距 BUY 触发 < 3% | 中 |
| ⚡ **组合大幅调仓** | 资金分配变动 ≥ 20% | 中 |
| 🎯 **Hunter 套利机会** | 真实净盈利 ≥ $5 | 高 |

## 频率控制

- super_agent 每天 4 次（08:10/12:10/18:10/22:10）→ 只在事件触发才推
- hunter 每 60 秒扫一次 → 只在找到 ≥$5 机会才推
- **预计每天 0-2 条**（市场无机会时安静）

## 重新加载（设完 .env 之后必跑）

```bash
launchctl unload ~/Library/LaunchAgents/com.coco.btc-super-agent.plist
launchctl load   ~/Library/LaunchAgents/com.coco.btc-super-agent.plist
launchctl unload ~/Library/LaunchAgents/com.coco.btc-donchian.plist
launchctl load   ~/Library/LaunchAgents/com.coco.btc-donchian.plist
launchctl unload ~/Library/LaunchAgents/com.coco.btc-hunter.plist
launchctl load   ~/Library/LaunchAgents/com.coco.btc-hunter.plist
```

## 验证（推完一条测试消息）

```bash
python setup_telegram.py --test
```

## 取消推送

删除 `.env` 文件即可（daemon 自动跳过推送但继续记录）。
