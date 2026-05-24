#!/usr/bin/env python3
"""v4.0: 코인 분석 도구 — GCP 크레딧 적극 활용.

3가지 분석 기능 (Gemini 2.5 Pro 사용):
  1. fetch_top_movers(n=10) — Upbit 24h 상승률 TOP/하락률 TOP
  2. recommend_coins(n=5) — AI가 주목할만한 코인 추천 (시장 종합 분석)
  3. analyze_coin(symbol) — 특정 코인 심층 분석

데이터 소스:
  - Upbit 공개 API (ticker, candle, orderbook)
  - CoinGecko (글로벌 시장 데이터, 무료)
  - alternative.me (Fear & Greed)
"""
import json
import logging
import os
import time
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

# v4.3: Grounded news (실시간 웹 검색)
try:
    from grounded_news import fetch_grounded_news
    _GROUNDED_OK = True
except ImportError:
    _GROUNDED_OK = False

try:
    from secrets_helper import get_secret as _get_secret
except ImportError:
    def _get_secret(k):
        return os.environ.get(k)


def fetch_tavily_news(query: str, days: int = 3, max_results: int = 5) -> list[dict]:
    """Tavily로 최근 뉴스 검색."""
    key = _get_secret("TAVILY_API_KEY")
    if not key:
        return []
    try:
        r = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                "topic": "news",
                "days": days,
            },
            timeout=10,
        )
        results = r.json().get("results", [])
        return [
            {
                "title": x.get("title", "")[:120],
                "url": x.get("url", ""),
                "content": (x.get("content", "") or "")[:300],
                "published_date": x.get("published_date", ""),
            }
            for x in results
        ]
    except Exception as e:
        logger.warning(f"Tavily {query} 실패: {e}")
        return []


# ─── 데이터 소스 ────────────────────────────
class UpbitData:
    """Upbit 공개 API 래퍼."""

    @staticmethod
    def all_krw_markets() -> list[str]:
        """KRW 마켓의 모든 종목 코드 반환."""
        try:
            r = requests.get("https://api.upbit.com/v1/market/all", timeout=10)
            return [m["market"] for m in r.json() if m["market"].startswith("KRW-")]
        except Exception as e:
            logger.warning(f"Upbit market list 실패: {e}")
            return []

    @staticmethod
    def tickers(markets: list[str]) -> list[dict]:
        """다수 마켓의 현재가 일괄 조회."""
        if not markets:
            return []
        try:
            # Upbit 최대 100개씩
            results = []
            for i in range(0, len(markets), 100):
                chunk = ",".join(markets[i:i + 100])
                r = requests.get(
                    "https://api.upbit.com/v1/ticker",
                    params={"markets": chunk},
                    timeout=10,
                )
                results.extend(r.json())
            return results
        except Exception as e:
            logger.warning(f"Upbit tickers 실패: {e}")
            return []

    @staticmethod
    def candles(market: str, count: int = 100, unit_minutes: int = 60) -> list[dict]:
        """캔들 데이터 (default: 1시간 × 100개)."""
        try:
            r = requests.get(
                f"https://api.upbit.com/v1/candles/minutes/{unit_minutes}",
                params={"market": market, "count": count},
                timeout=10,
            )
            return r.json()
        except Exception as e:
            logger.warning(f"Upbit candles 실패 ({market}): {e}")
            return []

    @staticmethod
    def daily_candles(market: str, count: int = 30) -> list[dict]:
        try:
            r = requests.get(
                "https://api.upbit.com/v1/candles/days",
                params={"market": market, "count": count},
                timeout=10,
            )
            return r.json()
        except Exception:
            return []


class GlobalData:
    """글로벌 시장 데이터 (CoinGecko, alternative.me)."""

    @staticmethod
    def fear_greed() -> Optional[dict]:
        try:
            r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
            d = r.json().get("data", [{}])[0]
            return {
                "score": int(d.get("value", 0)),
                "classification": d.get("value_classification", "?"),
            }
        except Exception:
            return None

    @staticmethod
    def btc_dominance() -> Optional[dict]:
        try:
            r = requests.get("https://api.alternative.me/v2/global/", timeout=5)
            d = r.json().get("data", {})
            quote = d.get("quotes", {}).get("USD", {})
            return {
                "btc_dominance": d.get("bitcoin_percentage_of_market_cap", 0),
                "market_cap_change_24h": quote.get("percent_change_24h", 0),
                "total_market_cap": quote.get("total_market_cap", 0),
            }
        except Exception:
            return None

    @staticmethod
    def coingecko_trending() -> list[dict]:
        """CoinGecko 24h 트렌딩 (무료, 키 불필요)."""
        try:
            r = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
            coins = r.json().get("coins", [])
            return [c["item"] for c in coins[:10]]
        except Exception:
            return []


# ─── TopMovers ───────────────────────────────
def fetch_top_movers(n: int = 10) -> dict:
    """Upbit KRW 마켓 24h 상승률/하락률 TOP N + 거래량 TOP N."""
    markets = UpbitData.all_krw_markets()
    if not markets:
        return {"error": "마켓 조회 실패"}

    tickers = UpbitData.tickers(markets)
    if not tickers:
        return {"error": "ticker 조회 실패"}

    # 24h 변화율 기준 정렬
    enriched = []
    for t in tickers:
        try:
            enriched.append({
                "market": t["market"],
                "symbol": t["market"].replace("KRW-", ""),
                "price": t.get("trade_price", 0),
                "change_24h": t.get("signed_change_rate", 0) * 100,
                "volume_24h_krw": t.get("acc_trade_price_24h", 0),
                "high_24h": t.get("high_price", 0),
                "low_24h": t.get("low_price", 0),
            })
        except KeyError:
            continue

    # 거래량 1억 미만 제외 (스캠 코인 필터)
    enriched = [e for e in enriched if e["volume_24h_krw"] >= 100_000_000]

    gainers = sorted(enriched, key=lambda x: x["change_24h"], reverse=True)[:n]
    losers = sorted(enriched, key=lambda x: x["change_24h"])[:n]
    by_volume = sorted(enriched, key=lambda x: x["volume_24h_krw"], reverse=True)[:n]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_pairs": len(enriched),
        "top_gainers": gainers,
        "top_losers": losers,
        "top_volume": by_volume,
    }


# ─── AI 추천 ─────────────────────────────────
def recommend_coins(n: int = 5) -> dict:
    """Gemini Pro로 주목할만한 코인 N개 추천 + 이유."""
    if not _LLM:
        return {"error": "LLMRouter 미가용"}

    # 시장 컨텍스트 수집
    movers = fetch_top_movers(n=15)
    fg = GlobalData.fear_greed()
    btc_d = GlobalData.btc_dominance()
    trending = GlobalData.coingecko_trending()

    if "error" in movers:
        return movers

    # 프롬프트 구성
    gainers_str = "\n".join(
        f"  {g['symbol']:8s} {g['change_24h']:+.2f}% "
        f"(가격 {g['price']:>12,.4f}, 거래대금 {g['volume_24h_krw']/1e8:.0f}억)"
        for g in movers["top_gainers"][:10]
    )
    losers_str = "\n".join(
        f"  {l['symbol']:8s} {l['change_24h']:+.2f}%"
        for l in movers["top_losers"][:5]
    )
    volume_str = "\n".join(
        f"  {v['symbol']:8s} {v['volume_24h_krw']/1e8:.0f}억 ({v['change_24h']:+.2f}%)"
        for v in movers["top_volume"][:10]
    )
    trending_str = "\n".join(
        f"  {t['symbol']} ({t.get('name', '?')}) market_rank #{t.get('market_cap_rank', '?')}"
        for t in trending[:5]
    ) if trending else "(데이터 없음)"

    prompt = f"""You are a senior crypto market analyst. Recommend {n} Korean Upbit-listed coins to watch over the next 1-7 days.

## Market Context

Fear & Greed Index: {fg['score'] if fg else '?'} ({fg['classification'] if fg else '?'})
BTC Dominance: {btc_d['btc_dominance']:.1f}% (24h change: {btc_d['market_cap_change_24h']:+.2f}%) {'' if btc_d else '(unavailable)'}
Total Crypto Market Cap: ${btc_d['total_market_cap']/1e12:.2f}T

## Upbit KRW Market — Top 10 Gainers (24h)
{gainers_str}

## Top 5 Losers (24h)
{losers_str}

## Top 10 by Volume (KRW)
{volume_str}

## CoinGecko Global Trending (Search)
{trending_str}

## Task
Select {n} coins from the Upbit-listed candidates. For each, provide:
- symbol (must be from the lists above)
- thesis_ko (한국어 1-2문장: 왜 주목할만한지)
- thesis_en (English 1-2 sentences)
- risk_level: low/medium/high
- time_horizon: short (1-3d) / medium (1w) / long (1m+)
- confidence: 0.0-1.0

Consider:
- Don't just pick top gainers (may have already moved)
- Volume × volatility combination = best entry candidates
- Fear & Greed + BTC.D context
- Avoid extreme losers (catching falling knife)

Respond JSON:
{{"recommendations": [
  {{"symbol": "...", "thesis_ko": "한국어 설명", "thesis_en": "English", "risk_level": "...", "time_horizon": "...", "confidence": 0.0-1.0}},
  ...
]}}"""

    try:
        result = _LLM.call(prompt, model="gemini-2.5-pro", timeout=120)
        recs = result["json"].get("recommendations", [])
        logger.info(f"AI 추천 {len(recs)}개 생성 (Pro 모델, "
                    f"in={result['usage'].get('promptTokenCount', 0)} "
                    f"out={result['usage'].get('candidatesTokenCount', 0)} "
                    f"think={result['usage'].get('thoughtsTokenCount', 0)})")
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recommendations": recs,
            "context": {"fear_greed": fg, "btc_dominance": btc_d},
            "usage": result.get("usage", {}),
        }
    except Exception as e:
        logger.error(f"AI 추천 실패: {e}")
        return {"error": str(e)[:200]}


# ─── 특정 코인 심층 분석 ───────────────────
def analyze_coin(symbol: str) -> dict:
    """특정 코인 심층 분석 (Pro 모델, 다중 데이터 소스).

    symbol: "BTC", "ETH", "XRP" 등 (KRW- 접두사 없이)
    """
    if not _LLM:
        return {"error": "LLMRouter 미가용"}

    market = f"KRW-{symbol.upper()}"

    # 1) 현재 시세
    tickers = UpbitData.tickers([market])
    if not tickers:
        return {"error": f"종목 조회 실패: {market}"}
    t = tickers[0]

    # 2) 1시간 캔들 100개 (약 4일)
    h1_candles = UpbitData.candles(market, count=100, unit_minutes=60)
    # 3) 일봉 30개
    daily = UpbitData.daily_candles(market, count=30)

    if not h1_candles or not daily:
        return {"error": "캔들 데이터 부족"}

    # 4) 기술 지표 계산
    closes = [c["trade_price"] for c in h1_candles[::-1]]  # 시간순
    daily_closes = [d["trade_price"] for d in daily[::-1]]
    volumes_24h = sum(d["candle_acc_trade_price"] for d in daily[:7])  # 7일 평균
    avg_vol_7d = volumes_24h / 7

    # 단순 통계
    high_7d = max(c["high_price"] for c in daily[:7])
    low_7d = min(c["low_price"] for c in daily[:7])
    high_30d = max(c["high_price"] for c in daily)
    low_30d = min(c["low_price"] for c in daily)
    current = t["trade_price"]

    # 변화율
    ch_24h = t.get("signed_change_rate", 0) * 100
    ch_7d = (current / daily[6]["trade_price"] - 1) * 100 if len(daily) > 6 else 0
    ch_30d = (current / daily[-1]["trade_price"] - 1) * 100 if len(daily) > 25 else 0

    # 단순 RSI(14, 1h)
    def calc_rsi(prices, period=14):
        if len(prices) < period + 1:
            return 50
        gains, losses = [], []
        for i in range(1, len(prices)):
            d = prices[i] - prices[i - 1]
            gains.append(max(d, 0))
            losses.append(max(-d, 0))
        avg_g = sum(gains[-period:]) / period
        avg_l = sum(losses[-period:]) / period
        if avg_l == 0:
            return 100
        rs = avg_g / avg_l
        return 100 - (100 / (1 + rs))

    rsi_1h = calc_rsi(closes)
    rsi_daily = calc_rsi(daily_closes)

    # 위치 (52주 대신 30일 기준)
    position_pct = (current - low_30d) / (high_30d - low_30d) * 100 if high_30d > low_30d else 50

    # 5) 글로벌 컨텍스트
    fg = GlobalData.fear_greed()
    btc_d = GlobalData.btc_dominance()

    # 6) v4.3: 실시간 뉴스 + Google Search grounding
    coin_name_full = {
        "BTC": "Bitcoin", "ETH": "Ethereum", "XRP": "Ripple XRP",
        "SOL": "Solana", "ADA": "Cardano", "DOGE": "Dogecoin",
        "AVAX": "Avalanche", "SHIB": "Shiba Inu",
    }.get(symbol.upper(), symbol)

    tavily_news = fetch_tavily_news(f"{coin_name_full} cryptocurrency", days=3, max_results=5)
    news_section = ""
    if tavily_news:
        news_section = "\n## Recent News (Tavily, last 3 days)\n" + "\n".join(
            f"- {n['title']}\n  {n['content'][:200]}\n  source: {n['url']}"
            for n in tavily_news[:5]
        )

    grounded = {}
    grounded_section = ""
    sources_for_ui = []
    if _GROUNDED_OK:
        grounded = fetch_grounded_news(
            f"What are the latest news, price catalysts, and market events for {coin_name_full} ({symbol}) cryptocurrency in the past 7 days? Include any regulatory news, major partnerships, or technical developments.",
            max_chars=1200,
        )
        if grounded.get("text"):
            grounded_section = f"\n## Live Market Intelligence (Google Search, real-time)\n{grounded['text']}\n"
            sources_for_ui = grounded.get("sources", [])

    # v4.6.3: prompt parts 분리 — 외부 텍스트 절대 f-string 평가 안 됨
    JSON_TEMPLATE_COIN = '''
## Task
Provide comprehensive analysis in JSON. **All narrative fields MUST be in 한국어 (Korean).**
Use the Recent News and Live Market Intelligence above for catalysts and risks — be specific.

**중요한 분석 원칙 (v4.9 - 온체인/매크로/검증방법론 통합):**
1. **Valuation**: 시가총액 순위, 동종 L1 대비 평가. "저평가/적정/고평가" 명시.
2. **유즈케이스/네트워크**: 실제 채택, 활성 주소수, TVL.
3. **토크노믹스**: 공급량/인플레이션, 스테이킹, 락업.
4. **구체적 규제**: SEC 분류, ETF, 국가별. 추상적 금지.
5. **★ 시간 프레임 분리**: 단기/중기/장기 각각 outlook 평가.
6. **★ 시나리오 분석 (Bull/Base/Bear)**: 확률(합 1.0) + 가격 목표 + 트리거.
7. **★ 데이터 신선도**: Grounding과 불일치 시 명시.
8. **★ 추격 매수 자제**: 단기 과열 시 분할 매수 권장 톤.
9. **★ 온체인 정량 지표 (NEW v4.9)**: NVT Ratio, MVRV, SOPR, Funding Rate, Open Interest, 거래소 입출금, 활성 주소수. Grounding으로 최신 값 확보. 모르면 'N/A'.
10. **★ 매크로 시나리오 맵핑 (NEW v4.9)**: Bull/Base/Bear 각각에 Fed 금리·USD Index·M2·BTC ETF 자금흐름 가정 명시.
11. **★ 검증 방법론 6개 점수 (NEW v4.9)**: Stage(Weinstein)/Wyckoff/On-chain Cycle/Momentum+RS vs BTC/Sentiment(F&G+Funding)/Macro Liquidity 각각 0-10점 + 근거.
12. **★ 포지션 사이징 (NEW v4.9)**: R/R 비율 명시, 자본 대비 최대 비중 % (코인은 변동성 커서 2-4% 권장), 분할 매수 가격대, 손절 근거, Kelly 추정(매우 보수적).

{
  "summary_ko": "<한국어 2-3문장 핵심 요약>",
  "summary_en": "<English 2-3 sentence executive summary>",
  "current_setup": "<accumulation/distribution/breakout/breakdown/consolidation/range_bound/uptrend/downtrend>",
  "current_setup_ko": "<위 영문 상태를 한국어로 (매집/분산/돌파/붕괴/통합/박스권/상승추세/하락추세)>",
  "trend_1d": "<bullish/bearish/neutral>",
  "trend_1w": "<bullish/bearish/neutral>",
  "trend_1m": "<bullish/bearish/neutral>",
  "support_levels_krw": [<3 지지선>],
  "resistance_levels_krw": [<3 저항선>],
  "entry_zone_krw": [<low, high>],
  "stop_loss_krw": <price>,
  "target_1_krw": <conservative target>,
  "target_2_krw": <aggressive target>,
  "risk_reward_ratio": <number>,
  "key_risks_ko": ["<구체적 리스크1: 사건/규제>", "<리스크2>", "<리스크3>"],
  "key_catalysts_ko": ["<구체적 모멘텀1: 업그레이드/파트너십>", "<모멘텀2>"],
  "recommendation": "STRONG_BUY/BUY/HOLD/SELL/STRONG_SELL/AVOID",
  "confidence": 0.0-1.0,
  "time_horizon": "short/medium/long",
  "korean_advice": "<한국어 상세 분석 3-5문장, 구체적 가격 포함>",
  "valuation_ko": "<밸류에이션: 시가총액 순위, 동종 L1 대비 평가. 저평가/적정/고평가 명시. 2-3문장.>",
  "valuation_verdict": "undervalued/fair/overvalued",
  "valuation_peer_comparison": "<예: 시총 X위, ETH 대비 N% 수준>",
  "use_case_ko": "<주요 유즈케이스 + 채택 현황 + 활성 사용자/TVL. 3-4문장.>",
  "tokenomics_ko": "<공급량, 인플레이션, 스테이킹, 락업, 소각. 2-3문장.>",
  "regulatory_risk_ko": "<구체적 규제: SEC 분류, ETF, 국가별 동향. 3-4문장.>",
  "horizon_analysis": {
    "short_term_1w": {"outlook": "bullish/neutral/bearish", "summary_ko": "<기술적, 1-7일. 1-2문장.>", "confidence": 0.0},
    "medium_term_3m": {"outlook": "bullish/neutral/bearish", "summary_ko": "<프로토콜 업데이트/채택, 1-3개월. 1-2문장.>", "confidence": 0.0},
    "long_term_1y": {"outlook": "bullish/neutral/bearish", "summary_ko": "<생태계/규제/유즈케이스, 6개월~1년. 1-2문장.>", "confidence": 0.0}
  },
  "scenarios": {
    "bullish": {"probability": 0.0, "price_target_krw": 0, "triggers_ko": ["<상승 트리거1>", "<트리거2>"], "narrative_ko": "<낙관 1-2문장>"},
    "base": {"probability": 0.0, "price_range_krw": [0, 0], "triggers_ko": ["<중립 가정>"], "narrative_ko": "<기본 1-2문장>"},
    "bearish": {"probability": 0.0, "downside_target_krw": 0, "triggers_ko": ["<하락 트리거1>", "<트리거2>"], "narrative_ko": "<비관 1-2문장>"}
  },
  "data_freshness_note_ko": "<지표 최신성. Grounding과 불일치 시 라이브 우선. 1-2문장.>",
  "quantitative_metrics": {
    "onchain_nvt_ko": "<NVT Ratio (Network Value to Transactions). 평가 + 적정/고평가/저평가. Grounding 활용. 1-2문장.>",
    "onchain_mvrv_ko": "<MVRV (Market Value to Realized Value). 사이클 위치(매집/상승초기/과열/조정). 1-2문장.>",
    "sopr_ko": "<SOPR(Spent Output Profit Ratio) — 1 이상 차익실현 우세, 미만 손절 우세. 모르면 'N/A'.>",
    "funding_rate_ko": "<Perpetual Funding Rate (Binance/Bybit). 양수=롱 과열, 음수=숏 과열. 절대값 0.05%+ 위험. 1-2문장.>",
    "open_interest_ko": "<OI 변화율. 가격 상승+OI 증가=신규 자금, OI만 증가=레버리지 위험. 1-2문장.>",
    "exchange_flow_ko": "<거래소 입금/출금 동향. 출금=장기 보유 의지, 입금=매도 압력. 1-2문장.>",
    "active_addresses_ko": "<일일 활성 주소수 트렌드 + Network 성장성. 1-2문장.>"
  },
  "macro_assumptions": {
    "current_macro_phase_ko": "<현재 매크로 사이클 위치. 예: 'Fed 동결, USD Index 105 횡보, ETF 자금 약한 순유입'. 1-2문장.>",
    "bullish_macro_ko": "<Bull 매크로 가정. 예: 'Fed 인하 사이클 진입, USD Index 100 이하, M2 확장, BTC ETF 순유입 가속'. 1-2문장.>",
    "base_macro_ko": "<Base 매크로 가정. 1-2문장.>",
    "bearish_macro_ko": "<Bear 매크로 가정. 예: 'Fed 추가 인상, USD Index 110+, M2 위축, ETF 순유출, 알트 자금 BTC로 회귀'. 1-2문장.>"
  },
  "methodology_scores": {
    "stage_weinstein": {"score": 0, "notes_ko": "<Weinstein 30주 MA 기준 Stage 1-4 위치. 1-2문장.>"},
    "wyckoff": {"score": 0, "notes_ko": "<Wyckoff 누적/분산 phase + Spring/Upthrust 시그널. 1-2문장.>"},
    "onchain_cycle": {"score": 0, "notes_ko": "<NVT/MVRV/SOPR 종합한 온체인 사이클 점수. 1-2문장.>"},
    "momentum_rs_vs_btc": {"score": 0, "notes_ko": "<vs BTC 상대 강도 + BTC.D 트렌드 + 알트시즌 지표. 1-2문장.>"},
    "sentiment_funding": {"score": 0, "notes_ko": "<공포·탐욕 지수 + Funding Rate + 소셜 멘션. 1-2문장.>"},
    "macro_liquidity": {"score": 0, "notes_ko": "<M2 / USD Index / Fed 정책 / ETF 자금 흐름. 1-2문장.>"}
  },
  "position_sizing": {
    "risk_reward_ratio_explicit": <float, target_1 vs stop_loss 기준>,
    "max_position_pct_of_capital": "<권장 최대 자본 비중. 코인은 변동성 커서 2-4% 권장. 알트는 더 보수적.>",
    "scaling_in_plan_ko": "<분할 매수 가격대와 비중. 예: '90M에서 40%, 85M에서 30%, 80M 지지 확인 후 30%'. 3-4문장.>",
    "stop_loss_rationale_ko": "<손절 근거: 2% rule, ATR, 기술적 지지선 중 어느 기준인지 명시. 1-2문장.>",
    "kelly_fraction_estimate": <float, 0.0-0.15 범위로 매우 보수적 — 코인은 Quarter-Kelly 권장>
  }
}

Be specific with numbers, not vague.
'''

    # 안전: 일부 변수가 None일 수 있어 fallback
    fg_score = fg.get("score", "?") if fg else "?"
    fg_class = fg.get("classification", "?") if fg else "?"
    btc_d_pct = btc_d.get("btc_dominance", 0) if btc_d else 0
    btc_d_chg = btc_d.get("market_cap_change_24h", 0) if btc_d else 0

    header = f"""You are a senior crypto trading analyst. Provide a deep analysis of {symbol} on Korean Upbit.

## Current State
Price: {current:,.4f} KRW
24h Change: {ch_24h:+.2f}%
7d Change: {ch_7d:+.2f}%
30d Change: {ch_30d:+.2f}%
24h Volume: {t.get('acc_trade_price_24h', 0)/1e8:.0f}억 KRW
7d Avg Volume: {avg_vol_7d/1e8:.0f}억 KRW

## Price Ranges
7d:  {low_7d:,.4f} ~ {high_7d:,.4f}
30d: {low_30d:,.4f} ~ {high_30d:,.4f}
Position in 30d range: {position_pct:.0f}%

## Indicators
RSI (1h): {rsi_1h:.1f}
RSI (daily): {rsi_daily:.1f}

## Market Context
Fear & Greed: {fg_score} ({fg_class})
BTC Dominance: {btc_d_pct:.1f}% (24h: {btc_d_chg:+.2f}%)
"""

    prompt = header + (news_section or "") + (grounded_section or "") + JSON_TEMPLATE_COIN

    try:
        result = _LLM.call(prompt, model="gemini-2.5-pro", timeout=120)
        analysis = result["json"]
        logger.info(f"코인 분석 {symbol} 완료 (Pro, "
                    f"think={result['usage'].get('thoughtsTokenCount', 0)})")
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "market": market,
            "current_price": current,
            "raw_data": {
                "ch_24h": ch_24h, "ch_7d": ch_7d, "ch_30d": ch_30d,
                "rsi_1h": rsi_1h, "rsi_daily": rsi_daily,
                "position_30d_pct": position_pct,
                "high_30d": high_30d, "low_30d": low_30d,
                "fear_greed": fg, "btc_dominance": btc_d,
            },
            "analysis": analysis,
            "news": tavily_news,           # v4.3: Tavily 뉴스 5개
            "grounded": grounded,           # v4.3: Google Search 결과 + 출처
            "sources": sources_for_ui,      # v4.3: UI 표시용 출처 URL
            "usage": result.get("usage", {}),
        }
    except Exception as e:
        logger.error(f"코인 분석 {symbol} 실패: {e}")
        return {"error": str(e)[:200], "symbol": symbol}


if __name__ == "__main__":
    # CLI 테스트
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "movers"
    if cmd == "movers":
        print(json.dumps(fetch_top_movers(), indent=2, ensure_ascii=False))
    elif cmd == "recommend":
        print(json.dumps(recommend_coins(), indent=2, ensure_ascii=False))
    elif cmd == "analyze":
        sym = sys.argv[2] if len(sys.argv) > 2 else "BTC"
        print(json.dumps(analyze_coin(sym), indent=2, ensure_ascii=False))
