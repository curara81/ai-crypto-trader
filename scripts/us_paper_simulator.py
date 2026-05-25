#!/usr/bin/env python3
"""v6.1: 미국 주식 자체 paper simulator.

KYC/API 키 없이 yfinance 기반 가상 매매. Alpaca 대체.

기능:
- $100,000 가상 자금 시작
- yfinance 실시간 시세 (15분 지연)
- 매수/매도 시뮬레이션 (slippage 0.1% 가산)
- 포지션/잔액 JSON 파일 저장 (재시작 안전)
- P&L 계산
- JSONL 거래 로그 (ML 학습용)
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import yfinance as yf
    _YF_OK = True
except ImportError:
    _YF_OK = False

logger = logging.getLogger(__name__)

STATE_DIR = Path(os.environ.get(
    "US_PAPER_STATE_DIR",
    os.path.expanduser("~/trading/data/us_paper")
))
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "trades.jsonl"

INITIAL_CASH = 100_000.0   # $100k 시작
SLIPPAGE_PCT = 0.001       # 0.1% slippage (매수 시 +0.1%, 매도 시 -0.1%)
COMMISSION = 0.0           # Alpaca paper는 무료라 가정


class USPaperSimulator:
    """미국 주식 가상 매매 시뮬레이터.

    상태:
      - cash: 가용 현금 USD
      - positions: {symbol: {qty, avg_cost, opened_at}}
      - history: 거래 이력 (JSONL은 별도)
    """

    def __init__(self):
        self._load_state()

    def _load_state(self):
        if STATE_FILE.exists():
            try:
                d = json.loads(STATE_FILE.read_text())
                self.cash = float(d.get("cash", INITIAL_CASH))
                self.positions = d.get("positions", {})
                self.created_at = d.get("created_at", datetime.now(timezone.utc).isoformat())
                logger.info(f"State loaded: cash=${self.cash:.2f}, positions={len(self.positions)}")
            except Exception as e:
                logger.warning(f"State 로드 실패, 초기화: {e}")
                self._init_fresh()
        else:
            self._init_fresh()

    def _init_fresh(self):
        self.cash = INITIAL_CASH
        self.positions = {}
        self.created_at = datetime.now(timezone.utc).isoformat()
        self._save_state()
        logger.info(f"신규 시뮬레이터 시작: ${INITIAL_CASH}")

    def _save_state(self):
        STATE_FILE.write_text(json.dumps({
            "cash": self.cash,
            "positions": self.positions,
            "created_at": self.created_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2, ensure_ascii=False))

    def _log_trade(self, event: dict):
        with LOG_FILE.open("a") as f:
            f.write(json.dumps({
                **event,
                "ts": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False) + "\n")

    def get_price(self, symbol: str) -> Optional[float]:
        """yfinance 실시간 시세 (15분 지연 가능)."""
        if not _YF_OK:
            return None
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="1d", interval="1m")
            if hist.empty:
                hist = t.history(period="5d", interval="1d")
                if hist.empty:
                    return None
            return float(hist["Close"].iloc[-1])
        except Exception as e:
            logger.warning(f"{symbol} price fetch 실패: {e}")
            return None

    def buy(self, symbol: str, dollar_amount: float, reason: str = "") -> dict:
        """매수: $X 어치 매수. dollar_amount는 USD."""
        symbol = symbol.upper()
        if dollar_amount > self.cash:
            return {"status": "rejected", "reason": f"insufficient cash (need ${dollar_amount:.2f}, have ${self.cash:.2f})"}
        if dollar_amount < 10:
            return {"status": "rejected", "reason": "minimum $10 per order"}

        price = self.get_price(symbol)
        if not price or price <= 0:
            return {"status": "rejected", "reason": f"price not available for {symbol}"}

        fill_price = price * (1 + SLIPPAGE_PCT)
        qty = dollar_amount / fill_price
        cost = qty * fill_price + COMMISSION

        existing = self.positions.get(symbol)
        if existing:
            new_qty = existing["qty"] + qty
            new_avg = (existing["qty"] * existing["avg_cost"] + qty * fill_price) / new_qty
            self.positions[symbol] = {
                "qty": new_qty,
                "avg_cost": new_avg,
                "opened_at": existing["opened_at"],
                "last_buy_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            self.positions[symbol] = {
                "qty": qty,
                "avg_cost": fill_price,
                "opened_at": datetime.now(timezone.utc).isoformat(),
            }

        self.cash -= cost
        self._save_state()
        event = {
            "action": "buy", "symbol": symbol, "qty": qty,
            "price": fill_price, "cost": cost, "cash_after": self.cash,
            "reason": reason,
        }
        self._log_trade(event)
        logger.info(f"BUY {symbol}: {qty:.4f} @ ${fill_price:.2f} = ${cost:.2f} (cash: ${self.cash:.2f})")
        return {"status": "filled", **event}

    def sell(self, symbol: str, qty: Optional[float] = None, reason: str = "") -> dict:
        """매도: qty=None이면 전량 매도."""
        symbol = symbol.upper()
        pos = self.positions.get(symbol)
        if not pos:
            return {"status": "rejected", "reason": f"no position in {symbol}"}

        if qty is None:
            qty = pos["qty"]
        if qty > pos["qty"]:
            return {"status": "rejected", "reason": f"insufficient qty (have {pos['qty']:.4f}, sell {qty:.4f})"}

        price = self.get_price(symbol)
        if not price or price <= 0:
            return {"status": "rejected", "reason": f"price not available for {symbol}"}

        fill_price = price * (1 - SLIPPAGE_PCT)
        proceeds = qty * fill_price - COMMISSION
        cost_basis = qty * pos["avg_cost"]
        pnl = proceeds - cost_basis
        pnl_pct = (pnl / cost_basis * 100) if cost_basis > 0 else 0

        if qty >= pos["qty"]:
            del self.positions[symbol]
        else:
            self.positions[symbol]["qty"] -= qty

        self.cash += proceeds
        self._save_state()
        event = {
            "action": "sell", "symbol": symbol, "qty": qty,
            "price": fill_price, "proceeds": proceeds,
            "pnl": pnl, "pnl_pct": pnl_pct,
            "cash_after": self.cash, "reason": reason,
        }
        self._log_trade(event)
        logger.info(f"SELL {symbol}: {qty:.4f} @ ${fill_price:.2f} = ${proceeds:.2f} (P&L: ${pnl:+.2f}, {pnl_pct:+.2f}%)")
        return {"status": "filled", **event}

    def portfolio_value(self) -> dict:
        """현재 포트폴리오 가치 (현금 + 포지션 시가)."""
        positions_value = 0.0
        position_details = []
        for sym, pos in self.positions.items():
            price = self.get_price(sym)
            if price is None:
                continue
            market_value = pos["qty"] * price
            pnl = market_value - (pos["qty"] * pos["avg_cost"])
            pnl_pct = (pnl / (pos["qty"] * pos["avg_cost"]) * 100) if pos["avg_cost"] > 0 else 0
            position_details.append({
                "symbol": sym, "qty": pos["qty"], "avg_cost": pos["avg_cost"],
                "current_price": price, "market_value": market_value,
                "pnl": pnl, "pnl_pct": pnl_pct,
            })
            positions_value += market_value

        total = self.cash + positions_value
        pnl_total = total - INITIAL_CASH
        pnl_total_pct = (pnl_total / INITIAL_CASH * 100)
        return {
            "cash": self.cash,
            "positions_value": positions_value,
            "total": total,
            "pnl_total": pnl_total,
            "pnl_total_pct": pnl_total_pct,
            "positions": position_details,
            "n_positions": len(self.positions),
            "initial": INITIAL_CASH,
        }


if __name__ == "__main__":
    import sys
    sim = USPaperSimulator()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        pv = sim.portfolio_value()
        print(f"Cash: ${pv['cash']:.2f}")
        print(f"Positions value: ${pv['positions_value']:.2f}")
        print(f"Total: ${pv['total']:.2f} (P&L: ${pv['pnl_total']:+.2f}, {pv['pnl_total_pct']:+.2f}%)")
        for p in pv["positions"]:
            print(f"  {p['symbol']}: {p['qty']:.2f} @ ${p['avg_cost']:.2f} → ${p['current_price']:.2f} ({p['pnl_pct']:+.2f}%)")
    elif cmd == "buy":
        sym = sys.argv[2]
        amt = float(sys.argv[3])
        print(json.dumps(sim.buy(sym, amt, reason="manual"), indent=2))
    elif cmd == "sell":
        sym = sys.argv[2]
        print(json.dumps(sim.sell(sym, reason="manual"), indent=2))
