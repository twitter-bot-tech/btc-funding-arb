"""Minimal OKX spot REST client.

Private requests follow OKX REST authentication:
signature = base64(hmac_sha256(secret, timestamp + method + path + body)).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
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


def utc_iso_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def compact_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass
class OkxConfig:
    api_key: str
    api_secret: str
    passphrase: str
    base_url: str = "https://www.okx.com"
    simulated: bool = True
    timeout: int = 15

    @classmethod
    def from_env(cls) -> "OkxConfig":
        load_env()
        return cls(
            api_key=os.environ.get("OKX_API_KEY", "").strip(),
            api_secret=os.environ.get("OKX_API_SECRET", "").strip(),
            passphrase=os.environ.get("OKX_API_PASSPHRASE", "").strip(),
            base_url=os.environ.get("OKX_BASE_URL", "https://www.okx.com").rstrip("/"),
            simulated=os.environ.get("OKX_SIMULATED", "1").strip() != "0",
        )


class OkxSpotClient:
    def __init__(self, config: OkxConfig | None = None) -> None:
        self.config = config or OkxConfig.from_env()
        self.session = requests.Session()

    def _headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        if not self.config.api_key or not self.config.api_secret or not self.config.passphrase:
            raise RuntimeError("Missing OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE")
        ts = utc_iso_ms()
        prehash = f"{ts}{method.upper()}{path}{body}"
        digest = hmac.new(
            self.config.api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        headers = {
            "OK-ACCESS-KEY": self.config.api_key,
            "OK-ACCESS-SIGN": base64.b64encode(digest).decode("ascii"),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.config.passphrase,
            "Content-Type": "application/json",
        }
        if self.config.simulated:
            headers["x-simulated-trading"] = "1"
        return headers

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None, auth: bool = False) -> dict[str, Any]:
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        request_path = path + query
        body_text = compact_json(body or {}) if body is not None else ""
        headers = self._headers(method, request_path, body_text) if auth else {"Content-Type": "application/json"}
        if self.config.simulated and not auth:
            headers["x-simulated-trading"] = "1"
        response = self.session.request(
            method,
            self.config.base_url + request_path,
            data=body_text if body is not None else None,
            headers=headers,
            timeout=self.config.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (None, "0"):
            raise RuntimeError(f"OKX error {payload.get('code')}: {payload.get('msg')} {payload.get('data')}")
        return payload

    def ticker(self, inst_id: str = "BTC-USDT") -> dict[str, Any]:
        payload = self._request("GET", "/api/v5/market/ticker", params={"instId": inst_id})
        return payload["data"][0]

    def instrument(self, inst_id: str = "BTC-USDT") -> dict[str, Any]:
        payload = self._request("GET", "/api/v5/public/instruments", params={"instType": "SPOT", "instId": inst_id})
        return payload["data"][0]

    def balance(self, ccy: str | None = None) -> dict[str, Any]:
        params = {"ccy": ccy} if ccy else None
        return self._request("GET", "/api/v5/account/balance", params=params, auth=True)

    def place_limit_order(self, *, inst_id: str, side: str, size: Decimal, price: Decimal, client_order_id: str) -> dict[str, Any]:
        body = {
            "instId": inst_id,
            "tdMode": "cash",
            "side": side,
            "ordType": "limit",
            "sz": str(size.normalize()),
            "px": str(price.normalize()),
            "clOrdId": client_order_id,
        }
        return self._request("POST", "/api/v5/trade/order", body=body, auth=True)

    def cancel_order(self, *, inst_id: str, order_id: str | None = None, client_order_id: str | None = None) -> dict[str, Any]:
        body = {"instId": inst_id}
        if order_id:
            body["ordId"] = order_id
        if client_order_id:
            body["clOrdId"] = client_order_id
        return self._request("POST", "/api/v5/trade/cancel-order", body=body, auth=True)

    def order(self, *, inst_id: str, order_id: str | None = None, client_order_id: str | None = None) -> dict[str, Any]:
        params = {"instId": inst_id}
        if order_id:
            params["ordId"] = order_id
        if client_order_id:
            params["clOrdId"] = client_order_id
        return self._request("GET", "/api/v5/trade/order", params=params, auth=True)

    def quote_to_base_size(self, *, inst_id: str, quote_amount: Decimal, price: Decimal, use_default_instrument: bool = False) -> Decimal:
        if use_default_instrument:
            lot = Decimal(os.environ.get("OKX_DEFAULT_LOT_SZ", "0.00000001"))
            min_size = Decimal(os.environ.get("OKX_DEFAULT_MIN_SZ", "0.00001"))
        else:
            inst = self.instrument(inst_id)
            lot = Decimal(inst.get("lotSz", "0.00000001"))
            min_size = Decimal(inst.get("minSz", "0"))
        size = quantize_down(quote_amount / price, lot)
        if size < min_size:
            raise ValueError(f"Order size {size} is below OKX minSz {min_size}")
        return size
