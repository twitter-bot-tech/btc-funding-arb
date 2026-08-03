"""Minimal Gate API v4 spot REST client."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def compact_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass
class GateConfig:
    api_key: str
    api_secret: str
    base_url: str = "https://api.gateio.ws"
    prefix: str = "/api/v4"
    timeout: int = 15

    @classmethod
    def from_env(cls) -> "GateConfig":
        load_env()
        return cls(
            api_key=os.environ.get("GATE_API_KEY", "").strip(),
            api_secret=os.environ.get("GATE_API_SECRET", "").strip(),
            base_url=os.environ.get("GATE_BASE_URL", "https://api.gateio.ws").rstrip("/"),
        )


class GateSpotClient:
    def __init__(self, config: GateConfig | None = None) -> None:
        self.config = config or GateConfig.from_env()
        self.session = requests.Session()

    def _headers(self, method: str, path: str, query: str = "", body: str = "") -> dict[str, str]:
        if not self.config.api_key or not self.config.api_secret:
            raise RuntimeError("Missing GATE_API_KEY / GATE_API_SECRET")
        timestamp = str(int(time.time()))
        body_hash = hashlib.sha512(body.encode("utf-8")).hexdigest()
        sign_string = "\n".join([method.upper(), self.config.prefix + path, query, body_hash, timestamp])
        signature = hmac.new(
            self.config.api_secret.encode("utf-8"),
            sign_string.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "KEY": self.config.api_key,
            "Timestamp": timestamp,
            "SIGN": signature,
        }

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None, auth: bool = False) -> Any:
        method = method.upper()
        query = ""
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
        body_text = compact_json(body or {}) if body is not None else ""
        headers = self._headers(method, path, query, body_text) if auth else {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        url = self.config.base_url + self.config.prefix + path + (f"?{query}" if query else "")
        response = self.session.request(
            method,
            url,
            headers=headers,
            data=body_text if body is not None else None,
            timeout=self.config.timeout,
        )
        response.raise_for_status()
        return response.json()

    def ticker(self, currency_pair: str = "BTC_USDT") -> dict[str, Any]:
        payload = self._request("GET", "/spot/tickers", params={"currency_pair": currency_pair})
        return payload[0]

    def balances(self, currency: str | None = None) -> list[dict[str, Any]]:
        payload = self._request("GET", "/spot/accounts", auth=True)
        if currency:
            return [row for row in payload if row.get("currency") == currency]
        return payload

    def place_limit_order(self, *, currency_pair: str, side: str, amount: Decimal, price: Decimal, text: str) -> dict[str, Any]:
        body = {
            "text": text,
            "currency_pair": currency_pair,
            "type": "limit",
            "account": "spot",
            "side": side,
            "amount": str(amount.normalize()),
            "price": str(price.normalize()),
            "time_in_force": "gtc",
            "auto_borrow": False,
        }
        return self._request("POST", "/spot/orders", body=body, auth=True)

    def quote_to_base_size(self, *, quote_amount: Decimal, price: Decimal) -> Decimal:
        lot = Decimal(os.environ.get("GATE_DEFAULT_LOT_SZ", "0.00000001"))
        min_size = Decimal(os.environ.get("GATE_DEFAULT_MIN_SZ", "0.00001"))
        size = quantize_down(quote_amount / price, lot)
        if size < min_size:
            raise ValueError(f"Order size {size} is below min size {min_size}")
        return size
