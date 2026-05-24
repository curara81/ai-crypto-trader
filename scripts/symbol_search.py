#!/usr/bin/env python3
"""v4.5: 종목명 → 티커 자동 검색.

코인:
  - "비트코인" → BTC, "이더리움" → ETH, "리플" → XRP
  - CoinGecko Search API + Upbit KRW 마켓 필터링
  - 한국어 별칭 추가 매핑

미국 주식:
  - "엔비디아" → NVDA, "테슬라" → TSLA, "애플" → AAPL
  - Yahoo Finance Search API
  - 한국어 흔한 별칭 추가 매핑
"""
import json
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ─── 한국어 → 티커 별칭 (CoinGecko/Yahoo가 못 잡는 것 보완) ──
COIN_ALIASES_KO = {
    "비트코인": "BTC", "비트": "BTC",
    "이더리움": "ETH", "이더": "ETH",
    "리플": "XRP",
    "솔라나": "SOL",
    "에이다": "ADA", "카르다노": "ADA",
    "도지": "DOGE", "도지코인": "DOGE",
    "시바": "SHIB", "시바이누": "SHIB",
    "아발란체": "AVAX",
    "폴리곤": "MATIC", "매틱": "MATIC",
    "체인링크": "LINK", "링크": "LINK",
    "유니스왑": "UNI",
    "라이트코인": "LTC",
    "비트코인캐시": "BCH",
    "스텔라": "XLM",
    "트론": "TRX",
    "코스모스": "ATOM", "아톰": "ATOM",
    "폴카닷": "DOT", "도트": "DOT",
}

STOCK_ALIASES_KO = {
    # 빅테크
    "엔비디아": "NVDA", "엔비": "NVDA",
    "테슬라": "TSLA", "테슬": "TSLA",
    "애플": "AAPL",
    "마이크로소프트": "MSFT", "마소": "MSFT",
    "구글": "GOOGL", "알파벳": "GOOGL",
    "아마존": "AMZN",
    "메타": "META", "페이스북": "META",
    "에이엠디": "AMD",
    "넷플릭스": "NFLX",
    "인텔": "INTC",
    "어도비": "ADBE",
    "오라클": "ORCL",
    "퀄컴": "QCOM",
    "시스코": "CSCO",
    "세일즈포스": "CRM",
    "브로드컴": "AVGO",
    # 금융
    "제이피모건": "JPM", "JP모건": "JPM",
    "비자": "V",
    "마스터카드": "MA",
    # 크립토 관련
    "코인베이스": "COIN",
    "마이크로스트래티지": "MSTR", "마스트": "MSTR",
    # 핫픽
    "팔란티어": "PLTR",
    "쇼피파이": "SHOP",
    "스노우플레이크": "SNOW",
    "로블록스": "RBLX",
    "로쿠": "ROKU",
    "우버": "UBER",
    "에어비앤비": "ABNB",
    # 한국인 선호
    "디즈니": "DIS",
    "나이키": "NKE",
    "스타벅스": "SBUX",
    "코카콜라": "KO",
    "월마트": "WMT",
    "존슨앤존슨": "JNJ",
    # 헬스/바이오
    "일라이릴리": "LLY", "릴리": "LLY",
    "화이자": "PFE",
    "모더나": "MRNA",
    # 한국주 (KIS)
    "룰루레몬": "LULU",
}


# ─── 코인 검색 ─────────────────────────────────
_UPBIT_KRW_CACHE: Optional[list[str]] = None
_UPBIT_CACHE_TS: float = 0


def get_upbit_krw_markets() -> list[str]:
    """Upbit KRW 마켓 코인 목록 (캐시 1시간)."""
    global _UPBIT_KRW_CACHE, _UPBIT_CACHE_TS
    now = time.time()
    if _UPBIT_KRW_CACHE and (now - _UPBIT_CACHE_TS) < 3600:
        return _UPBIT_KRW_CACHE
    try:
        r = requests.get("https://api.upbit.com/v1/market/all", timeout=10)
        markets = [m["market"].replace("KRW-", "") for m in r.json() if m["market"].startswith("KRW-")]
        _UPBIT_KRW_CACHE = markets
        _UPBIT_CACHE_TS = now
        return markets
    except Exception:
        return _UPBIT_KRW_CACHE or []


def search_coins(query: str, limit: int = 8) -> list[dict]:
    """코인 검색 — 한국어 별칭 + CoinGecko + Upbit 가용성 체크.

    Returns: [{symbol, name, name_ko, available_on_upbit}, ...]
    """
    q = query.strip()
    if not q:
        return []

    upbit_markets = set(get_upbit_krw_markets())
    q_lower = q.lower()
    results = []
    seen_symbols = set()

    # 1) 한국어 별칭 우선 매칭
    for ko_name, sym in COIN_ALIASES_KO.items():
        if q in ko_name or ko_name in q:
            if sym not in seen_symbols:
                results.append({
                    "symbol": sym,
                    "name": sym,
                    "name_ko": ko_name,
                    "available_on_upbit": sym in upbit_markets,
                })
                seen_symbols.add(sym)

    # 2) 이미 티커처럼 보이면 직접 매칭 (ASCII 영문 + 숫자만)
    q_upper = q.upper()
    is_ticker_like = (
        2 <= len(q_upper) <= 7
        and all(c.isascii() and (c.isalnum()) for c in q_upper)
    )
    if is_ticker_like and q_upper not in seen_symbols and q_upper in upbit_markets:
        results.append({
            "symbol": q_upper,
            "name": q_upper,
            "name_ko": "",
            "available_on_upbit": True,
        })
        seen_symbols.add(q_upper)

    # 3) CoinGecko Search API
    if len(results) < limit:
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/search",
                params={"query": q},
                timeout=8,
            )
            coins = r.json().get("coins", [])[:limit]
            for c in coins:
                sym = c.get("symbol", "").upper()
                if sym and sym not in seen_symbols:
                    results.append({
                        "symbol": sym,
                        "name": c.get("name", sym),
                        "name_ko": "",
                        "available_on_upbit": sym in upbit_markets,
                        "market_cap_rank": c.get("market_cap_rank"),
                    })
                    seen_symbols.add(sym)
                    if len(results) >= limit:
                        break
        except Exception as e:
            logger.warning(f"CoinGecko search failed: {e}")

    # 4) Upbit에서 거래 가능한 것 우선 정렬
    results.sort(key=lambda x: (not x["available_on_upbit"], x.get("market_cap_rank") or 9999))
    return results[:limit]


# ─── 주식 검색 ─────────────────────────────────
def search_stocks(query: str, limit: int = 8) -> list[dict]:
    """미국 주식 검색 — 한국어 별칭 + Yahoo Finance Search.

    Returns: [{symbol, name, exchange, type}, ...]
    """
    q = query.strip()
    if not q:
        return []

    results = []
    seen_symbols = set()

    # 1) 한국어 별칭 우선
    for ko_name, sym in STOCK_ALIASES_KO.items():
        if q in ko_name or ko_name in q:
            if sym not in seen_symbols:
                results.append({
                    "symbol": sym,
                    "name": sym,
                    "name_ko": ko_name,
                    "exchange": "US",
                    "type": "EQUITY",
                })
                seen_symbols.add(sym)

    # 2) 이미 티커처럼 보이면 우선 (ASCII 영문 1-5자)
    q_upper = q.upper()
    cleaned = q_upper.replace("-", "").replace(".", "")
    is_ticker_like = (
        1 <= len(cleaned) <= 5
        and all(c.isascii() and c.isalpha() for c in cleaned)
    )
    if is_ticker_like and q_upper not in seen_symbols:
        results.append({
            "symbol": q_upper,
            "name": q_upper,
            "name_ko": "",
            "exchange": "US",
            "type": "EQUITY",
        })
        seen_symbols.add(q_upper)

    # 3) Yahoo Finance Search API
    if len(results) < limit:
        try:
            r = requests.get(
                "https://query2.finance.yahoo.com/v1/finance/search",
                params={"q": q, "quotesCount": limit, "newsCount": 0},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
            quotes = r.json().get("quotes", [])[:limit]
            for x in quotes:
                sym = x.get("symbol", "")
                # 미국 주식만 (NASDAQ, NYSE 등)
                exchange = x.get("exchange", "")
                if sym and sym not in seen_symbols and exchange in ("NMS", "NYQ", "NGM", "ASE", "PCX"):
                    results.append({
                        "symbol": sym,
                        "name": x.get("longname") or x.get("shortname", sym),
                        "name_ko": "",
                        "exchange": exchange,
                        "type": x.get("quoteType", "?"),
                    })
                    seen_symbols.add(sym)
                    if len(results) >= limit:
                        break
        except Exception as e:
            logger.warning(f"Yahoo search failed: {e}")

    return results[:limit]


if __name__ == "__main__":
    import sys
    market = sys.argv[1] if len(sys.argv) > 1 else "coin"
    q = sys.argv[2] if len(sys.argv) > 2 else "엔비디아"
    if market == "coin":
        print(json.dumps(search_coins(q), indent=2, ensure_ascii=False))
    else:
        print(json.dumps(search_stocks(q), indent=2, ensure_ascii=False))
