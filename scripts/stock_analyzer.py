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
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

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
                last = df.iloc[-1]
                prev = df.iloc[-2]
                if not (last["Close"] > 0 and prev["Close"] > 0):
                    continue
                change_24h = (last["Close"] / prev["Close"] - 1) * 100
                volume_usd = float(last["Volume"]) * float(last["Close"])
                enriched.append({
                    "symbol": sym,
                    "price": float(last["Close"]),
                    "change_24h": float(change_24h),
                    "volume_24h_usd": volume_usd,
                    "high_24h": float(last["High"]),
                    "low_24h": float(last["Low"]),
                })
            except (KeyError, IndexError, ValueError):
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

    # v4.6.2: news/grounded 텍스트의 {...} 가 f-string 변수로 잘못 해석되는 것 방지
    news_section_safe = news_section.replace("{", "{{").replace("}", "}}")
    grounded_section_safe = grounded_section.replace("{", "{{").replace("}", "}}")

    # Gemini Pro 분석 프롬프트
    prompt = f"""You are a senior US equity analyst. Provide deep analysis of {sym} ({company_name}).

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
Dividend Yield: {dividend*100:.2f}%

## Market Context
S&P 500 24h: {spy_change:+.2f}%
VIX: {vix_level:.1f}
{news_section_safe}
{grounded_section_safe}

## Task
Provide comprehensive analysis in JSON. **All narrative fields MUST be in 한국어 (Korean).**
Use the Recent News and Live Market Intelligence above for catalysts and risks — cite specific events.

**중요한 분석 원칙 (v4.6 — 사용자 피드백 기반):**
1. **Valuation 필수**: P/E를 동종 섹터 피어(예: 반도체면 NVDA/AVGO/AMD)와 비교. 5년 평균 PER과도 비교. "저평가/적정/고평가" 명시.
2. **본업 분석**: 매출 구성(스마트폰칩/자동차/IoT 등 segment 비율). 주요 고객(애플/삼성 등) 의존도.
3. **신규 성장 동력**: 회사가 신규 진출 중인 시장 (예: 퀄컴의 PC AI, 엔비디아의 데이터센터). 점유율 데이터.
4. **구체적 지정학/규제 리스크**: "거시 불확실성" 같은 추상적 표현 금지. 구체적 사건/규제 명시 (예: 미-중 반도체 수출 통제, EU AI Act 등).
5. **주주 환원**: 배당 + 자사주 매입 정책. 5년간 트렌드.

{{
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
  "key_risks_ko": ["<구체적 리스크1: 회사명/규제명/사건 포함>", "<리스크2>", "<리스크3>"],
  "key_catalysts_ko": ["<구체적 모멘텀1: 제품명/계약 포함>", "<모멘텀2>"],
  "recommendation": "STRONG_BUY/BUY/HOLD/SELL/STRONG_SELL/AVOID",
  "confidence": 0.0-1.0,
  "time_horizon": "short/medium/long",
  "korean_advice": "<한국어 상세 조언 3-5문장>",

  "valuation_ko": "<밸류에이션 판단: 위 P/E 수치를 동종 피어(예: NVDA/AVGO/AMD)와 비교. 5년 평균 PER 대비. 저평가/적정/고평가 명시. 2-3문장.>",
  "valuation_verdict": "undervalued/fair/overvalued",
  "valuation_peer_comparison": "<예: 'P/E 22 (NVDA 35, AVGO 28, AMD 27 대비 저평가)' 식의 구체적 비교>",

  "core_business_ko": "<매출 구성(segment 비율) + 주요 고객 의존도 + 주력 제품 라인업. 3-4문장.>",
  "core_business_segments": [
    {{"name": "<segment명 한국어>", "revenue_share_pct": <추정 %>, "trend": "growing/stable/declining"}}
  ],

  "growth_drivers_ko": "<신규 진출 시장/제품 + 시장 점유율 변화 + 파트너십. 3-4문장. 구체적 제품명/회사명 포함.>",

  "shareholder_returns_ko": "<배당 수익률, 자사주 매입 정책, 최근 5년 자본 환원 트렌드. 2-3문장.>",

  "geopolitical_risk_ko": "<구체적 지정학 리스크: 미-중 갈등/특정 국가 매출 의존도/규제(SEC, EU AI Act 등). 추상적 '거시 불확실성' 금지. 3-4문장.>"
}}

Be specific with dollar prices, not vague."""

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


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "movers"
    if cmd == "movers":
        print(json.dumps(fetch_top_movers_stocks(), indent=2, ensure_ascii=False))
    elif cmd == "recommend":
        print(json.dumps(recommend_stocks(), indent=2, ensure_ascii=False))
    elif cmd == "analyze":
        sym = sys.argv[2] if len(sys.argv) > 2 else "NVDA"
        print(json.dumps(analyze_stock(sym), indent=2, ensure_ascii=False))
