"""Health check for Bitget observe-only board refresh."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).parent
HEALTH = ROOT / "data" / "bitget_observer_health.json"
STATE = ROOT / "data" / "bitget_observer_state.json"


def load_json(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"missing {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-age-sec", type=int, default=180)
    args = parser.parse_args()

    health = load_json(HEALTH)
    state = load_json(STATE)
    age = time.time() - float(state.get("ts", 0))
    if health.get("status") != "ok":
        raise RuntimeError(f"observer unhealthy: {health}")
    if age > args.max_age_sec:
        raise RuntimeError(f"observer state stale: age={age:.0f}s max={args.max_age_sec}s")
    print(json.dumps({
        "status": "ok",
        "state_age_sec": round(age, 1),
        "action": state.get("action"),
        "live_price": state.get("live_price"),
        "last_ok_at": health.get("last_ok_at"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"health_check_failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
