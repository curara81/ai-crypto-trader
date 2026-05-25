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


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "movers"
    if cmd == "movers":
        print(json.dumps(fetch_top_movers_stocks(), indent=2, ensure_ascii=False))
    elif cmd == "recommend":
        print(json.dumps(recommend_stocks(), indent=2, ensure_ascii=False))
    elif cmd == "analyze":
        sym = sys.argv[2] if len(sys.argv) > 2 else "NVDA"
        print(json.dumps(analyze_stock(sym), indent=2, ensure_ascii=False))
