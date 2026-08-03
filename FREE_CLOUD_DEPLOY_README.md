# 免费云端测试部署

这个方案使用 GitHub Actions + GitHub Pages 免费跑测试数据。

限制：

- 不是 24 小时常驻服务器。
- 定时刷新建议 15 分钟一次，不适合 60 秒级策略执行。
- 只跑公开测试模式 `PUBLIC_TEST`，不读取 Bitget 私有账户，不下单。
- 页面里的 BTC 实时价仍然由浏览器每 1 秒读取 Bitget 公共 ticker。

## 1. 创建 GitHub 仓库

在 GitHub 创建一个新仓库，例如：

```text
btc-funding-arb
```

## 2. 本地初始化并推送

```bash
cd /Users/coco/btc-funding-arb
git init
git add .
git commit -m "Add free observer pages deployment"
git branch -M main
git remote add origin git@github.com:你的用户名/btc-funding-arb.git
git push -u origin main
```

如果你用 HTTPS：

```bash
git remote add origin https://github.com/你的用户名/btc-funding-arb.git
git push -u origin main
```

## 3. 开启 GitHub Pages

进入 GitHub 仓库：

```text
Settings -> Pages -> Build and deployment -> Source -> GitHub Actions
```

## 4. 手动跑一次

进入：

```text
Actions -> Free Observer Pages -> Run workflow
```

跑完后，页面地址通常是：

```text
https://你的用户名.github.io/btc-funding-arb/
```

## 5. 刷新频率

`.github/workflows/free-observer-pages.yml` 当前设置为：

```yaml
schedule:
  - cron: "*/15 * * * *"
```

也就是每 15 分钟更新一次测试数据并重新发布页面。

## 6. 安全说明

不要把 `.env` 提交到 GitHub。当前 `.gitignore` 已忽略 `.env`。

免费云端测试模式不需要：

- `BITGET_API_KEY`
- `BITGET_API_SECRET`
- `BITGET_API_PASSPHRASE`

后续如果要读私有账户，也应该用 GitHub Secrets，而不是提交 `.env`。
