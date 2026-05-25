#!/usr/bin/env python3
"""v6.0: Alpaca paper trading 자율 봇.

- 5분 주기로 미국 시장 개장 시간만 동작
- 종목 universe: stock_analyzer.STOCK_UNIVERSE (50개)
- LLMRouter (Gemini 2.5 Flash)로 매수/매도 판단
- 종목당 $100 매수, max 10 포지션
- 결과 JSONL로 ML 학습용 누적
- KILLSWITCH 파일 존재 시 중단
- paper API only (실거래 URL 차단)
"""
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# scripts 경로
sys.path.insert(0, str(Path(__file__).resolve().parent))

from alpaca_client import AlpacaPaperClient

try:
    from llm_router import LLMRouter
    _LLM = LLMRouter()
except Exception as e:
    _LLM = None
    print(f"LLMRouter 로드 실패: {e}", file=sys.stderr)

try:
    from stock_analyzer import STOCK_UNIVERSE
except ImportError:
    STOCK_UNIVERSE = ["AAPL", "MSFT", "GOOGL", "NVDA", "AMD", "TSLA"]

# 설정
TRADING_ROOT = Path(os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading")))
KILLSWITCH = TRADING_ROOT / "KILLSWITCH"
LOG_DIR = TRADING_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True, parents=True)
DECISION_LOG = LOG_DIR / "alpaca_decisions.jsonl"

LOOP_INTERVAL_SEC = 300  # 5분
DOLLARS_PER_TRADE = float(os.environ.get("ALPACA_DOLLARS_PER_TRADE", "100"))
MAX_POSITIONS = int(os.environ.get("ALPACA_MAX_POSITIONS", "10"))
MIN_CONFIDENCE_BUY = float(os.environ.get("ALPACA_MIN_CONF_BUY", "0.6"))
MIN_CONFIDENCE_SELL = float(os.environ.get("ALPACA_MIN_CONF_SELL", "0.55"))

# universe 일부만 매 사이클 분석 (Gemini 비용 절약)
ANALYZE_PER_CYCLE = int(os.environ.get("ALPACA_ANALYZE_PER_CYCLE", "8"))

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(LOG_DIR / "alpaca_paper_bot.log")),
    ],
)
logger = logging.getLogger("alpaca_paper")

_shutdown = False


def _signal_handler(sig, frame):
    global _shutdown
    logger.info(f"signal {sig} → graceful shutdown")
    _shutdown = True


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


def _log_decision(record: dict):
    """매 의사결정 JSONL로 누적 (ML 학습용)."""
    record["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(DECISION_LOG, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"decision log 쓰기 실패: {e}")


def get_decision(symbol: str, position: dict | None, raw_data: dict) -> dict:
    """Gemini Flash로 매수/매도/관망 판단.

    Returns: {"action": "buy"|"sell"|"hold", "confidence": 0.0-1.0, "reason": "..."}
    """
    if not _LLM:
        return {"action": "hold", "confidence": 0, "reason": "LLM unavailable"}

    pos_summary = ""
    if position:
        qty = float(position.get("qty", 0))
        avg_entry = float(position.get("avg_entry_price", 0))
        cur = float(position.get("current_price", 0))
        unrealized_pct = (
            (cur / avg_entry - 1) * 100 if avg_entry > 0 else 0
        )
        pos_summary = (
            f"\n[현재 보유] {qty}주 @ ${avg_entry:.2f} "
            f"(현재 ${cur:.2f}, 미실현 {unrealized_pct:+.2f}%)"
        )

    prompt = f"""You are an aggressive US stock paper-trading bot. Decide BUY/SELL/HOLD for {symbol}.

## Market Data
Price: ${raw_data.get('price', 0):.2f}
24h change: {raw_data.get('change_24h', 0):+.2f}%
7d change: {raw_data.get('change_7d', 0):+.2f}%
30d change: {raw_data.get('change_30d', 0):+.2f}%
RSI(daily): {raw_data.get('rsi', 50):.1f}
Volume vs 30d avg: {raw_data.get('vol_ratio', 1):.2f}x
{pos_summary}

## Rules (paper trading - 데이터 누적 + 학습 목적이라 조금 공격적):
- 무포지션 + 강한 매수 신호 (모멘텀/거래량 + RSI 적정) → BUY
- 보유 중 + 손절 신호 (RSI 70+ 과열 또는 -3% 손실) → SELL
- 애매하면 HOLD
- 거의 50/50 → action=hold, 다음 사이클에 재평가
- Confidence 0.55-0.75 → 정상 ACT (이건 paper, 데이터 쌓아야 함)

JSON 응답:
{{"action": "buy"|"sell"|"hold", "confidence": 0.0-1.0, "reason": "<25자 이내 한국어>"}}
"""
    try:
        result = _LLM.call(prompt, model="gemini-2.5-flash", timeout=30)
        decision = result["json"]
        return {
            "action": str(decision.get("action", "hold")).lower(),
            "confidence": float(decision.get("confidence", 0)),
            "reason": str(decision.get("reason", ""))[:100],
        }
    except Exception as e:
        logger.warning(f"{symbol} 의사결정 실패: {e}")
        return {"action": "hold", "confidence": 0, "reason": f"error: {e}"}


def fetch_raw(symbol: str) -> dict | None:
    """yfinance로 종목 raw 데이터 (RSI 포함)."""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        hist = t.history(period="3mo", interval="1d")
        if hist.empty or len(hist) < 22:
            return None
        last = hist.iloc[-1]
        cur = float(last["Close"])
        ch_24h = (cur / hist["Close"].iloc[-2] - 1) * 100
        ch_7d = (cur / hist["Close"].iloc[-6] - 1) * 100
        ch_30d = (cur / hist["Close"].iloc[-22] - 1) * 100
        # RSI
        closes = hist["Close"].tolist()
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i-1]
            gains.append(max(d, 0))
            losses.append(max(-d, 0))
        avg_g = sum(gains[-14:]) / 14
        avg_l = sum(losses[-14:]) / 14
        rsi = 100 if avg_l == 0 else 100 - (100 / (1 + avg_g / avg_l))
        vol_ratio = float(last["Volume"]) / float(hist["Volume"].iloc[-22:].mean())
        return {
            "price": cur,
            "change_24h": ch_24h,
            "change_7d": ch_7d,
            "change_30d": ch_30d,
            "rsi": rsi,
            "vol_ratio": vol_ratio,
        }
    except Exception as e:
        logger.debug(f"{symbol} raw 실패: {e}")
        return None


def run_cycle(client: AlpacaPaperClient):
    """1 사이클: 시장 시간 체크 → universe 일부 분석 → 매매."""
    if KILLSWITCH.exists():
        logger.warning("KILLSWITCH 활성 — 사이클 건너뜀")
        return

    if not client.is_market_open():
        logger.info("미국 시장 closed — 사이클 건너뜀")
        return

    account = client.get_account()
    cash = float(account.get("cash", 0))
    equity = float(account.get("equity", 0))
    logger.info(f"계좌: equity=${equity:,.0f}, cash=${cash:,.0f}")

    positions = {p["symbol"]: p for p in client.get_positions()}
    logger.info(f"보유: {len(positions)}개 {list(positions.keys())[:10]}")

    # 매 사이클 분석할 종목 선정:
    # 1) 보유 종목은 항상 (매도 신호 체크)
    # 2) 미보유 + cash 충분하면 universe에서 ANALYZE_PER_CYCLE개 sample
    import random
    held_syms = set(positions.keys())
    candidates = list(held_syms)  # 보유는 무조건 분석 (매도 판단)
    if cash >= DOLLARS_PER_TRADE and len(positions) < MAX_POSITIONS:
        non_held = [s for s in STOCK_UNIVERSE if s not in held_syms]
        sample = random.sample(non_held, min(ANALYZE_PER_CYCLE, len(non_held)))
        candidates.extend(sample)

    logger.info(f"이번 사이클 분석: {len(candidates)}개 (보유 {len(held_syms)} + 후보 {len(candidates) - len(held_syms)})")

    for sym in candidates:
        if _shutdown or KILLSWITCH.exists():
            return
        raw = fetch_raw(sym)
        if not raw:
            continue
        pos = positions.get(sym)
        decision = get_decision(sym, pos, raw)
        action = decision["action"]
        conf = decision["confidence"]
        reason = decision["reason"]

        _log_decision({
            "symbol": sym,
            "action": action,
            "confidence": conf,
            "reason": reason,
            "price": raw["price"],
            "rsi": raw["rsi"],
            "ch_24h": raw["change_24h"],
            "position": "yes" if pos else "no",
        })

        try:
            if action == "buy" and not pos and conf >= MIN_CONFIDENCE_BUY:
                if cash >= DOLLARS_PER_TRADE and len(positions) < MAX_POSITIONS:
                    client.buy_dollars(sym, DOLLARS_PER_TRADE)
                    cash -= DOLLARS_PER_TRADE
                    positions[sym] = {"qty": 0}  # placeholder
                    logger.info(f"✅ BUY {sym} ${DOLLARS_PER_TRADE} (conf={conf:.2f}, {reason})")
            elif action == "sell" and pos and conf >= MIN_CONFIDENCE_SELL:
                client.sell_all(sym)
                positions.pop(sym, None)
                logger.info(f"💰 SELL {sym} (conf={conf:.2f}, {reason})")
            else:
                logger.debug(f"{sym}: {action} conf={conf:.2f} (no action)")
        except Exception as e:
            logger.error(f"{sym} 주문 실패: {e}")

        time.sleep(0.5)  # rate limit 여유


def main():
    logger.info("=" * 60)
    logger.info("v6.0 Alpaca Paper Trading Bot 시작")
    logger.info(f"  종목당 매수: ${DOLLARS_PER_TRADE}")
    logger.info(f"  최대 포지션: {MAX_POSITIONS}")
    logger.info(f"  사이클당 분석: {ANALYZE_PER_CYCLE} (랜덤 + 보유 전부)")
    logger.info(f"  Min confidence: BUY {MIN_CONFIDENCE_BUY}, SELL {MIN_CONFIDENCE_SELL}")
    logger.info("=" * 60)

    try:
        client = AlpacaPaperClient()
    except Exception as e:
        logger.error(f"Alpaca 초기화 실패: {e}")
        logger.error("Keychain에 ALPACA_API_KEY + ALPACA_API_SECRET 등록 필요")
        sys.exit(1)

    while not _shutdown:
        try:
            run_cycle(client)
        except Exception as e:
            logger.exception(f"사이클 오류: {e}")
        if _shutdown:
            break
        # 시장 closed면 더 길게 sleep
        wait = LOOP_INTERVAL_SEC
        try:
            if not client.is_market_open():
                wait = 1800  # 30분
        except Exception:
            pass
        for _ in range(wait):
            if _shutdown:
                break
            time.sleep(1)

    logger.info("Alpaca paper bot 정상 종료")


if __name__ == "__main__":
    main()
