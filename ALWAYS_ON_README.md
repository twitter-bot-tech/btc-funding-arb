# 常驻运行方案

当前默认是观察模式，只读取 Bitget 数据、刷新看板，不会下单。

## 本机开机自启

安装：

```bash
cd /Users/coco/btc-funding-arb
./install_macos_autostart.sh
```

检查健康状态：

```bash
/Users/coco/btc-funding-arb/.venv/bin/python /Users/coco/btc-funding-arb/check_bitget_observer_health.py
```

查看日志：

```bash
tail -f /Users/coco/btc-funding-arb/data/bitget_observer_board.log
```

卸载自启：

```bash
cd /Users/coco/btc-funding-arb
./uninstall_macos_autostart.sh
```

说明：macOS LaunchAgent 会在你登录电脑后启动。电脑关机、睡眠、断网时仍然不会运行。

## VPS / 云服务器

建议目录：

```bash
/opt/btc-funding-arb
```

安装 systemd 服务：

```bash
sudo cp /opt/btc-funding-arb/deploy/bitget-observer-board.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bitget-observer-board
sudo systemctl start bitget-observer-board
```

检查：

```bash
sudo systemctl status bitget-observer-board
journalctl -u bitget-observer-board -f
```

## 安全开关

保持 `.env` 里：

```bash
BITGET_ALLOW_LIVE=0
```

这表示观察模式和策略执行器不会发送真实策略订单。

只有做人工确认的小额实盘测试时，才临时使用：

```bash
BITGET_ALLOW_LIVE=1
```

并且还需要命令行确认字符串：

```bash
--live --confirm I_UNDERSTAND_LIVE_BITGET_ORDER
```

## 当前刷新频率

- 浏览器 BTC 现价：1 秒
- Bitget 观察状态：60 秒
- 页面自动 reload：60 秒
- 15m K 线和策略参数回测：仍由 `refresh_strategy_board.py` 单独按 15m / 1h 刷新

## 推荐上线顺序

1. 本机观察模式跑 24 小时。
2. 检查健康状态、日志、看板时间戳是否稳定。
3. 迁移到 VPS，继续观察 3-7 天。
4. 再做半自动：触发信号后只发通知，不自动下单。
5. 最后才考虑小金额自动下单。
