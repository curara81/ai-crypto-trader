#!/usr/bin/env python3
"""v6.0: Alpaca paper trading API 래퍼.

paper 모드만 사용. 실거래 키 절대 금지.
"""
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Paper trading base URL (실거래 URL은 사용 금지)
BASE_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

try:
    from secrets_helper import get_secret as _get_secret
except ImportError:
    def _get_secret(key: str) -> Optional[str]:
        return os.environ.get(key)


class AlpacaPaperClient:
    """Alpaca paper trading 클라이언트 — 안전을 위해 paper URL hardcoded."""

    def __init__(self):
        self.api_key = _get_secret("ALPACA_API_KEY") or _get_secret("APCA_API_KEY_ID")
        self.api_secret = _get_secret("ALPACA_API_SECRET") or _get_secret("APCA_API_SECRET_KEY")
        if not self.api_key or not self.api_secret:
            raise RuntimeError(
                "Alpaca paper API key 없음. "
                "Keychain에 ALPACA_API_KEY + ALPACA_API_SECRET 저장 필요"
            )
        self._headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }

    # ─── 계정 정보 ─────────────────────
    def get_account(self) -> dict:
        """현금/주식가치/포지션 수."""
        r = requests.get(f"{BASE_URL}/v2/account", headers=self._headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def is_market_open(self) -> bool:
        """미국 시장 개장 여부."""
        try:
            r = requests.get(f"{BASE_URL}/v2/clock", headers=self._headers, timeout=5)
            return r.json().get("is_open", False)
        except Exception as e:
            logger.warning(f"Alpaca clock 실패: {e}")
            return False

    # ─── 포지션 ────────────────────────
    def get_positions(self) -> list[dict]:
        r = requests.get(f"{BASE_URL}/v2/positions", headers=self._headers, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_position(self, symbol: str) -> Optional[dict]:
        try:
            r = requests.get(f"{BASE_URL}/v2/positions/{symbol}",
                             headers=self._headers, timeout=10)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    # ─── 주문 ──────────────────────────
    def submit_order(self, symbol: str, qty: float, side: str,
                     order_type: str = "market", time_in_force: str = "day") -> dict:
        """매수/매도 주문 (paper). side: buy/sell."""
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        r = requests.post(f"{BASE_URL}/v2/orders", json=payload,
                          headers=self._headers, timeout=15)
        r.raise_for_status()
        order = r.json()
        logger.info(f"[ALPACA-PAPER] {side.upper()} {symbol} qty={qty} → {order.get('id')[:8]}")
        return order

    def buy_dollars(self, symbol: str, dollars: float) -> dict:
        """USD notional 매수 (qty 자동 계산). $1 단위 가능."""
        payload = {
            "symbol": symbol,
            "notional": str(dollars),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        }
        r = requests.post(f"{BASE_URL}/v2/orders", json=payload,
                          headers=self._headers, timeout=15)
        r.raise_for_status()
        order = r.json()
        logger.info(f"[ALPACA-PAPER] BUY ${dollars} of {symbol} → {order.get('id', '?')[:8]}")
        return order

    def sell_all(self, symbol: str) -> Optional[dict]:
        """보유 전량 매도."""
        pos = self.get_position(symbol)
        if not pos:
            return None
        qty = float(pos["qty"])
        if qty <= 0:
            return None
        return self.submit_order(symbol, qty, "sell")

    # ─── 시세 (Alpaca 데이터 무료 등급) ───
    def get_latest_quote(self, symbol: str) -> Optional[dict]:
        try:
            r = requests.get(
                f"{DATA_URL}/v2/stocks/{symbol}/quotes/latest",
                headers=self._headers, timeout=5,
            )
            return r.json().get("quote")
        except Exception:
            return None


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO)
    c = AlpacaPaperClient()
    print(json.dumps(c.get_account(), indent=2))
    print("market open:", c.is_market_open())
    print("positions:", json.dumps(c.get_positions(), indent=2))
