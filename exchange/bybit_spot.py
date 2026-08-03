"""Minimal Bybit V5 spot REST client."""
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
class BybitConfig:
    api_key: str
    api_secret: str
    base_url: str
    recv_window: str = "5000"
    timeout: int = 15

    @classmethod
    def from_env(cls) -> "BybitConfig":
        load_env()
        testnet = os.environ.get("BYBIT_TESTNET", "1").strip() != "0"
        default_url = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"
        return cls(
            api_key=os.environ.get("BYBIT_API_KEY", "").strip(),
            api_secret=os.environ.get("BYBIT_API_SECRET", "").strip(),
            base_url=os.environ.get("BYBIT_BASE_URL", default_url).rstrip("/"),
            recv_window=os.environ.get("BYBIT_RECV_WINDOW", "5000").strip(),
        )


class BybitSpotClient:
    def __init__(self, config: BybitConfig | None = None) -> None:
        self.config = config or BybitConfig.from_env()
        self.session = requests.Session()

    def _headers(self, payload_text: str) -> dict[str, str]:
        if not self.config.api_key or not self.config.api_secret:
            raise RuntimeError("Missing BYBIT_API_KEY / BYBIT_API_SECRET")
        ts = str(int(time.time() * 1000))
        raw = ts + self.config.api_key + self.config.recv_window + payload_text
        signature = hmac.new(
            self.config.api_secret.encode("utf-8"),
            raw.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-BAPI-API-KEY": self.config.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": self.config.recv_window,
            "X-BAPI-SIGN": signature,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None, auth: bool = False) -> dict[str, Any]:
        method = method.upper()
        query = ""
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
        payload_text = compact_json(body or {}) if body is not None else query
        headers = self._headers(payload_text) if auth else {"Content-Type": "application/json"}
        url = self.config.base_url + path + (f"?{query}" if query and method == "GET" else "")
        response = self.session.request(
            method,
            url,
            data=payload_text if body is not None else None,
            headers=headers,
            timeout=self.config.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") not in (None, 0):
            raise RuntimeError(f"Bybit error {payload.get('retCode')}: {payload.get('retMsg')} {payload.get('result')}")
        return payload

    def ticker(self, symbol: str = "BTCUSDT") -> dict[str, Any]:
        payload = self._request("GET", "/v5/market/tickers", params={"category": "spot", "symbol": symbol})
        return payload["result"]["list"][0]

    def wallet_balance(self, account_type: str = "UNIFIED", coin: str | None = None) -> dict[str, Any]:
        params = {"accountType": account_type}
        if coin:
            params["coin"] = coin
        return self._request("GET", "/v5/account/wallet-balance", params=params, auth=True)

    def place_limit_order(self, *, symbol: str, side: str, qty: Decimal, price: Decimal, order_link_id: str) -> dict[str, Any]:
        body = {
            "category": "spot",
            "symbol": symbol,
            "side": side.title(),
            "orderType": "Limit",
            "qty": str(qty.normalize()),
            "price": str(price.normalize()),
            "timeInForce": "GTC",
            "orderLinkId": order_link_id,
            "isLeverage": 0,
            "orderFilter": "Order",
        }
        return self._request("POST", "/v5/order/create", body=body, auth=True)

    def quote_to_base_size(self, *, quote_amount: Decimal, price: Decimal) -> Decimal:
        lot = Decimal(os.environ.get("BYBIT_DEFAULT_LOT_SZ", "0.000001"))
        min_size = Decimal(os.environ.get("BYBIT_DEFAULT_MIN_SZ", "0.00001"))
        size = quantize_down(quote_amount / price, lot)
        if size < min_size:
            raise ValueError(f"Order size {size} is below min size {min_size}")
        return size
