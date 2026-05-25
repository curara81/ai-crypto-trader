#!/usr/bin/env python3
"""v6.1: 미국 주식 paper trading 봇 (자체 simulator + Gemini).

매 사이클:
1. 시장 시간 체크 (한국시간 22:30~05:00 = US 09:30~16:00 ET, 평일만)
2. KILLSWITCH 파일 체크 (있으면 즉시 중단)
3. 후보 종목 분석 (top 10 from STOCK_UNIVERSE)
4. Gemini로 각 종목별 매수/매도/관망 결정
5. 매수 결정 + 신뢰도 0.7+ → simulator.buy()
6. 보유 종목 손절/익절 체크
7. 포트폴리오 상태 로깅

실행: cron 또는 launchd 30분 주기
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# scripts 경로
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("us_paper_bot")

from us_paper_simulator import USPaperSimulator, INITIAL_CASH

try:
    from llm_router import LLMRouter
    _LLM = LLMRouter()
except Exception as e:
    _LLM = None
    logger.error(f"LLMRouter 로드 실패: {e}")

try:
    import yfinance as yf
    _YF_OK = True
except ImportError:
    _YF_OK = False

# ─── 설정 ───────────────────────────────────────
TRADING_ROOT = Path(os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading")))
KILLSWITCH = TRADING_ROOT / "KILLSWITCH"

UNIVERSE = [
    # 빅테크 + ETF
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AMD",
    "AVGO", "ORCL", "NFLX", "QCOM", "INTC",
    # ETF (시장 추종)
    "SPY", "QQQ", "VOO", "IWM",
    # 핫픽
    "PLTR", "COIN", "MSTR", "RBLX", "SNOW", "NET",
    # 안정주
    "JPM", "V", "WMT", "JNJ", "LLY",
]

MAX_POSITIONS = 5             # 동시 보유 5개
POSITION_SIZE_USD = 10_000    # 종목당 $10,000 (≈ 10%)
MIN_CONFIDENCE = 0.7          # Gemini 신뢰도 임계
STOP_LOSS_PCT = -5.0          # -5% 손절
TAKE_PROFIT_PCT = 8.0         # +8% 익절
MAX_HOLD_DAYS = 7             # 최대 보유 기간


def is_us_market_open() -> bool:
    """미국 NYSE 시장 시간 체크 (한국시간 기준).
    Summer (DST): 22:30~05:00 KST → ET 09:30~16:00
    Winter:      23:30~06:00 KST → ET 09:30~16:00
    평일만 (월~금)
    """
    now_utc = datetime.now(timezone.utc)
    # NYSE ET (대략 UTC-4 또는 -5)
    # 간단히 ET 시간으로 환산: UTC-5 (DST 무시 conservatively)
    et = now_utc - timedelta(hours=5)
    # 평일 + 9:30-16:00
    if et.weekday() >= 5:  # Sat=5, Sun=6
        return False
    if et.hour < 9 or et.hour >= 16:
        return False
    if et.hour == 9 and et.minute < 30:
        return False
    return True


def check_killswitch() -> bool:
    if KILLSWITCH.exists():
        logger.warning(f"⚠️ KILLSWITCH 활성 ({KILLSWITCH}) — 매매 중단")
        return True
    return False


def analyze_and_decide(symbol: str, sim: USPaperSimulator) -> dict:
    """Gemini로 매수/매도/관망 결정.

    Returns: {"action": "buy"|"sell"|"hold", "confidence": 0-1, "reason": "..."}
    """
    if not _LLM or not _YF_OK:
        return {"action": "hold", "confidence": 0, "reason": "LLM/yfinance 미가용"}
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="1mo", interval="1d")
        if hist.empty or len(hist) < 5:
            return {"action": "hold", "confidence": 0, "reason": "데이터 부족"}
        price = float(hist["Close"].iloc[-1])
        change_1d = (price / hist["Close"].iloc[-2] - 1) * 100 if len(hist) >= 2 else 0
        change_5d = (price / hist["Close"].iloc[-6] - 1) * 100 if len(hist) >= 6 else 0
        change_30d = (price / hist["Close"].iloc[0] - 1) * 100
        high_30d = float(hist["High"].iloc[-22:].max()) if len(hist) >= 22 else price
        low_30d = float(hist["Low"].iloc[-22:].min()) if len(hist) >= 22 else price
        position_pct = (price - low_30d) / (high_30d - low_30d) * 100 if high_30d > low_30d else 50
        vol_24h = float(hist["Volume"].iloc[-1])
        vol_avg = float(hist["Volume"].iloc[-22:].mean()) if len(hist) >= 22 else vol_24h
        vol_ratio = vol_24h / vol_avg if vol_avg > 0 else 1

        # 보유 여부 + P&L
        pos = sim.positions.get(symbol)
        pos_info = ""
        if pos:
            pnl = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
            opened_at = pos.get("opened_at", "")
            pos_info = f"\n## 보유 중\nQty: {pos['qty']:.2f}, Avg cost: ${pos['avg_cost']:.2f}, P&L: {pnl:+.2f}%, opened: {opened_at[:10]}"

        prompt = f"""You are a senior US equity trader making a quick trade decision for paper trading.

## {symbol}
Price: ${price:.2f}
Change: 1d {change_1d:+.2f}%, 5d {change_5d:+.2f}%, 30d {change_30d:+.2f}%
30d range: ${low_30d:.2f} ~ ${high_30d:.2f} (currently at {position_pct:.0f}% of range)
Volume ratio (24h/30d avg): {vol_ratio:.2f}x
{pos_info}

## Task
Decide: buy / sell / hold for this symbol RIGHT NOW (next ~24 hours).
- buy: enter new position (only if not already holding, or scaling in)
- sell: exit existing position (only if holding)
- hold: no action

Constraints:
- Paper trading, so be willing to act (we want data for ML training)
- Min confidence for buy: 0.6+ with clear catalyst
- For sell: trigger if -5% loss OR +8% profit OR clear bearish reversal
- Position size: ${POSITION_SIZE_USD} per trade

Respond JSON:
{{"action": "buy"|"sell"|"hold", "confidence": 0.0-1.0, "reason": "<short 20-30 word analysis>", "expected_move_pct": <number>, "risk_level": "low"|"medium"|"high"}}"""

        result = _LLM.call(prompt, model="gemini-2.5-flash", timeout=30)
        decision = result["json"]
        action = decision.get("action", "hold").lower()
        if action not in ("buy", "sell", "hold"):
            action = "hold"
        return {
            "action": action,
            "confidence": float(decision.get("confidence", 0)),
            "reason": decision.get("reason", ""),
            "expected_move": decision.get("expected_move_pct", 0),
            "risk_level": decision.get("risk_level", "medium"),
            "current_price": price,
        }
    except Exception as e:
        logger.warning(f"{symbol} decide 실패: {e}")
        return {"action": "hold", "confidence": 0, "reason": f"error: {str(e)[:80]}"}


def check_exit_signals(sim: USPaperSimulator) -> int:
    """보유 종목 손절/익절/시간만료 체크. 매도 건수 반환."""
    n_sold = 0
    for sym in list(sim.positions.keys()):
        pos = sim.positions[sym]
        price = sim.get_price(sym)
        if not price:
            continue
        pnl_pct = (price - pos["avg_cost"]) / pos["avg_cost"] * 100
        # 시간 체크
        opened = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00"))
        held_days = (datetime.now(timezone.utc) - opened).days

        sell_reason = None
        if pnl_pct <= STOP_LOSS_PCT:
            sell_reason = f"손절 ({pnl_pct:+.2f}%)"
        elif pnl_pct >= TAKE_PROFIT_PCT:
            sell_reason = f"익절 ({pnl_pct:+.2f}%)"
        elif held_days >= MAX_HOLD_DAYS:
            sell_reason = f"시간만료 ({held_days}d, P&L: {pnl_pct:+.2f}%)"

        if sell_reason:
            r = sim.sell(sym, reason=sell_reason)
            if r.get("status") == "filled":
                n_sold += 1
                logger.info(f"  [EXIT] {sym}: {sell_reason}")
    return n_sold


def run_cycle():
    """한 사이클 실행."""
    if check_killswitch():
        return

    if not is_us_market_open():
        logger.info("US 시장 휴장 — 분석만 (매매 X). 다음 사이클에 재확인.")
        # 휴장 시에도 분석은 함 (학습 데이터 + 사전 준비)
        # 하지만 buy/sell은 안 함

    sim = USPaperSimulator()
    pv = sim.portfolio_value()
    logger.info(f"=== 사이클 시작: total=${pv['total']:.2f} (P&L {pv['pnl_total_pct']:+.2f}%) ===")
    logger.info(f"Cash: ${pv['cash']:.2f}, Positions: {pv['n_positions']}/{MAX_POSITIONS}")

    # 1. 보유 종목 exit 체크 (시장 시간에만)
    if is_us_market_open():
        n_sold = check_exit_signals(sim)
        if n_sold > 0:
            logger.info(f"Exit: {n_sold}건 매도 완료")

    # 2. 신규 매수 후보 분석 (포지션 여유 있을 때만)
    available_slots = MAX_POSITIONS - len(sim.positions)
    if available_slots <= 0:
        logger.info("포지션 풀 — 신규 매수 안함")
    else:
        # 보유 종목 제외하고 분석
        candidates = [s for s in UNIVERSE if s not in sim.positions]
        # 너무 많이 호출하지 않도록 우선 10개만
        n_analyzed = 0
        for sym in candidates[:10]:
            if check_killswitch():
                break
            if available_slots <= 0:
                break
            decision = analyze_and_decide(sym, sim)
            n_analyzed += 1
            logger.info(f"  {sym}: {decision['action']} (conf={decision['confidence']:.2f}) - {decision['reason'][:60]}")
            if (decision["action"] == "buy"
                and decision["confidence"] >= MIN_CONFIDENCE
                and is_us_market_open()
                and sim.cash >= POSITION_SIZE_USD):
                r = sim.buy(sym, POSITION_SIZE_USD, reason=decision["reason"])
                if r.get("status") == "filled":
                    available_slots -= 1
                    logger.info(f"  [ENTRY] {sym}: ${POSITION_SIZE_USD}")
        logger.info(f"Analyzed {n_analyzed} candidates")

    # 3. 최종 상태
    pv = sim.portfolio_value()
    logger.info(f"=== 사이클 종료: total=${pv['total']:.2f} (P&L {pv['pnl_total_pct']:+.2f}%, {pv['n_positions']} positions) ===")


if __name__ == "__main__":
    run_cycle()
