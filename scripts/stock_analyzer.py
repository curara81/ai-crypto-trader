#!/usr/bin/env python3
"""v4.2: 해외주식(미국) 분석 도구 — coin_analyzer와 동일 패턴.

3가지 분석 기능 (Gemini 2.5 Pro 사용):
  1. fetch_top_movers_stocks(n=10) — S&P 500 + 인기주 24h 상승/하락 TOP N
  2. recommend_stocks(n=5) — AI 추천 미국 주식
  3. analyze_stock(symbol) — 특정 주식 심층 분석

데이터 소스:
  - Yahoo Finance via yfinance (free, no key)
  - 시장 시간 외에는 전일 종가 기준
"""
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests


def _safe_float(v) -> Optional[float]:
    """NaN/Inf/None을 None으로 변환 (JSON 직렬화 안전). v5.0.4"""
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None

logger = logging.getLogger(__name__)

try:
    from llm_router import LLMRouter
    _LLM = LLMRouter()
except Exception as e:
    _LLM = None
    logger.warning(f"LLMRouter 로드 실패: {e}")

try:
    import yfinance as yf
    _YF_OK = True
except ImportError:
    _YF_OK = False
    logger.warning("yfinance 미설치")

# v4.3: 실시간 뉴스 + grounding
try:
    from grounded_news import fetch_grounded_news
    _GROUNDED_OK = True
except ImportError:
    _GROUNDED_OK = False

try:
    from coin_analyzer import fetch_tavily_news  # 동일 함수 재사용
except ImportError:
    def fetch_tavily_news(*args, **kwargs):
        return []


# ─── 종목 유니버스 (50개) ─────────────────
STOCK_UNIVERSE = [
    # Tech 빅테크
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AMD",
    "AVGO", "ORCL", "ADBE", "CRM", "NFLX", "INTC", "QCOM", "CSCO",
    # 금융
    "JPM", "BAC", "WFC", "GS", "MS", "V", "MA", "AXP",
    # 크립토 관련
    "COIN", "MSTR", "RIOT", "MARA",
    # 핫픽
    "PLTR", "SHOP", "NET", "SNOW", "RBLX", "ROKU", "UBER", "ABNB",
    # 한국인 선호
    "DIS", "NKE", "SBUX", "KO", "WMT", "JNJ",
    # 헬스/바이오
    "LLY", "PFE", "MRNA",
    # 한국주
    "LULU", "CALM", "HRMY",
]

# v5.2: 국내 주식 유니버스 (KOSPI 30 + KOSDAQ 20) — yfinance ticker
STOCK_UNIVERSE_KR = [
    # KOSPI 대형주 (시총 상위)
    ("005930.KS", "삼성전자",     "반도체"),
    ("000660.KS", "SK하이닉스",   "반도체"),
    ("373220.KS", "LG에너지솔루션","2차전지"),
    ("207940.KS", "삼성바이오로직스","바이오"),
    ("005380.KS", "현대차",       "자동차"),
    ("000270.KS", "기아",         "자동차"),
    ("035420.KS", "NAVER",        "인터넷"),
    ("035720.KS", "카카오",       "인터넷"),
    ("005490.KS", "POSCO홀딩스",  "철강"),
    ("051910.KS", "LG화학",       "화학"),
    ("006400.KS", "삼성SDI",      "2차전지"),
    ("105560.KS", "KB금융",       "금융"),
    ("055550.KS", "신한지주",     "금융"),
    ("086790.KS", "하나금융지주", "금융"),
    ("316140.KS", "우리금융지주", "금융"),
    ("012330.KS", "현대모비스",   "자동차부품"),
    ("028260.KS", "삼성물산",     "건설"),
    ("066570.KS", "LG전자",       "가전"),
    ("003550.KS", "LG",           "지주"),
    ("034730.KS", "SK",           "지주"),
    ("015760.KS", "한국전력",     "전력"),
    ("032830.KS", "삼성생명",     "보험"),
    ("033780.KS", "KT&G",         "담배"),
    ("011200.KS", "HMM",          "해운"),
    ("009150.KS", "삼성전기",     "전자부품"),
    ("000810.KS", "삼성화재",     "보험"),
    ("003670.KS", "포스코퓨처엠", "2차전지소재"),
    ("018260.KS", "삼성에스디에스","IT서비스"),
    ("030200.KS", "KT",           "통신"),
    ("017670.KS", "SK텔레콤",     "통신"),
    # KOSDAQ 대형주
    ("247540.KQ", "에코프로비엠", "2차전지소재"),
    ("086520.KQ", "에코프로",     "2차전지"),
    ("091990.KQ", "셀트리온헬스케어","바이오"),
    ("196170.KQ", "알테오젠",     "바이오"),
    ("293490.KQ", "카카오게임즈", "게임"),
    ("042700.KQ", "한미반도체",   "반도체장비"),
    ("058470.KQ", "리노공업",     "반도체부품"),
    ("357780.KQ", "솔브레인",     "반도체소재"),
    ("039030.KQ", "이오테크닉스", "반도체장비"),
    ("112040.KQ", "위메이드",     "게임"),
    ("095340.KQ", "ISC",          "반도체부품"),
    ("214150.KQ", "클래시스",     "의료기기"),
    ("328130.KQ", "루닛",         "AI"),
    ("389030.KQ", "신성델타테크", "2차전지부품"),
    ("000990.KQ", "DB하이텍",     "반도체"),
    ("348370.KQ", "엔켐",         "2차전지소재"),
    ("277810.KQ", "레인보우로보틱스","로봇"),
    ("253450.KQ", "스튜디오드래곤","미디어"),
    ("078340.KQ", "컴투스",       "게임"),
    ("181710.KQ", "NHN",          "인터넷"),
]


# ─── TopMovers ───────────────────────────────
def fetch_top_movers_stocks(n: int = 10) -> dict:
    """미국 주식 50개 유니버스에서 24h 상승률 TOP N."""
    if not _YF_OK:
        return {"error": "yfinance 미설치"}

    try:
        tickers_str = " ".join(STOCK_UNIVERSE)
        # 2일 데이터로 일일 변화율 계산
        data = yf.download(tickers_str, period="5d", interval="1d",
                          group_by="ticker", progress=False, threads=True)

        enriched = []
        for sym in STOCK_UNIVERSE:
            try:
                if sym in data.columns.get_level_values(0).unique():
                    df = data[sym]
                else:
                    continue
                if df.empty or len(df) < 2:
                    continue
                # v5.0.4: NaN 행 제거 후 마지막 2개 (yfinance NaN으로 인한 500 방지)
                df_valid = df.dropna(subset=["Close"])
                if len(df_valid) < 2:
                    continue
                last = df_valid.iloc[-1]
                prev = df_valid.iloc[-2]
                close_last = _safe_float(last["Close"])
                close_prev = _safe_float(prev["Close"])
                if not close_last or not close_prev or close_last <= 0 or close_prev <= 0:
                    continue
                change_24h = (close_last / close_prev - 1) * 100
                vol_last = _safe_float(last.get("Volume", 0)) or 0
                volume_usd = vol_last * close_last
                if volume_usd < 10_000_000:
                    continue
                enriched.append({
                    "symbol": sym,
                    "price": close_last,
                    "change_24h": _safe_float(change_24h),
                    "volume_24h_usd": _safe_float(volume_usd),
                    "high_24h": _safe_float(last.get("High")) or close_last,
                    "low_24h": _safe_float(last.get("Low")) or close_last,
                    "noise_flag": abs(change_24h) > 25,
                })
            except (KeyError, IndexError, ValueError, TypeError) as e:
                logger.debug(f"stock {sym} skipped: {e}")
                continue

        gainers = sorted(enriched, key=lambda x: x["change_24h"], reverse=True)[:n]
        losers = sorted(enriched, key=lambda x: x["change_24h"])[:n]
        by_volume = sorted(enriched, key=lambda x: x["volume_24h_usd"], reverse=True)[:n]

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_stocks": len(enriched),
            "top_gainers": gainers,
            "top_losers": losers,
            "top_volume": by_volume,
        }
    except Exception as e:
        logger.error(f"주식 TopMovers 실패: {e}")
        return {"error": str(e)[:200]}


# v5.2: 국내 주식 TopMovers
def fetch_top_movers_kr(n: int = 10) -> dict:
    """국내 주식(KOSPI+KOSDAQ) 24h 상승/하락/거래량 TOP N (yfinance .KS/.KQ)."""
    if not _YF_OK:
        return {"error": "yfinance 미설치"}
    try:
        symbols_map = {t[0]: (t[1], t[2]) for t in STOCK_UNIVERSE_KR}
        tickers_str = " ".join(symbols_map.keys())
        data = yf.download(tickers_str, period="5d", interval="1d",
                           group_by="ticker", progress=False, threads=True)

        enriched = []
        available = set(data.columns.get_level_values(0).unique()) if hasattr(data.columns, 'get_level_values') else set()
        for sym, (name_ko, sector) in symbols_map.items():
            try:
                if sym not in available:
                    continue
                df = data[sym]
                if df.empty or len(df) < 2:
                    continue
                df_valid = df.dropna(subset=["Close"])
                if len(df_valid) < 2:
                    continue
                last = df_valid.iloc[-1]
                prev = df_valid.iloc[-2]
                close_last = _safe_float(last["Close"])
                close_prev = _safe_float(prev["Close"])
                if not close_last or not close_prev or close_last <= 0 or close_prev <= 0:
                    continue
                change_24h = (close_last / close_prev - 1) * 100
                vol_last = _safe_float(last.get("Volume", 0)) or 0
                volume_krw = vol_last * close_last
                # 국내 일일 거래대금 10억원 미만 제외
                if volume_krw < 1_000_000_000:
                    continue
                enriched.append({
                    "symbol": sym,
                    "name_ko": name_ko,
                    "sector": sector,
                    "price": close_last,
                    "change_24h": _safe_float(change_24h),
                    "volume_24h_krw": _safe_float(volume_krw),
                    "high_24h": _safe_float(last.get("High")) or close_last,
                    "low_24h": _safe_float(last.get("Low")) or close_last,
                    "noise_flag": abs(change_24h) > 25,
                })
            except (KeyError, IndexError, ValueError, TypeError) as e:
                logger.debug(f"KR stock {sym} skipped: {e}")
                continue

        gainers = sorted(enriched, key=lambda x: x["change_24h"], reverse=True)[:n]
        losers = sorted(enriched, key=lambda x: x["change_24h"])[:n]
        by_volume = sorted(enriched, key=lambda x: x["volume_24h_krw"], reverse=True)[:n]

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_stocks": len(enriched),
            "top_gainers": gainers,
            "top_losers": losers,
            "top_volume": by_volume,
        }
    except Exception as e:
        logger.error(f"KR TopMovers 실패: {e}")
        return {"error": str(e)[:200]}


# ─── AI 추천 ─────────────────────────────────
def recommend_stocks(n: int = 5) -> dict:
    """Gemini Pro로 주목할만한 미국 주식 N개 추천."""
    if not _LLM:
        return {"error": "LLMRouter 미가용"}
    if not _YF_OK:
        return {"error": "yfinance 미설치"}

    movers = fetch_top_movers_stocks(n=15)
    if "error" in movers:
        return movers

    # 시장 컨텍스트: S&P 500, VIX
    try:
        spy = yf.Ticker("SPY").history(period="5d")
        vix = yf.Ticker("^VIX").history(period="2d")
        spy_change = (spy["Close"].iloc[-1] / spy["Close"].iloc[-2] - 1) * 100 if len(spy) >= 2 else 0
        spy_week_change = (spy["Close"].iloc[-1] / spy["Close"].iloc[0] - 1) * 100 if len(spy) >= 5 else 0
        vix_level = vix["Close"].iloc[-1] if len(vix) >= 1 else 0
    except Exception:
        spy_change = spy_week_change = vix_level = 0

    gainers_str = "\n".join(
        f"  {g['symbol']:6s} {g['change_24h']:+.2f}%  "
        f"(${g['price']:>8.2f}, 거래대금 ${g['volume_24h_usd']/1e9:.1f}B)"
        for g in movers["top_gainers"][:10]
    )
    losers_str = "\n".join(
        f"  {l['symbol']:6s} {l['change_24h']:+.2f}%"
        for l in movers["top_losers"][:5]
    )
    volume_str = "\n".join(
        f"  {v['symbol']:6s} ${v['volume_24h_usd']/1e9:.1f}B ({v['change_24h']:+.2f}%)"
        for v in movers["top_volume"][:10]
    )

    prompt = f"""You are a senior US equity analyst. Recommend {n} US-listed stocks to watch over the next 1-7 days.

## Market Context
S&P 500 24h: {spy_change:+.2f}%
S&P 500 5d:  {spy_week_change:+.2f}%
VIX: {vix_level:.1f} ({'low fear' if vix_level < 15 else 'normal' if vix_level < 25 else 'elevated' if vix_level < 35 else 'high fear'})

## TOP 10 Gainers (24h, from 50-stock universe)
{gainers_str}

## TOP 5 Losers (24h)
{losers_str}

## TOP 10 by Volume
{volume_str}

## Task
Select {n} stocks from the lists. For each:
- symbol (must be from the universe above)
- thesis_ko (한국어 1-2 문장: 왜 주목할만한지)
- thesis_en (English 1-2 sentences)
- risk_level: low/medium/high
- time_horizon: short (1-3d) / medium (1w) / long (1m+)
- confidence: 0.0-1.0
- sector: tech/finance/crypto/consumer/healthcare/other

Consider:
- VIX level (low = momentum plays, high = quality)
- Don't just pick top gainers
- Volume × price action quality
- Earnings season risks if applicable

Respond JSON:
{{"recommendations": [{{"symbol": "...", "thesis_ko": "...", "thesis_en": "...", "risk_level": "...", "time_horizon": "...", "confidence": 0.0-1.0, "sector": "..."}}, ...]}}"""

    try:
        result = _LLM.call(prompt, model="gemini-2.5-pro", timeout=120)
        recs = result["json"].get("recommendations", [])
        logger.info(f"미주 AI 추천 {len(recs)}개 (Pro)")
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recommendations": recs,
            "context": {
                "spy_change_24h": spy_change,
                "spy_change_5d": spy_week_change,
                "vix": vix_level,
            },
            "usage": result.get("usage", {}),
        }
    except Exception as e:
        logger.error(f"미주 AI 추천 실패: {e}")
        return {"error": str(e)[:200]}


# ─── 특정 주식 심층 분석 ───────────────────
def analyze_stock(symbol: str) -> dict:
    """특정 미국 주식 심층 분석 (Pro 모델)."""
    if not _LLM:
        return {"error": "LLMRouter 미가용"}
    if not _YF_OK:
        return {"error": "yfinance 미설치"}

    sym = symbol.upper()

    try:
        t = yf.Ticker(sym)
        hist_daily = t.history(period="3mo", interval="1d")
        hist_hourly = t.history(period="5d", interval="1h")
        if hist_daily.empty:
            return {"error": f"종목 데이터 없음: {sym}"}

        last = hist_daily.iloc[-1]
        current = float(last["Close"])

        # 변화율
        ch_1d = (current / hist_daily["Close"].iloc[-2] - 1) * 100 if len(hist_daily) >= 2 else 0
        ch_7d = (current / hist_daily["Close"].iloc[-6] - 1) * 100 if len(hist_daily) >= 6 else 0
        ch_30d = (current / hist_daily["Close"].iloc[-22] - 1) * 100 if len(hist_daily) >= 22 else 0
        ch_3mo = (current / hist_daily["Close"].iloc[0] - 1) * 100

        # 범위
        high_30d = float(hist_daily["High"].iloc[-22:].max()) if len(hist_daily) >= 22 else current
        low_30d = float(hist_daily["Low"].iloc[-22:].min()) if len(hist_daily) >= 22 else current
        high_3mo = float(hist_daily["High"].max())
        low_3mo = float(hist_daily["Low"].min())

        # RSI 계산 (간이)
        def calc_rsi(closes, period=14):
            if len(closes) < period + 1:
                return 50
            gains, losses = [], []
            for i in range(1, len(closes)):
                d = closes[i] - closes[i-1]
                gains.append(max(d, 0))
                losses.append(max(-d, 0))
            avg_g = sum(gains[-period:]) / period
            avg_l = sum(losses[-period:]) / period
            if avg_l == 0:
                return 100
            rs = avg_g / avg_l
            return 100 - (100 / (1 + rs))

        rsi_daily = calc_rsi(hist_daily["Close"].tolist())
        rsi_hourly = calc_rsi(hist_hourly["Close"].tolist()) if not hist_hourly.empty else 50

        # 30일 범위 내 위치
        position_pct = (current - low_30d) / (high_30d - low_30d) * 100 if high_30d > low_30d else 50

        # 거래량
        vol_24h = float(last["Volume"])
        avg_vol_30d = float(hist_daily["Volume"].iloc[-22:].mean()) if len(hist_daily) >= 22 else vol_24h
        vol_ratio = vol_24h / avg_vol_30d if avg_vol_30d > 0 else 1

        # 회사 정보 (있으면)
        try:
            info = t.info
            company_name = info.get("longName", sym)
            market_cap = info.get("marketCap", 0)
            sector = info.get("sector", "Unknown")
            forward_pe = info.get("forwardPE", 0)
            dividend = info.get("dividendYield", 0)
        except Exception:
            company_name = sym
            market_cap = forward_pe = dividend = 0
            sector = "Unknown"

        # 글로벌 컨텍스트
        try:
            spy = yf.Ticker("SPY").history(period="2d")
            vix = yf.Ticker("^VIX").history(period="2d")
            spy_change = (spy["Close"].iloc[-1] / spy["Close"].iloc[-2] - 1) * 100 if len(spy) >= 2 else 0
            vix_level = float(vix["Close"].iloc[-1]) if len(vix) >= 1 else 0
        except Exception:
            spy_change = vix_level = 0

    except Exception as e:
        return {"error": f"데이터 수집 실패: {str(e)[:200]}"}

    # v4.3: 실시간 뉴스 + Google Search
    tavily_news = fetch_tavily_news(f"{company_name} {sym} stock", days=3, max_results=5)
    news_section = ""
    if tavily_news:
        news_section = "\n## Recent News (last 3 days)\n" + "\n".join(
            f"- {n['title']}\n  {n['content'][:200]}\n  source: {n['url']}"
            for n in tavily_news[:5]
        )

    grounded = {}
    grounded_section = ""
    sources_for_ui = []
    if _GROUNDED_OK:
        grounded = fetch_grounded_news(
            f"What are the latest news, earnings updates, analyst ratings, and catalysts for {company_name} ({sym}) stock in the past 7 days? Include any major announcements, earnings surprises, or sector-wide news.",
            max_chars=1200,
        )
        if grounded.get("text"):
            grounded_section = f"\n## Live Market Intelligence (Google Search, real-time)\n{grounded['text']}\n"
            sources_for_ui = grounded.get("sources", [])

    # v4.6.3: prompt를 parts로 분리 — 외부 텍스트는 절대 f-string 평가 안 되도록
    JSON_TEMPLATE = '''
## Task
Provide comprehensive analysis in JSON. **All narrative fields MUST be in 한국어 (Korean).**
Use the Recent News and Live Market Intelligence above for catalysts and risks — cite specific events.

**중요한 분석 원칙 (v4.9 - 정량/매크로/검증방법론 통합):**
1. **Valuation**: P/E를 동종 섹터 피어와 비교. 5년 평균 대비. "저평가/적정/고평가" 명시.
2. **본업**: 매출 segment 비율 + 주요 고객 의존도.
3. **신규 성장 동력**: 신규 시장 진출 + 점유율 + 파트너십.
4. **구체적 지정학/규제**: 추상적 표현 금지.
5. **주주 환원**: 배당 + 자사주 매입 5년 트렌드.
6. **★ 회사 공식 가이던스 우선**: Grounding의 회사 공식 가이던스(다음 분기 매출/EPS) vs 시장 컨센서스. 가이던스가 컨센서스 하회면 단기 낙관 자제.
7. **★ 시간 프레임 분리**: 단기(1주)/중기(3개월)/장기(1년) outlook을 각각 평가.
8. **★ 시나리오 분석 (Bull/Base/Bear)**: 각 시나리오에 확률(합 1.0), 가격 목표, 트리거 명시.
9. **★ 데이터 신선도**: yfinance P/E 등이 Grounding과 불일치 시 명시. 라이브 우선.
10. **★ 추격 매수 자제**: 30일 위치 80% 초과 + RSI 70+ 시 "분할 진입, 조정 시 매수" 톤.
11. **★ 정량 핵심 지표 (NEW v4.9)**: 수주 잔고($), DOI(재고일수), EPS Surprise %, 내부자 매수, 공매도 비율, 기관 보유 비율. Grounding으로 최신 수치 확보. 모르면 'N/A' 명시.
12. **★ 매크로 시나리오 맵핑 (NEW v4.9)**: Bull/Base/Bear 각각에 Fed 금리·달러·10Y·유동성 가정 명시. 단순 "거시 불확실성" 금지.
13. **★ 검증 방법론 6개 점수 (NEW v4.9)**: CANSLIM(O'Neil)/SEPA(Minervini)/Stage(Weinstein)/Wyckoff/Quality+Value/Momentum+RS 각각 0-10점 + 근거 1-2문장.
14. **★ 포지션 사이징 (NEW v4.9)**: R/R 비율 명시, 자본 대비 최대 비중 %, 분할 매수 가격대·비중, 손절 근거(2% rule/ATR/기술적), Kelly 추정(보수적).

{
  "summary_ko": "<한국어 2-3문장 핵심 요약>",
  "summary_en": "<English 2-3 sentence executive summary>",
  "current_setup": "<accumulation/distribution/breakout/breakdown/consolidation/range_bound/uptrend/downtrend>",
  "current_setup_ko": "<위 상태를 한국어로>",
  "trend_1d": "<bullish/bearish/neutral>",
  "trend_1w": "<bullish/bearish/neutral>",
  "trend_1m": "<bullish/bearish/neutral>",
  "support_levels_usd": [<3 지지선>],
  "resistance_levels_usd": [<3 저항선>],
  "entry_zone_usd": [<low, high>],
  "stop_loss_usd": <price>,
  "target_1_usd": <보수적 목표>,
  "target_2_usd": <공격적 목표>,
  "risk_reward_ratio": <number>,
  "key_risks_ko": ["<구체적 리스크1>", "<리스크2>", "<리스크3>"],
  "key_catalysts_ko": ["<구체적 모멘텀1>", "<모멘텀2>"],
  "recommendation": "STRONG_BUY/BUY/HOLD/SELL/STRONG_SELL/AVOID",
  "confidence": 0.0-1.0,
  "time_horizon": "short/medium/long",
  "korean_advice": "<한국어 상세 조언 3-5문장>",
  "valuation_ko": "<밸류에이션 판단: P/E를 동종 피어와 비교. 5년 평균 대비. 저평가/적정/고평가 명시. 2-3문장.>",
  "valuation_verdict": "undervalued/fair/overvalued",
  "valuation_peer_comparison": "<예: P/E 22 (NVDA 35, AVGO 28 대비 저평가)>",
  "core_business_ko": "<매출 segment 비율 + 주요 고객 + 주력 제품. 3-4문장.>",
  "core_business_segments": [
    {"name": "<segment명>", "revenue_share_pct": 0, "trend": "growing/stable/declining"}
  ],
  "growth_drivers_ko": "<신규 시장 + 점유율 + 파트너십. 3-4문장.>",
  "shareholder_returns_ko": "<배당, 자사주 매입, 5년 트렌드. 2-3문장.>",
  "geopolitical_risk_ko": "<구체적 지정학/규제 리스크. 3-4문장.>",
  "company_guidance_ko": "<회사 공식 다음 분기 가이던스(매출/EPS) vs 시장 컨센서스. Grounding 정보 활용. 가이던스가 컨센서스 상회/하회/부합 여부 명시. 2-3문장. 모르면 '공식 가이던스 정보 부족' 명시.>",
  "horizon_analysis": {
    "short_term_1w": {"outlook": "bullish/neutral/bearish", "summary_ko": "<기술적 매매 관점, 1-7일. 1-2문장.>", "confidence": 0.0},
    "medium_term_3m": {"outlook": "bullish/neutral/bearish", "summary_ko": "<실적 모멘텀 + 가이던스 관점, 1-3개월. 1-2문장.>", "confidence": 0.0},
    "long_term_1y": {"outlook": "bullish/neutral/bearish", "summary_ko": "<펀더멘털 + 비즈니스 모델 + 산업 구조, 6개월~1년. 1-2문장.>", "confidence": 0.0}
  },
  "scenarios": {
    "bullish": {"probability": 0.0, "price_target_usd": 0, "triggers_ko": ["<상승 트리거1>", "<트리거2>"], "narrative_ko": "<낙관 시나리오 1-2문장>"},
    "base": {"probability": 0.0, "price_range_usd": [0, 0], "triggers_ko": ["<중립 가정>"], "narrative_ko": "<기본 시나리오 1-2문장>"},
    "bearish": {"probability": 0.0, "downside_target_usd": 0, "triggers_ko": ["<하락 트리거1>", "<트리거2>"], "narrative_ko": "<비관 시나리오 1-2문장>"}
  },
  "data_freshness_note_ko": "<제공된 P/E 등 펀더멘털 데이터가 최신인지 확인. Grounding과 불일치 시 라이브 수치 우선. 1-2문장.>",
  "quantitative_metrics": {
    "backlog_or_pipeline_usd_ko": "<수주 잔고/파이프라인 $ 규모. 예: '자동차 부문 수주 300억 달러 (2024 발표)'. Grounding에서 확인. 없으면 '공시 자료 부족' 명시.>",
    "inventory_days_ko": "<DOI(재고 회전일수) 또는 채널 inventory 수준. 반도체/하드웨어면 사이클 위치(피크/하강/저점/회복) 명시. 모르면 'N/A'.>",
    "earnings_surprise_last_q_pct": "<최근 분기 EPS 서프라이즈 %. 양수=beat, 음수=miss. 모르면 null.>",
    "insider_activity_90d_ko": "<최근 90일 내부자 매수/매도 동향. Grounding 우선. 모르면 'N/A'.>",
    "short_interest_pct": "<공매도 비율 %. 모르면 null.>",
    "institutional_ownership_pct": "<기관 보유 %. 모르면 null.>"
  },
  "macro_assumptions": {
    "current_macro_phase_ko": "<현재 매크로 사이클 위치. 예: 'Fed 동결 후반부, 인하 기대 vs 끈적한 인플레 갈등'. 1-2문장.>",
    "bullish_macro_ko": "<Bull 시나리오의 매크로 가정. 예: 'Fed 25bp 인하 시작, USD Index 100 이하, 10Y 4% 미만, 유동성 확장'. 1-2문장.>",
    "base_macro_ko": "<Base 매크로 가정. 1-2문장.>",
    "bearish_macro_ko": "<Bear 매크로 가정. 예: '인플레 재점화, Fed 추가 인상, USD Index 110+, 10Y 5% 돌파'. 1-2문장.>"
  },
  "methodology_scores": {
    "canslim": {"score": 0, "notes_ko": "<William O'Neil CANSLIM 7요소(EPS 분기 성장, EPS 연 성장, 신제품/신고가, 수급, RS Rating, 기관 후원, 시장 방향) 적합도. 1-2문장.>"},
    "sepa_minervini": {"score": 0, "notes_ko": "<Minervini SEPA Stage 2 진입 여부 + VCP(Volatility Contraction Pattern) + 7주 base 평가. 1-2문장.>"},
    "stage_weinstein": {"score": 0, "notes_ko": "<Stan Weinstein 30주 MA 기준 Stage 1(저점매집)/2(상승)/3(고점분산)/4(하락) 위치. 1-2문장.>"},
    "wyckoff": {"score": 0, "notes_ko": "<Wyckoff 누적/분산 phase + Spring/Upthrust/SOS 시그널. 1-2문장.>"},
    "quality_value": {"score": 0, "notes_ko": "<Quality(ROIC, 영업이익률, FCF Margin) × Value(P/E, P/FCF, EV/EBITDA) 결합 평가. 1-2문장.>"},
    "momentum_rs": {"score": 0, "notes_ko": "<vs S&P 500 RS(Relative Strength) + 52주 신고가 근접도 + 3·6·12개월 모멘텀. 1-2문장.>"}
  },
  "position_sizing": {
    "risk_reward_ratio_explicit": <float, target_1 vs stop_loss 기준>,
    "max_position_pct_of_capital": "<권장 최대 자본 비중. 예: '4-6%' (변동성·확신도 기반). 고변동·저확신은 1-3%.>",
    "scaling_in_plan_ko": "<분할 매수 가격대와 비중. 예: '$210에서 40%, $218에서 30%, $225 돌파 확인 후 30%'. 3-4문장.>",
    "stop_loss_rationale_ko": "<손절 근거: 2% rule(계좌 대비 1회 손실), ATR(평균 변동성), 기술적 지지선 중 어느 기준인지 명시. 1-2문장.>",
    "kelly_fraction_estimate": <float, 0.0-0.25 범위로 보수적 — Half-Kelly 권장>
  }
}

Be specific with dollar prices, not vague.

**v5.0 IMPORTANT — Bilingual output**:
For EVERY field ending in `_ko`, ALSO include the corresponding `_en` field with high-quality
Wall-Street-analyst-level English (not literal translation). For example:
- summary_ko → also provide summary_en
- narrative_ko → also provide narrative_en
- triggers_ko (array) → also provide triggers_en (array)
- valuation_ko → valuation_en
- core_business_ko → core_business_en
- growth_drivers_ko → growth_drivers_en
- shareholder_returns_ko → shareholder_returns_en
- geopolitical_risk_ko → geopolitical_risk_en
- company_guidance_ko → company_guidance_en
- data_freshness_note_ko → data_freshness_note_en
- horizon_analysis.*.summary_ko → horizon_analysis.*.summary_en
- scenarios.*.narrative_ko → scenarios.*.narrative_en
- scenarios.*.triggers_ko → scenarios.*.triggers_en
- key_risks_ko / key_catalysts_ko → key_risks_en / key_catalysts_en
- korean_advice → ALSO english_advice (English equivalent)
- All quantitative_metrics *_ko → *_en
- All macro_assumptions *_ko → *_en
- All methodology_scores.*.notes_ko → notes_en
- position_sizing scaling_in_plan_ko / stop_loss_rationale_ko → scaling_in_plan_en / stop_loss_rationale_en

English fields must be natural professional English (CFA/sell-side level), NOT word-for-word translation.
'''

    # Header: 변수만 (외부 텍스트 X)
    header = f"""You are a senior US equity analyst. Provide deep analysis of {sym} ({company_name}).

## Current State
Price: ${current:.2f}
Change: 1d {ch_1d:+.2f}% / 7d {ch_7d:+.2f}% / 30d {ch_30d:+.2f}% / 3mo {ch_3mo:+.2f}%
30d Range: ${low_30d:.2f} ~ ${high_30d:.2f} (current at {position_pct:.0f}% of range)
3mo Range: ${low_3mo:.2f} ~ ${high_3mo:.2f}

## Volume
24h: {vol_24h/1e6:.1f}M shares (${vol_24h * current / 1e9:.1f}B)
30d avg: {avg_vol_30d/1e6:.1f}M
Ratio: {vol_ratio:.2f}x

## Indicators
RSI (daily): {rsi_daily:.1f}
RSI (hourly): {rsi_hourly:.1f}

## Fundamentals
Sector: {sector}
Market Cap: ${market_cap/1e9:.1f}B
Forward P/E: {forward_pe:.1f}
Dividend Yield: {(dividend or 0)*100:.2f}%

## Market Context
S&P 500 24h: {spy_change:+.2f}%
VIX: {vix_level:.1f}
"""

    # 최종 prompt = header(f-string) + raw news/grounded + raw JSON template
    prompt = header + (news_section or "") + (grounded_section or "") + JSON_TEMPLATE

    try:
        result = _LLM.call(prompt, model="gemini-2.5-pro", timeout=120)
        analysis = result["json"]
        logger.info(f"미주 분석 {sym} 완료 (Pro)")
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": sym,
            "company_name": company_name,
            "current_price": current,
            "raw_data": {
                "ch_1d": ch_1d, "ch_7d": ch_7d, "ch_30d": ch_30d, "ch_3mo": ch_3mo,
                "rsi_daily": rsi_daily, "rsi_hourly": rsi_hourly,
                "position_30d_pct": position_pct,
                "high_30d": high_30d, "low_30d": low_30d,
                "volume_ratio": vol_ratio, "market_cap": market_cap,
                "forward_pe": forward_pe, "dividend_yield": dividend,
                "sector": sector,
                "spy_change": spy_change, "vix": vix_level,
            },
            "analysis": analysis,
            "news": tavily_news,           # v4.3
            "grounded": grounded,           # v4.3
            "sources": sources_for_ui,      # v4.3 UI 출처
            "usage": result.get("usage", {}),
        }
    except Exception as e:
        logger.error(f"미주 분석 {sym} 실패: {e}")
        return {"error": str(e)[:200], "symbol": sym}


# ─── v5.2: 국내 주식 분석 ─────────────────────────────────
def _kr_company_name(symbol: str) -> str:
    """KR 종목 코드 → 회사명 (universe + yfinance info fallback)."""
    for sym, name, _sector in STOCK_UNIVERSE_KR:
        if sym == symbol:
            return name
    return symbol


def _parse_pct(value) -> Optional[float]:
    """문자열/숫자에서 % 숫자만 추출. '14.2%' → 14.2, '14.246' → 14.246."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        s = str(value).strip().replace("%", "").replace(",", "")
        return float(s)
    except (ValueError, TypeError):
        return None


def _sanity_check_kr(analysis: dict, raw_data: dict) -> dict:
    """v6.2: KR 분석 응답 sanity check + 신뢰도 평가.

    검출: 외국인+기관 합계 모순 / 베타 outlier / 배당수익률 outlier /
    방법론 0/10 + 시나리오 모순 / 저유동성.
    """
    warnings = []
    reliability = "HIGH"  # HIGH / MEDIUM / LOW

    q = analysis.get("quantitative_metrics", {}) or {}

    # [규칙 1] 외국인 + 기관 합계 ≤ 유통주식 (free float)
    foreign_pct = _parse_pct(q.get("foreign_ownership_pct"))
    # institutional_flow_ko는 보통 narrative 텍스트라 % 추출 어려움 — 스킵
    if foreign_pct is not None:
        if foreign_pct < 0 or foreign_pct > 100:
            warnings.append(f"외국인 보유율 {foreign_pct}% 비현실적")
            q["foreign_ownership_pct"] = "검증 실패 (재확인 필요)"
            reliability = "LOW"
        # 정확한 free float는 데이터 부족이지만 50% 초과는 의심
        elif foreign_pct > 50:
            warnings.append(f"외국인 보유율 {foreign_pct}% 비정상 높음 — 출처 재확인 권고")
            reliability = "MEDIUM" if reliability == "HIGH" else reliability

    # [규칙 2] 베타 outlier
    beta = raw_data.get("beta")
    if beta is not None and (beta < 0.3 or beta > 3.0):
        warnings.append(f"베타 {beta} 비정상 범위 (0.3~3.0 권장) — 저유동성 가능")

    # [규칙 3] 배당수익률
    div_yield = _parse_pct(q.get("dividend_yield_pct"))
    if div_yield is not None:
        if div_yield > 100:
            warnings.append(f"배당수익률 {div_yield}% 비현실적 — 단위 오기 추정")
            q["dividend_yield_pct"] = "검증 실패 (실제는 0~10% 범위)"
            reliability = "LOW"
        elif div_yield > 20:
            warnings.append(f"배당수익률 {div_yield}% 의심스러움 — 출처 확인 권고")
            reliability = "MEDIUM" if reliability == "HIGH" else reliability

    # [규칙 6] 5개 방법론 모두 2/10 이하 → 시나리오 제거 + AVOID
    methods = analysis.get("methodology_scores", {}) or {}
    scores = [m.get("score", 0) for m in methods.values() if isinstance(m, dict)]
    if scores and all(s <= 2 for s in scores):
        warnings.append("모든 방법론 ≤2/10 → 시나리오/목표가 제거 (논리 일관성)")
        analysis["scenarios"] = {}
        if analysis.get("recommendation") not in ("AVOID", "STRONG_SELL"):
            analysis["recommendation"] = "AVOID"
        analysis["korean_advice"] = (
            "⚠️ 정량 신호 전무 (모든 방법론 0~2/10). AI 시스템 분석 부적합 종목. "
            "정성적 관망 권고. " + (analysis.get("korean_advice", "") or "")
        )
        reliability = "LOW"

    # [규칙 7] 저유동성 종목 경고 (일평균 거래대금)
    vol_ratio = raw_data.get("volume_ratio", 1)
    avg_vol = raw_data.get("avg_volume_30d_krw")
    if avg_vol and avg_vol < 100_000_000:  # 1억 KRW 미만
        warnings.append(
            f"일평균 거래대금 {avg_vol/1e8:.1f}억 KRW — 저유동성, 데이터 신뢰도 낮음. "
            f"거래 시 지정가/분할 매매 권고"
        )
        reliability = "MEDIUM" if reliability == "HIGH" else reliability

    analysis["_sanity_warnings"] = warnings
    analysis["_reliability"] = reliability
    return analysis


def recommend_kr_stocks(n: int = 5) -> dict:
    """Gemini Pro로 KOSPI/KOSDAQ 주식 N개 추천."""
    if not _LLM:
        return {"error": "LLMRouter 미가용"}
    if not _YF_OK:
        return {"error": "yfinance 미설치"}

    movers = fetch_top_movers_kr(n=15)
    if "error" in movers:
        return movers

    # 시장 컨텍스트
    try:
        kospi = yf.Ticker("^KS11").history(period="5d")
        kosdaq = yf.Ticker("^KQ11").history(period="5d")
        usdkrw = yf.Ticker("KRW=X").history(period="2d")
        kospi_ch = (kospi["Close"].iloc[-1] / kospi["Close"].iloc[-2] - 1) * 100 if len(kospi) >= 2 else 0
        kospi_5d = (kospi["Close"].iloc[-1] / kospi["Close"].iloc[0] - 1) * 100 if len(kospi) >= 5 else 0
        kosdaq_ch = (kosdaq["Close"].iloc[-1] / kosdaq["Close"].iloc[-2] - 1) * 100 if len(kosdaq) >= 2 else 0
        usdkrw_now = float(usdkrw["Close"].iloc[-1]) if len(usdkrw) >= 1 else 0
    except Exception:
        kospi_ch = kospi_5d = kosdaq_ch = usdkrw_now = 0

    gainers_str = "\n".join(
        f"  {g['symbol']:11s} ({g['name_ko']:12s} · {g['sector']}) {g['change_24h']:+.2f}%  "
        f"가격 {g['price']:>10,.0f} KRW · 거래대금 {g['volume_24h_krw']/1e8:.0f}억"
        for g in movers["top_gainers"][:10]
    )
    losers_str = "\n".join(
        f"  {l['symbol']:11s} ({l['name_ko']}) {l['change_24h']:+.2f}%"
        for l in movers["top_losers"][:5]
    )

    prompt = f"""당신은 한국 주식시장 시니어 애널리스트입니다. KOSPI/KOSDAQ 주식 {n}개를 1~7일 관점으로 추천하세요.

## 시장 컨텍스트
KOSPI 1d: {kospi_ch:+.2f}% / 5d: {kospi_5d:+.2f}%
KOSDAQ 1d: {kosdaq_ch:+.2f}%
USD/KRW: {usdkrw_now:.0f}

## TOP 10 Gainers (KR, 24h)
{gainers_str}

## TOP 5 Losers
{losers_str}

## Task
위 종목 중 {n}개 선정. 각 종목별:
- symbol (반드시 위 목록의 .KS/.KQ 코드)
- name_ko (한국 회사명)
- thesis_ko (한국어 1-2문장: 왜 주목)
- thesis_en (English 1-2 sentences)
- risk_level: low/medium/high
- time_horizon: short (1-3d) / medium (1w) / long (1m+)
- confidence: 0.0-1.0
- sector: 한국 섹터 (반도체/2차전지/바이오/자동차/금융 등)

고려사항:
- 단순 급등주만 고르지 말 것 (이미 움직임)
- 거래대금 × 모멘텀 + 외국인/기관 매수세
- KOSPI 200 편입 종목 안정성, KOSDAQ 성장성

JSON 응답:
{{"recommendations": [{{"symbol": "...", "name_ko": "...", "thesis_ko": "...", "thesis_en": "...", "risk_level": "...", "time_horizon": "...", "confidence": 0.0-1.0, "sector": "..."}}, ...]}}"""

    try:
        result = _LLM.call(prompt, model="gemini-2.5-pro", timeout=120)
        recs = result["json"].get("recommendations", [])
        # v6.1.1: name_ko 보장 — Gemini가 빠뜨려도 universe/네이버에서 채움
        try:
            from symbol_search import KR_STOCK_ALIASES_KO, _search_naver_kr
        except ImportError:
            KR_STOCK_ALIASES_KO = {}
            _search_naver_kr = None
        # universe → 회사명 reverse lookup
        kr_universe_names = {sym: name for sym, name, _sec in STOCK_UNIVERSE_KR}
        # alias DB → 회사명 (코드 기준 reverse)
        alias_names = {sym: name for name, sym in KR_STOCK_ALIASES_KO.items()}
        for rec in recs:
            sym = rec.get("symbol", "")
            if rec.get("name_ko"):
                continue  # 이미 있음
            # 1) universe
            if sym in kr_universe_names:
                rec["name_ko"] = kr_universe_names[sym]
                continue
            # 2) alias
            if sym in alias_names:
                rec["name_ko"] = alias_names[sym]
                continue
            # 3) 네이버 fallback (코드만 발췌)
            if _search_naver_kr:
                code = sym.replace(".KS", "").replace(".KQ", "")
                naver = _search_naver_kr(code, limit=1)
                if naver:
                    rec["name_ko"] = naver[0].get("name_ko", "")
        logger.info(f"KR AI 추천 {len(recs)}개 (Pro) — 회사명 보장")
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recommendations": recs,
            "context": {
                "kospi_change_1d": kospi_ch,
                "kospi_change_5d": kospi_5d,
                "kosdaq_change_1d": kosdaq_ch,
                "usdkrw": usdkrw_now,
            },
            "usage": result.get("usage", {}),
        }
    except Exception as e:
        logger.error(f"KR AI 추천 실패: {e}")
        return {"error": str(e)[:200]}


def analyze_kr_stock(symbol: str) -> dict:
    """특정 KOSPI/KOSDAQ 종목 심층 분석 (Pro). symbol은 '005930.KS' 또는 '005930' (자동 보정)."""
    if not _LLM:
        return {"error": "LLMRouter 미가용"}
    if not _YF_OK:
        return {"error": "yfinance 미설치"}

    sym = symbol.upper().strip()
    # 6자리 숫자만 들어오면 .KS suffix 자동 추가
    if sym.isdigit() and len(sym) == 6:
        # KS 시도 후 실패 시 KQ
        sym_ks = sym + ".KS"
        try:
            test = yf.Ticker(sym_ks).history(period="1d")
            sym = sym_ks if not test.empty else sym + ".KQ"
        except Exception:
            sym = sym + ".KS"
    elif not (sym.endswith(".KS") or sym.endswith(".KQ")):
        # 그 외에도 .KS 가정
        sym = sym + ".KS"

    company_name_default = _kr_company_name(sym)

    try:
        t = yf.Ticker(sym)
        hist_daily = t.history(period="3mo", interval="1d")
        hist_hourly = t.history(period="5d", interval="1h")
        if hist_daily.empty:
            return {"error": f"종목 데이터 없음: {sym}"}

        last = hist_daily.iloc[-1]
        current = float(last["Close"])
        ch_1d = (current / hist_daily["Close"].iloc[-2] - 1) * 100 if len(hist_daily) >= 2 else 0
        ch_7d = (current / hist_daily["Close"].iloc[-6] - 1) * 100 if len(hist_daily) >= 6 else 0
        ch_30d = (current / hist_daily["Close"].iloc[-22] - 1) * 100 if len(hist_daily) >= 22 else 0
        ch_3mo = (current / hist_daily["Close"].iloc[0] - 1) * 100

        high_30d = float(hist_daily["High"].iloc[-22:].max()) if len(hist_daily) >= 22 else current
        low_30d = float(hist_daily["Low"].iloc[-22:].min()) if len(hist_daily) >= 22 else current
        high_3mo = float(hist_daily["High"].max())
        low_3mo = float(hist_daily["Low"].min())

        def calc_rsi(closes, period=14):
            if len(closes) < period + 1:
                return 50
            gains, losses = [], []
            for i in range(1, len(closes)):
                d = closes[i] - closes[i-1]
                gains.append(max(d, 0))
                losses.append(max(-d, 0))
            avg_g = sum(gains[-period:]) / period
            avg_l = sum(losses[-period:]) / period
            if avg_l == 0:
                return 100
            rs = avg_g / avg_l
            return 100 - (100 / (1 + rs))

        rsi_daily = calc_rsi(hist_daily["Close"].tolist())
        rsi_hourly = calc_rsi(hist_hourly["Close"].tolist()) if not hist_hourly.empty else 50
        position_pct = (current - low_30d) / (high_30d - low_30d) * 100 if high_30d > low_30d else 50

        vol_24h = float(last["Volume"])
        avg_vol_30d = float(hist_daily["Volume"].iloc[-22:].mean()) if len(hist_daily) >= 22 else vol_24h
        vol_ratio = vol_24h / avg_vol_30d if avg_vol_30d > 0 else 1
        # v6.2: 일평균 거래대금 (KRW) — sanity check용
        avg_vol_30d_krw = avg_vol_30d * current

        try:
            info = t.info
            company_name = info.get("longName") or info.get("shortName") or company_name_default
            market_cap = info.get("marketCap", 0)
            sector = info.get("sector", "Unknown")
            forward_pe = info.get("forwardPE", 0)
            beta_val = info.get("beta")  # v6.2: yfinance 베타
            dividend = info.get("dividendYield", 0)
        except Exception:
            company_name = company_name_default
            market_cap = forward_pe = dividend = 0
            sector = "Unknown"

        try:
            kospi = yf.Ticker("^KS11").history(period="2d")
            kosdaq = yf.Ticker("^KQ11").history(period="2d")
            usdkrw = yf.Ticker("KRW=X").history(period="2d")
            kospi_change = (kospi["Close"].iloc[-1] / kospi["Close"].iloc[-2] - 1) * 100 if len(kospi) >= 2 else 0
            kosdaq_change = (kosdaq["Close"].iloc[-1] / kosdaq["Close"].iloc[-2] - 1) * 100 if len(kosdaq) >= 2 else 0
            usdkrw_now = float(usdkrw["Close"].iloc[-1]) if len(usdkrw) >= 1 else 0
        except Exception:
            kospi_change = kosdaq_change = usdkrw_now = 0
    except Exception as e:
        return {"error": f"데이터 수집 실패: {str(e)[:200]}"}

    # 뉴스 + Grounding
    tavily_news = fetch_tavily_news(f"{company_name} 주식", days=3, max_results=5)
    news_section = ""
    if tavily_news:
        news_section = "\n## Recent News (한국 매체 우선, 3일)\n" + "\n".join(
            f"- {n['title']}\n  {n['content'][:200]}\n  source: {n['url']}"
            for n in tavily_news[:5]
        )

    grounded = {}
    grounded_section = ""
    sources_for_ui = []
    if _GROUNDED_OK:
        grounded = fetch_grounded_news(
            f"{company_name} ({sym}) 한국 주식 최근 7일 뉴스, 실적, 애널리스트 의견, "
            f"외국인/기관 매매, 카탈리스트. KOSPI/KOSDAQ 시장 동향 포함.",
            max_chars=1200,
        )
        if grounded.get("text"):
            grounded_section = f"\n## Live Market Intelligence (Google Search, 실시간)\n{grounded['text']}\n"
            sources_for_ui = grounded.get("sources", [])

    JSON_TEMPLATE_KR = '''
## Task
한국 주식에 대한 종합 분석을 JSON으로 작성. 모든 narrative 필드는 한국어.
뉴스/Grounding의 외국인/기관 매매, 공시, 실적 등 한국 시장 특수성 반영.

**분석 원칙 (v6.2 - 한국 시장 특화 + 환각 방지):**
1. **밸류에이션**: PER을 동종 KOSPI/KOSDAQ 피어 + 글로벌 피어 양쪽 비교.
2. **본업**: 매출 부문별 비중 + 주요 거래처 의존도.
3. **신규 성장 동력**: 신사업 + 점유율 + 파트너십.
4. **구체적 리스크**: 한국 규제(공정위, 금감원), 지정학(대중·대미 관계), 환율.
5. **주주 환원**: 배당, 자사주매입, 지배구조 이슈.
6. **회사 공식 가이던스 vs 컨센서스**.
7. **시간 프레임 분리**: 단기/중기/장기.
8. **시나리오 (Bull/Base/Bear)** + 확률 + 가격 목표 + 트리거.
9. **외국인·기관 수급**: Grounding에서 최근 동향 확인.
10. **공매도/대차잔고**: 코스닥은 특히 중요.

**🚫 v6.2 환각 방지 — 정량 데이터 절대 규칙 (위반 시 신뢰도 LOW)**:

[규칙 1] **외국인 + 기관 합계는 유통 주식수 이하**여야 함
- 한국 소형주는 최대주주 + 특수관계인 + 자사주가 60-80% 흔함
- 유통주식 = 100% - 최대주주 - 자사주
- 외국인 + 기관은 유통주식보다 클 수 없음 (산술적 불가)
- 모르면 'N/A'. **절대 14.246% 같은 정밀 수치 환각 금지**

[규칙 2] **베타는 0.3~3.0 범위 외면 'N/A'**
- 일평균 거래량 1만 주 미만은 베타 추정 불가 → 'N/A'
- 3.0+ 절대 사용 금지

[규칙 3] **배당수익률은 0~10%**. 20%+ 면 단위 오기 → 'N/A'. 100%+ 절대 불가

[규칙 4] **자산 2조원 미만 한국 기업은 분기 의무공시 면제**
- 분기 잠정실적 인용 시 출처 URL 필수
- 출처 없으면 "분기 잠정실적 공시 자료 부족" 명시
- **절대 "1Q 적자전환, 영업이익 -8%" 같은 정밀 수치 환각 금지**

[규칙 5] 모든 정량 수치는 **출처 또는 'N/A'**. 출처 = Grounding URL 또는 yfinance

[규칙 6] **5개 방법론 모두 2/10 이하면 시나리오/목표가 제시 금지**
- recommendation = "AVOID", scenarios 모두 빈 객체 {}
- 결론: "AI 시스템 분석 부적합 — 정성적 관망"

[규칙 7] **일평균 거래대금 1억 KRW 미만은 "저유동성, 신뢰도 낮음" 명시**
- 거래 시 지정가/분할 매매 권고 강조

{
  "summary_ko": "<한국어 2-3문장>",
  "summary_en": "<English 2-3 sentences>",
  "current_setup": "<accumulation/distribution/breakout/breakdown/consolidation/range_bound/uptrend/downtrend>",
  "current_setup_ko": "<한국어>",
  "trend_1d": "<bullish/bearish/neutral>",
  "trend_1w": "<bullish/bearish/neutral>",
  "trend_1m": "<bullish/bearish/neutral>",
  "support_levels_krw": [<3 지지선 원화>],
  "resistance_levels_krw": [<3 저항선 원화>],
  "entry_zone_krw": [<low, high>],
  "stop_loss_krw": <원화>,
  "target_1_krw": <보수 목표>,
  "target_2_krw": <공격 목표>,
  "risk_reward_ratio": <number>,
  "key_risks_ko": ["<구체적 리스크1>", "<2>", "<3>"],
  "key_catalysts_ko": ["<구체적 모멘텀1>", "<2>"],
  "recommendation": "STRONG_BUY/BUY/HOLD/SELL/STRONG_SELL/AVOID",
  "confidence": 0.0-1.0,
  "time_horizon": "short/medium/long",
  "korean_advice": "<한국어 상세 3-5문장>",
  "valuation_ko": "<PER 동종 비교. 저평가/적정/고평가. 2-3문장.>",
  "valuation_verdict": "undervalued/fair/overvalued",
  "valuation_peer_comparison": "<예: PER 15 (삼성 12, SK 18 대비 중간)>",
  "core_business_ko": "<매출 부문 + 주요 고객. 3-4문장.>",
  "growth_drivers_ko": "<신규 성장 동력. 3-4문장.>",
  "shareholder_returns_ko": "<배당, 자사주매입. 2-3문장.>",
  "geopolitical_risk_ko": "<한국 규제 + 지정학 + 환율. 3-4문장.>",
  "company_guidance_ko": "<회사 가이던스 vs 컨센서스. 2-3문장.>",
  "horizon_analysis": {
    "short_term_1w": {"outlook": "bullish/neutral/bearish", "summary_ko": "<1-2문장>", "confidence": 0.0},
    "medium_term_3m": {"outlook": "bullish/neutral/bearish", "summary_ko": "<1-2문장>", "confidence": 0.0},
    "long_term_1y": {"outlook": "bullish/neutral/bearish", "summary_ko": "<1-2문장>", "confidence": 0.0}
  },
  "scenarios": {
    "bullish": {"probability": 0.0, "price_target_krw": 0, "triggers_ko": ["<상승1>", "<2>"], "narrative_ko": "<1-2문장>"},
    "base": {"probability": 0.0, "price_range_krw": [0, 0], "triggers_ko": ["<중립>"], "narrative_ko": "<1-2문장>"},
    "bearish": {"probability": 0.0, "downside_target_krw": 0, "triggers_ko": ["<하락1>", "<2>"], "narrative_ko": "<1-2문장>"}
  },
  "data_freshness_note_ko": "<펀더 데이터 최신성. 1-2문장.>",
  "quantitative_metrics": {
    "foreign_ownership_pct_ko": "<외국인 보유 비율 %. Grounding 활용. 모르면 'N/A'.>",
    "institutional_flow_ko": "<최근 30일 기관·외국인 순매수 동향. 1-2문장.>",
    "short_interest_ko": "<공매도 비율 또는 대차잔고. 코스닥 중요. 모르면 'N/A'.>",
    "earnings_surprise_last_q_pct": "<최근 분기 영업이익 서프라이즈 %. 모르면 null.>",
    "kospi_200_inclusion_ko": "<KOSPI 200 편입 여부 + 영향. 1문장.>",
    "dividend_yield_pct": "<배당수익률 %. 모르면 null.>"
  },
  "macro_assumptions": {
    "current_macro_phase_ko": "<현재 한국 경제 사이클. 한은 금리/원달러/수출. 1-2문장.>",
    "bullish_macro_ko": "<Bull 매크로: 예 '한은 인하 시작, 원달러 1300원 하향, 반도체 사이클 회복, 수출 +5%'.>",
    "base_macro_ko": "<Base 매크로.>",
    "bearish_macro_ko": "<Bear 매크로: 예 '원달러 1400+, 미·중 갈등 격화, 수출 둔화'.>"
  },
  "methodology_scores": {
    "sepa_minervini": {"score": 0, "notes_ko": "<Stage 2 + VCP. 1-2문장.>"},
    "stage_weinstein": {"score": 0, "notes_ko": "<30주 MA Stage 1-4. 1-2문장.>"},
    "wyckoff": {"score": 0, "notes_ko": "<Wyckoff phase. 1-2문장.>"},
    "quality_value": {"score": 0, "notes_ko": "<ROE/영업이익률/PER. 1-2문장.>"},
    "momentum_rs_vs_kospi": {"score": 0, "notes_ko": "<vs KOSPI RS + 52주 신고가. 1-2문장.>"},
    "foreign_institutional_flow": {"score": 0, "notes_ko": "<외국인/기관 매매 강도. 1-2문장.>"}
  },
  "position_sizing": {
    "risk_reward_ratio_explicit": <float>,
    "max_position_pct_of_capital": "<예: '4-6%'. 코스닥은 더 보수적.>",
    "scaling_in_plan_ko": "<분할 매수 가격대. 3-4문장.>",
    "stop_loss_rationale_ko": "<2% rule/ATR/지지선. 1-2문장.>",
    "kelly_fraction_estimate": <float, 0.0-0.20 보수적>
  }
}

**v5.0 Bilingual**: 모든 _ko 필드에 _en pair도 함께 제공 (예: summary_ko + summary_en).
한국 시장 특수 용어는 영문판에 ('외국인 매매' → 'foreign investor flows') 자연스러운 영어.
'''

    header = f"""당신은 한국 주식시장 시니어 애널리스트입니다. {sym} ({company_name})를 심층 분석하세요.

## 현재 상태
가격: {current:,.0f} KRW
변동: 1d {ch_1d:+.2f}% / 7d {ch_7d:+.2f}% / 30d {ch_30d:+.2f}% / 3mo {ch_3mo:+.2f}%
30일 범위: {low_30d:,.0f} ~ {high_30d:,.0f} KRW (현재 {position_pct:.0f}% 위치)
3개월 범위: {low_3mo:,.0f} ~ {high_3mo:,.0f} KRW

## 거래량
24h: {vol_24h/1e6:.1f}M 주 ({vol_24h * current / 1e8:.0f}억 원)
30일 평균: {avg_vol_30d/1e6:.1f}M 주
비율: {vol_ratio:.2f}x

## 지표
RSI(일): {rsi_daily:.1f}
RSI(시간): {rsi_hourly:.1f}

## 펀더멘털
섹터: {sector}
시가총액: {market_cap/1e12:.2f}조 원 (KRW)
Forward PER: {forward_pe:.1f}
배당수익률: {(dividend or 0)*100:.2f}%

## 시장 컨텍스트
KOSPI 1d: {kospi_change:+.2f}%
KOSDAQ 1d: {kosdaq_change:+.2f}%
USD/KRW: {usdkrw_now:.0f}
"""

    prompt = header + (news_section or "") + (grounded_section or "") + JSON_TEMPLATE_KR

    try:
        result = _LLM.call(prompt, model="gemini-2.5-pro", timeout=120)
        analysis = result["json"]
        # v6.2: sanity check 적용 — outlier 자동 검출 + 신뢰도 평가
        raw_for_check = {
            "beta": beta_val,
            "volume_ratio": vol_ratio,
            "avg_volume_30d_krw": avg_vol_30d_krw,
        }
        analysis = _sanity_check_kr(analysis, raw_for_check)
        warnings = analysis.get("_sanity_warnings", [])
        reliability = analysis.get("_reliability", "HIGH")
        if warnings:
            logger.warning(f"KR 분석 {sym} sanity warnings ({reliability}): {warnings}")
        logger.info(f"KR 분석 {sym} 완료 (Pro, reliability={reliability})")
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": sym,
            "company_name": company_name,
            "current_price": current,
            "raw_data": {
                "ch_1d": ch_1d, "ch_7d": ch_7d, "ch_30d": ch_30d, "ch_3mo": ch_3mo,
                "rsi_daily": rsi_daily, "rsi_hourly": rsi_hourly,
                "position_30d_pct": position_pct,
                "high_30d": high_30d, "low_30d": low_30d,
                "volume_ratio": vol_ratio, "market_cap": market_cap,
                "forward_pe": forward_pe, "dividend_yield": dividend,
                "sector": sector,
                "beta": beta_val,  # v6.2
                "avg_volume_30d_krw": avg_vol_30d_krw,  # v6.2
                "kospi_change": kospi_change, "kosdaq_change": kosdaq_change,
                "usdkrw": usdkrw_now,
            },
            "analysis": analysis,
            "news": tavily_news,
            "grounded": grounded,
            "sources": sources_for_ui,
            "usage": result.get("usage", {}),
        }
    except Exception as e:
        logger.error(f"KR 분석 {sym} 실패: {e}")
        return {"error": str(e)[:200], "symbol": sym}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "movers"
    if cmd == "movers":
        print(json.dumps(fetch_top_movers_stocks(), indent=2, ensure_ascii=False))
    elif cmd == "recommend":
        print(json.dumps(recommend_stocks(), indent=2, ensure_ascii=False))
    elif cmd == "analyze":
        sym = sys.argv[2] if len(sys.argv) > 2 else "NVDA"
        print(json.dumps(analyze_stock(sym), indent=2, ensure_ascii=False))
