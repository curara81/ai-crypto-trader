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
- thesis (1-2 sentences why)
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
  {{"symbol": "...", "thesis": "...", "risk_level": "...", "time_horizon": "...", "confidence": 0.0-1.0}},
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

    # 6) Gemini Pro에 분석 요청
    prompt = f"""You are a senior crypto trading analyst. Provide a deep analysis of {symbol} on Korean Upbit.

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
Fear & Greed: {fg['score'] if fg else '?'} ({fg['classification'] if fg else '?'})
BTC Dominance: {btc_d['btc_dominance']:.1f}% (24h: {btc_d['market_cap_change_24h']:+.2f}%) {'' if btc_d else 'n/a'}

## Task
Provide comprehensive analysis in JSON:

{{
  "summary": "<2-3 sentence executive summary in Korean>",
  "current_setup": "<accumulation/distribution/breakout/breakdown/consolidation/etc>",
  "trend_1d": "<bullish/bearish/neutral>",
  "trend_1w": "<bullish/bearish/neutral>",
  "trend_1m": "<bullish/bearish/neutral>",
  "support_levels_krw": [<3 key support prices>],
  "resistance_levels_krw": [<3 key resistance prices>],
  "entry_zone_krw": [<low, high>],
  "stop_loss_krw": <price>,
  "target_1_krw": <conservative target>,
  "target_2_krw": <aggressive target>,
  "risk_reward_ratio": <number>,
  "key_risks": ["<risk1>", "<risk2>", "<risk3>"],
  "key_catalysts": ["<catalyst1>", "<catalyst2>"],
  "recommendation": "STRONG_BUY/BUY/HOLD/SELL/STRONG_SELL/AVOID",
  "confidence": 0.0-1.0,
  "time_horizon": "short/medium/long",
  "korean_advice": "<detailed Korean reasoning, 3-5 sentences>"
}}

Be specific with numbers, not vague."""

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
