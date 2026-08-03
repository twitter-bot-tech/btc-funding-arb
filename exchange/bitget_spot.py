"""Minimal Bitget UTA spot REST client."""
from __future__ import annotations

import base64
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
class BitgetConfig:
    api_key: str
    api_secret: str
    passphrase: str
    base_url: str = "https://api.bitget.com"
    locale: str = "en-US"
    timeout: int = 15

    @classmethod
    def from_env(cls) -> "BitgetConfig":
        load_env()
        return cls(
            api_key=os.environ.get("BITGET_API_KEY", "").strip(),
            api_secret=os.environ.get("BITGET_API_SECRET", "").strip(),
            passphrase=os.environ.get("BITGET_API_PASSPHRASE", "").strip(),
            base_url=os.environ.get("BITGET_BASE_URL", "https://api.bitget.com").rstrip("/"),
            locale=os.environ.get("BITGET_LOCALE", "en-US").strip(),
        )


class BitgetSpotClient:
    def __init__(self, config: BitgetConfig | None = None) -> None:
        self.config = config or BitgetConfig.from_env()
        self.session = requests.Session()

    def _headers(self, method: str, request_path: str, body: str = "") -> dict[str, str]:
        if not self.config.api_key or not self.config.api_secret or not self.config.passphrase:
            raise RuntimeError("Missing BITGET_API_KEY / BITGET_API_SECRET / BITGET_API_PASSPHRASE")
        ts = str(int(time.time() * 1000))
        prehash = f"{ts}{method.upper()}{request_path}{body}"
        digest = hmac.new(
            self.config.api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return {
            "ACCESS-KEY": self.config.api_key,
            "ACCESS-SIGN": base64.b64encode(digest).decode("ascii"),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.config.passphrase,
            "Content-Type": "application/json",
            "locale": self.config.locale,
        }

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None, auth: bool = False) -> dict[str, Any]:
        method = method.upper()
        query = ""
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
        request_path = path + (f"?{query}" if query else "")
        body_text = compact_json(body or {}) if body is not None else ""
        headers = self._headers(method, request_path, body_text) if auth else {
            "Content-Type": "application/json",
            "locale": self.config.locale,
        }
        response = self.session.request(
            method,
            self.config.base_url + request_path,
            data=body_text if body is not None else None,
            headers=headers,
            timeout=self.config.timeout,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            snippet = response.text[:300] if response.text else "<empty body>"
            raise RuntimeError(
                f"Bitget non-JSON response HTTP {response.status_code} "
                f"for {method} {request_path}: {snippet}"
            ) from exc
        if response.status_code >= 400:
            raise RuntimeError(f"Bitget HTTP {response.status_code}: {payload}")
        if payload.get("code") not in (None, "00000"):
            raise RuntimeError(f"Bitget error {payload.get('code')}: {payload.get('msg')} {payload.get('data')}")
        return payload

    def ticker(self, symbol: str = "BTCUSDT") -> dict[str, Any]:
        payload = self._request("GET", "/api/v2/spot/market/tickers", params={"symbol": symbol})
        data = payload.get("data") or []
        return data[0] if isinstance(data, list) else data

    def account_info(self) -> dict[str, Any]:
        return self._request("GET", "/api/v3/account/info", auth=True)

    def assets(self, coin: str | None = None) -> dict[str, Any]:
        payload = self._request("GET", "/api/v3/account/assets", auth=True)
        if coin and isinstance(payload.get("data"), dict):
            assets = payload["data"].get("assets") or []
            payload["data"]["assets"] = [row for row in assets if row.get("coin") == coin.upper()]
        return payload

    def all_account_balance(self) -> dict[str, Any]:
        return self._request("GET", "/api/v2/account/all-account-balance", auth=True)

    def funding_assets(self, coin: str | None = None) -> dict[str, Any]:
        params = {}
        if coin:
            params["coin"] = coin
        return self._request("GET", "/api/v3/account/funding-assets", params=params, auth=True)

    def transfer(self, *, from_type: str, to_type: str, coin: str, amount: Decimal, client_oid: str) -> dict[str, Any]:
        body = {
            "fromType": from_type,
            "toType": to_type,
            "coin": coin,
            "amount": str(amount.normalize()),
            "clientOid": client_oid,
        }
        return self._request("POST", "/api/v3/account/transfer", body=body, auth=True)

    def max_open_available(self, *, symbol: str = "BTCUSDT", side: str = "buy", order_type: str = "limit", price: Decimal | None = None) -> dict[str, Any]:
        body = {
            "category": "SPOT",
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
        }
        if price is not None:
            body["price"] = str(price.normalize())
        return self._request("POST", "/api/v3/account/max-open-available", body=body, auth=True)

    def place_limit_order(self, *, symbol: str, side: str, size: Decimal, price: Decimal, client_oid: str) -> dict[str, Any]:
        body = {
            "category": "SPOT",
            "symbol": symbol,
            "side": side,
            "orderType": "limit",
            "timeInForce": "gtc",
            "price": str(price.normalize()),
            "qty": str(size.normalize()),
            "clientOid": client_oid,
            "reduceOnly": "no",
        }
        return self._request("POST", "/api/v3/trade/place-order", body=body, auth=True)

    def order_info(self, *, order_id: str | None = None, client_oid: str | None = None) -> dict[str, Any]:
        params = {}
        if order_id:
            params["orderId"] = order_id
        if client_oid:
            params["clientOid"] = client_oid
        params["category"] = "SPOT"
        return self._request("GET", "/api/v3/trade/order-info", params=params, auth=True)

    def open_orders(self, symbol: str = "BTCUSDT") -> dict[str, Any]:
        return self._request(
            "GET",
            "/api/v3/trade/unfilled-orders",
            params={"category": "SPOT", "symbol": symbol},
            auth=True,
        )

    def cancel_order(self, *, order_id: str | None = None, client_oid: str | None = None, symbol: str = "BTCUSDT") -> dict[str, Any]:
        body = {"category": "SPOT", "symbol": symbol}
        if order_id:
            body["orderId"] = order_id
        if client_oid:
            body["clientOid"] = client_oid
        if not order_id and not client_oid:
            raise ValueError("Provide order_id or client_oid")
        return self._request("POST", "/api/v3/trade/cancel-order", body=body, auth=True)

    def quote_to_base_size(self, *, quote_amount: Decimal, price: Decimal) -> Decimal:
        lot = Decimal(os.environ.get("BITGET_DEFAULT_LOT_SZ", "0.00000001"))
        min_size = Decimal(os.environ.get("BITGET_DEFAULT_MIN_SZ", "0.00001"))
        size = quantize_down(quote_amount / price, lot)
        if size < min_size:
            raise ValueError(f"Order size {size} is below min size {min_size}")
        return size
