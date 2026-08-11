# VPS 迁移步骤

目标：让 Bitget 观察模式、策略数据刷新、纸交易状态在 VPS 上 24 小时运行，并通过浏览器访问看板。

当前仍然是观察模式：

- 不自动下单
- 不发送策略订单
- `.env` 必须保持 `BITGET_ALLOW_LIVE=0`

## 1. 准备 VPS

建议：

- Ubuntu 22.04 或 24.04
- 1 核 1G 以上即可
- 开放 TCP 22 和 80
- 系统时区可以保持 UTC，不影响策略，策略内部按 SGT 显示

拿到 VPS 后，先确认公网出口 IP：

```bash
curl https://api.ipify.org
```

把这个 IP 加到 Bitget API 的可信 IP 里。

## 2. 从本机上传代码

在本机运行：

```bash
cd /Users/coco/btc-funding-arb
./deploy_to_vps.sh root@你的VPS_IP /opt/btc-funding-arb
```

这个命令不会上传 `.env`，避免 API 密钥误传。

## 3. 在 VPS 上安装服务

SSH 登录 VPS：

```bash
ssh root@你的VPS_IP
cd /opt/btc-funding-arb
sudo bash deploy/setup_vps_ubuntu.sh
```

安装脚本会：

- 安装 Python / venv / nginx / rsync
- 创建 `btcbot` 系统用户
- 安装 Python 依赖
- 安装 4 个 systemd 常驻服务
- 安装 Nginx 看板服务

常驻服务：

- `strategy-refresh`：每 15 分钟拉 Bitget BTC 15m K 线，1 小时跑一次回测
- `bitget-observer-board`：每 60 秒刷新观察状态和看板
- `paper-trader`：每 15 分钟刷新纸交易状态
- `bitget-live-5u-test`：每 15 分钟检查一次真实 5U 测试条件，满足信号才发小额现货单

## 4. 在 VPS 上填写 .env

```bash
sudo nano /opt/btc-funding-arb/.env
sudo chmod 600 /opt/btc-funding-arb/.env
sudo chown btcbot:btcbot /opt/btc-funding-arb/.env
```

至少需要：

```bash
BITGET_API_KEY=
BITGET_API_SECRET=
BITGET_API_PASSPHRASE=
BITGET_BASE_URL=https://api.bitget.com
BITGET_ALLOW_LIVE=0
```

## 5. 重启服务

```bash
sudo systemctl restart strategy-refresh bitget-observer-board paper-trader bitget-live-5u-test
sudo systemctl status strategy-refresh bitget-observer-board paper-trader bitget-live-5u-test
```

查看日志：

```bash
journalctl -u strategy-refresh -f
journalctl -u bitget-observer-board -f
journalctl -u paper-trader -f
journalctl -u bitget-live-5u-test -f
```

健康检查：

```bash
/opt/btc-funding-arb/.venv/bin/python /opt/btc-funding-arb/check_vps_config.py
/opt/btc-funding-arb/.venv/bin/python /opt/btc-funding-arb/check_bitget_observer_health.py
```

## 6. 打开看板

浏览器访问：

```text
http://你的VPS_IP/
```

看板顶部会 60 秒自动刷新，BTC 现价每 1 秒刷新。

## 7. 停止/启动

停止：

```bash
sudo systemctl stop strategy-refresh bitget-observer-board paper-trader bitget-live-5u-test
```

启动：

```bash
sudo systemctl start strategy-refresh bitget-observer-board paper-trader bitget-live-5u-test
```

开机自启：

```bash
sudo systemctl enable strategy-refresh bitget-observer-board paper-trader bitget-live-5u-test
```

取消开机自启：

```bash
sudo systemctl disable strategy-refresh bitget-observer-board paper-trader bitget-live-5u-test
```

## 重要提醒

迁移后先观察 3-7 天。确认日志、信号、账户余额、看板时间都稳定后，再考虑半自动通知。不要直接打开自动实盘。
