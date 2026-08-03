"""Telegram setup helper.

Walks you through:
  1. Confirm you have a bot token (from @BotFather)
  2. Discover your chat_id via getUpdates
  3. Test send a message
  4. Save credentials to .env (which the daemons load)

Usage:
  python setup_telegram.py              # interactive
  python setup_telegram.py --token XXX  # auto-discover chat_id with given token
  python setup_telegram.py --test       # send a test message using existing .env
"""
import json
import os
import sys
import time
from pathlib import Path
import requests

ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line: continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("\"").strip("'")
    return env


def save_env(env):
    lines = ["# BTC Quant Terminal · auto-generated", ""]
    for k, v in env.items():
        lines.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(lines) + "\n")
    ENV_FILE.chmod(0o600)  # owner read/write only
    print(f"✅ Saved to {ENV_FILE} (chmod 600)")


def test_bot(token):
    """Verify bot token works."""
    r = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
    if not r.ok:
        return None
    j = r.json()
    if not j.get("ok"): return None
    return j["result"]


def discover_chat_id(token):
    """Call getUpdates to find chat_id from recent messages."""
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
    if not r.ok:
        return None
    j = r.json()
    if not j.get("ok") or not j.get("result"):
        return None
    # Most recent message's chat
    for upd in reversed(j["result"]):
        if "message" in upd and "chat" in upd["message"]:
            return upd["message"]["chat"]
    return None


def send_test(token, chat_id):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": "*✅ BTC Quant Terminal · Telegram Connected*\n\n"
                    "你将收到以下事件推送：\n"
                    "• Donchian 信号触发（BUY/SELL）\n"
                    "• Funding 突破 P75 入场窗口\n"
                    "• Basis 突破 P75\n"
                    "• Hunter 找到真实套利机会\n"
                    "• Daemon 错误警告\n\n"
                    "你的 chat_id 已保存。",
            "parse_mode": "Markdown",
        },
        timeout=10,
    )
    return r.ok, r.text


def main():
    args = sys.argv[1:]
    env = load_env()

    if "--test" in args:
        token = env.get("TELEGRAM_BOT_TOKEN")
        chat = env.get("TELEGRAM_CHAT_ID")
        if not token or not chat:
            print("❌ .env 缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
            sys.exit(1)
        ok, msg = send_test(token, chat)
        print(f"测试推送 {'✅' if ok else '❌'}: {msg[:200]}")
        return

    # Get token
    token = env.get("TELEGRAM_BOT_TOKEN")
    if "--token" in args:
        i = args.index("--token")
        if i + 1 < len(args):
            token = args[i + 1]

    if not token:
        print("=" * 60)
        print("第 1 步：在 Telegram 中找 @BotFather")
        print("=" * 60)
        print("  发送  /newbot")
        print("  填入 bot 名字（如 'My BTC Agent'）")
        print("  填入 username（必须以 _bot 结尾，如 my_btc_agent_bot）")
        print("  @BotFather 会返回你的 token，格式：")
        print("    123456789:ABCdef...XYZ")
        print()
        token = input("粘贴你的 bot token: ").strip()

    if not token:
        print("❌ 未提供 token")
        sys.exit(1)

    print("\n验证 token 中...")
    bot = test_bot(token)
    if not bot:
        print(f"❌ Token 无效，请检查后重试")
        sys.exit(1)
    print(f"✅ Bot 验证成功: @{bot['username']} ({bot['first_name']})")

    print("\n" + "=" * 60)
    print(f"第 2 步：用 Telegram 找到 @{bot['username']}")
    print("=" * 60)
    print("  点击 START 或发送任意一条消息（例如 'hi'）给这个 bot")
    print("  （这一步是为了让你的 chat 出现在 getUpdates 里）")
    print()
    input("发送消息后按 Enter 继续...")

    print("\n查询你的 chat_id 中...")
    chat = None
    for attempt in range(5):
        chat = discover_chat_id(token)
        if chat: break
        print(f"  尝试 {attempt+1}/5... 没找到消息。请确认你已给 bot 发过消息。")
        time.sleep(2)

    if not chat:
        print("❌ 未找到 chat_id。请确认你已给 bot 发过消息，然后重新跑此脚本。")
        sys.exit(1)

    print(f"✅ 找到 chat_id: {chat['id']} ({chat.get('first_name','')} {chat.get('last_name','')})")

    # Save
    env["TELEGRAM_BOT_TOKEN"] = token
    env["TELEGRAM_CHAT_ID"] = str(chat["id"])
    save_env(env)

    # Test
    print("\n第 3 步：发送测试消息...")
    ok, msg = send_test(token, str(chat["id"]))
    if ok:
        print("✅ 测试消息已发送 — 检查你的 Telegram")
    else:
        print(f"❌ 发送失败: {msg[:200]}")

    print("\n" + "=" * 60)
    print("完成！下一步：")
    print("=" * 60)
    print("  重新加载 launchd 以让 daemon 读取新 .env：")
    print("  launchctl unload ~/Library/LaunchAgents/com.coco.btc-super-agent.plist")
    print("  launchctl load   ~/Library/LaunchAgents/com.coco.btc-super-agent.plist")
    print("  launchctl unload ~/Library/LaunchAgents/com.coco.btc-donchian.plist")
    print("  launchctl load   ~/Library/LaunchAgents/com.coco.btc-donchian.plist")


if __name__ == "__main__":
    main()
