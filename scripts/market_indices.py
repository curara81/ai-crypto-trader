#!/usr/bin/env python3
"""v5.0.2: 시장 지수/ETF/외환/원자재/코인 지수 시세 모듈.

investing.com 스타일의 카테고리별 종목 시세를 한 번에 fetch.

카테고리 (cat):
  - us_index   : S&P 500, NASDAQ 100, DOW, Russell 2000, VIX, DXY, 10Y
  - etf        : SPY, QQQ, VOO, IWM, ARKK, GLD, SLV, TLT, HYG, BITO, XLK, XLF, XLE
  - fx         : USD/KRW, EUR/USD, USD/JPY, GBP/USD, USD/CNY
  - commodity  : Gold, Silver, WTI, Brent, NatGas, Copper, Corn
  - crypto_idx : BTC, ETH, SOL, XRP, ADA, DOGE, AVAX, MATIC (Upbit KRW)
  - macro      : DXY, VIX, US10Y, US2Y, Gold (코인 매크로용)

v5.0.2: NaN/Inf 안전 처리 + 개별 티커 격리 (한 종목 실패해도 전체 OK).
"""
import logging
import math
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)


def _safe_float(v) -> Optional[float]:
    """NaN/Inf/None을 None으로 변환 (JSON 직렬화 안전)."""
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None

try:
    import yfinance as yf
    _YF_OK = True
except ImportError:
    _YF_OK = False
    logger.warning("yfinance 미설치 — 지수 위젯 비활성")


# ─── 카테고리별 종목 정의 ────────────────
CATEGORIES = {
    "us_index": [
        ("^GSPC",     "S&P 500",         "지수"),
        ("^IXIC",     "NASDAQ Composite", "지수"),
        ("^NDX",      "NASDAQ 100",      "지수"),
        ("^DJI",      "Dow Jones",       "지수"),
        ("^RUT",      "Russell 2000",    "지수"),
        ("^VIX",      "VIX 변동성",      "변동성"),
        ("DX-Y.NYB",  "달러 지수 (DXY)", "외환"),
        ("^TNX",      "미국 10년물",     "채권"),
    ],
    "etf": [
        ("SPY",  "SPDR S&P 500",          "대형주"),
        ("QQQ",  "Invesco NASDAQ 100",    "테크"),
        ("VOO",  "Vanguard S&P 500",      "대형주"),
        ("IWM",  "iShares Russell 2000",  "소형주"),
        ("ARKK", "ARK Innovation",        "혁신"),
        ("XLK",  "Tech Select Sector",    "테크"),
        ("XLF",  "Financial Sector",      "금융"),
        ("XLE",  "Energy Sector",         "에너지"),
        ("XLV",  "Healthcare Sector",     "헬스"),
        ("GLD",  "SPDR Gold",             "금"),
        ("SLV",  "iShares Silver",        "은"),
        ("TLT",  "20+Y Treasury",         "장기채"),
        ("HYG",  "High Yield Bond",       "하이일드"),
        ("BITO", "Bitcoin Strategy ETF",  "비트코인"),
    ],
    "fx": [
        ("KRW=X",     "USD/KRW",  "원달러"),
        ("EURUSD=X",  "EUR/USD",  "유로"),
        ("JPY=X",     "USD/JPY",  "엔달러"),
        ("GBPUSD=X",  "GBP/USD",  "파운드"),
        ("CNY=X",     "USD/CNY",  "위안"),
        ("AUDUSD=X",  "AUD/USD",  "호주달러"),
        ("CHF=X",     "USD/CHF",  "스위스"),
        ("DX-Y.NYB",  "DXY",      "달러지수"),
    ],
    "commodity": [
        ("GC=F",  "금 (Gold)",            "귀금속"),
        ("SI=F",  "은 (Silver)",          "귀금속"),
        ("PL=F",  "백금 (Platinum)",      "귀금속"),
        ("HG=F",  "구리 (Copper)",        "산업금속"),
        ("CL=F",  "WTI 원유",             "에너지"),
        ("BZ=F",  "브렌트유",             "에너지"),
        ("NG=F",  "천연가스",             "에너지"),
        ("ZC=F",  "옥수수",               "농산물"),
        ("ZS=F",  "대두",                 "농산물"),
        ("ZW=F",  "밀",                   "농산물"),
    ],
    # 코인용: 매크로 자산 (코인과 상관관계 높음)
    "macro": [
        ("DX-Y.NYB",  "달러 지수 (DXY)",  "외환"),
        ("^VIX",      "VIX 변동성",       "변동성"),
        ("^TNX",      "미국 10년물",      "채권"),
        ("^IRX",      "미국 3개월물",     "채권"),
        ("GC=F",      "금",               "귀금속"),
        ("CL=F",      "WTI 원유",         "에너지"),
        ("^GSPC",     "S&P 500",          "지수"),
        ("^IXIC",     "NASDAQ",           "지수"),
    ],
    # v5.2.1: 국내 시장 — ^KS11/^KQ11이 yfinance에서 unstable → ETF로 대체
    "kr_index": [
        ("069500.KS",  "KODEX 200 (코스피 추종)",     "지수ETF"),
        ("229200.KS",  "KODEX 코스닥150",            "지수ETF"),
        ("102110.KS",  "TIGER 200",                  "지수ETF"),
        ("122630.KS",  "KODEX 레버리지 (코스피 2X)", "레버리지"),
        ("114800.KS",  "KODEX 인버스",               "인버스"),
        ("251340.KS",  "KODEX 코스닥150 선물인버스",  "인버스"),
        ("091160.KS",  "KODEX 반도체",               "섹터ETF"),
        ("305720.KS",  "KODEX 2차전지산업",          "섹터ETF"),
        ("091170.KS",  "KODEX 은행",                 "섹터ETF"),
        ("091180.KS",  "KODEX 자동차",               "섹터ETF"),
        ("266390.KS",  "KODEX 헬스케어",             "섹터ETF"),
        ("261240.KS",  "KODEX WTI원유선물(H)",       "원자재ETF"),
    ],
    # v5.2.1: 국내 외환·환율 — DXY/^TNX 제거 후 더 안정적 ticker로
    "kr_fx": [
        ("KRW=X",     "USD/KRW",      "원달러"),
        ("EURKRW=X",  "EUR/KRW",      "유로/원"),
        ("JPYKRW=X",  "JPY/KRW",      "엔/원"),
        ("CNYKRW=X",  "CNY/KRW",      "위안/원"),
        ("GBPKRW=X",  "GBP/KRW",      "파운드/원"),
        ("AUDKRW=X",  "AUD/KRW",      "호주달러/원"),
        ("GC=F",      "금 (USD)",     "귀금속"),
        ("CL=F",      "WTI 원유",     "에너지"),
    ],
}


def fetch_yf_quotes(symbols: list[tuple[str, str, str]]) -> list[dict]:
    """yfinance로 다수 종목 시세 일괄 조회. NaN/오류 격리.

    각 종목: (ticker, display_name_ko, category_ko)
    """
    if not _YF_OK or not symbols:
        return []
    tickers_str = " ".join(s[0] for s in symbols)
    try:
        data = yf.download(
            tickers_str, period="5d", interval="1d",
            group_by="ticker", progress=False, threads=True,
            auto_adjust=True,  # v5.0.2: 분할/배당 조정값
        )
    except Exception as e:
        logger.warning(f"yf.download 실패 (전체 폴백): {e}")
        # 폴백: 개별 종목씩 fetch (한 종목 망가져도 나머지 OK)
        return _fetch_yf_individual(symbols)

    out = []
    # 단일 vs 멀티 티커 시 데이터 구조 다름
    try:
        available = set(data.columns.get_level_values(0).unique()) if hasattr(data.columns, 'get_level_values') else set()
    except Exception:
        available = set()

    for ticker, name_ko, cat_ko in symbols:
        try:
            if ticker in available:
                df = data[ticker]
            else:
                # 멀티 인덱스 아닐 때 (단일 종목)
                df = data if len(symbols) == 1 else None
            if df is None or df.empty or len(df) < 2:
                out.append({
                    "ticker": ticker, "name": name_ko, "category": cat_ko,
                    "price": None, "change_pct": None, "change_abs": None,
                    "error": "no data",
                })
                continue
            # NaN 행 제거 후 마지막 2개
            df_valid = df.dropna(subset=["Close"])
            if len(df_valid) < 2:
                out.append({
                    "ticker": ticker, "name": name_ko, "category": cat_ko,
                    "price": None, "change_pct": None,
                    "error": "insufficient valid close prices",
                })
                continue
            last = df_valid.iloc[-1]
            prev = df_valid.iloc[-2]
            price = _safe_float(last["Close"])
            prev_close = _safe_float(prev["Close"])
            if price is None or prev_close is None or prev_close <= 0:
                out.append({
                    "ticker": ticker, "name": name_ko, "category": cat_ko,
                    "price": price, "change_pct": None,
                    "error": "bad price",
                })
                continue
            out.append({
                "ticker": ticker, "name": name_ko, "category": cat_ko,
                "price": price,
                "change_pct": _safe_float((price / prev_close - 1) * 100),
                "change_abs": _safe_float(price - prev_close),
                "prev_close": prev_close,
            })
        except Exception as e:
            logger.debug(f"{ticker} 처리 실패: {e}")
            out.append({
                "ticker": ticker, "name": name_ko, "category": cat_ko,
                "price": None, "change_pct": None, "error": str(e)[:80],
            })
    return out


def _fetch_yf_individual(symbols: list[tuple[str, str, str]]) -> list[dict]:
    """폴백: 개별 종목씩 fetch (전체 download 실패 시)."""
    out = []
    for ticker, name_ko, cat_ko in symbols:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d", interval="1d", auto_adjust=True)
            if hist.empty or len(hist) < 2:
                out.append({
                    "ticker": ticker, "name": name_ko, "category": cat_ko,
                    "price": None, "change_pct": None, "error": "no data",
                })
                continue
            hist = hist.dropna(subset=["Close"])
            if len(hist) < 2:
                out.append({
                    "ticker": ticker, "name": name_ko, "category": cat_ko,
                    "price": None, "change_pct": None, "error": "all nan",
                })
                continue
            price = _safe_float(hist["Close"].iloc[-1])
            prev_close = _safe_float(hist["Close"].iloc[-2])
            if price is None or prev_close is None or prev_close <= 0:
                out.append({
                    "ticker": ticker, "name": name_ko, "category": cat_ko,
                    "price": price, "change_pct": None,
                })
                continue
            out.append({
                "ticker": ticker, "name": name_ko, "category": cat_ko,
                "price": price,
                "change_pct": _safe_float((price / prev_close - 1) * 100),
                "change_abs": _safe_float(price - prev_close),
                "prev_close": prev_close,
            })
        except Exception as e:
            out.append({
                "ticker": ticker, "name": name_ko, "category": cat_ko,
                "price": None, "change_pct": None, "error": str(e)[:80],
            })
    return out


def fetch_crypto_indices() -> list[dict]:
    """Upbit KRW 마켓 주요 코인 시세. v5.0.5: 상장폐지 코드 자동 필터링."""
    # 후보 목록 (Upbit 상장 여부와 무관하게 정의)
    candidates = [
        ("KRW-BTC",    "비트코인",      "BTC"),
        ("KRW-ETH",    "이더리움",      "ETH"),
        ("KRW-SOL",    "솔라나",        "SOL"),
        ("KRW-XRP",    "리플",          "XRP"),
        ("KRW-ADA",    "카르다노",      "ADA"),
        ("KRW-DOGE",   "도지코인",      "DOGE"),
        ("KRW-AVAX",   "아발란체",      "AVAX"),
        ("KRW-POL",    "폴리곤 (POL)",  "POL"),    # MATIC → POL 변경
        ("KRW-MATIC",  "폴리곤 (구)",   "MATIC"),  # 일부 거래소만 잔존
        ("KRW-DOT",    "폴카닷",        "DOT"),
        ("KRW-LINK",   "체인링크",      "LINK"),
        ("KRW-TRX",    "트론",          "TRX"),
        ("KRW-ATOM",   "코스모스",      "ATOM"),
        ("KRW-NEAR",   "니어",          "NEAR"),
        ("KRW-SHIB",   "시바이누",      "SHIB"),
        ("KRW-SUI",    "수이",          "SUI"),
        ("KRW-APT",    "앱토스",        "APT"),
    ]
    # v5.0.5: 사전에 Upbit 상장 마켓 리스트 받아서 필터링
    try:
        rm = requests.get("https://api.upbit.com/v1/market/all", timeout=10)
        available = {m["market"] for m in rm.json() if m["market"].startswith("KRW-")}
    except Exception as e:
        logger.warning(f"Upbit market list 실패: {e}")
        available = set()

    majors = [m for m in candidates if not available or m[0] in available]
    if not majors:
        return []

    try:
        markets_str = ",".join(m[0] for m in majors)
        r = requests.get(
            "https://api.upbit.com/v1/ticker",
            params={"markets": markets_str},
            timeout=10,
        )
        data = r.json()
        if not isinstance(data, list):
            logger.warning(f"Upbit ticker 응답 예상 외: {str(data)[:100]}")
            return []
        tickers = {t["market"]: t for t in data}
    except Exception as e:
        logger.warning(f"Upbit ticker fetch 실패: {e}")
        return []

    out = []
    for market, name_ko, cat in majors:
        t = tickers.get(market)
        if not t:
            out.append({
                "ticker": market, "name": name_ko, "category": cat,
                "price": None, "change_pct": None, "error": "no data",
            })
            continue
        price = _safe_float(t.get("trade_price"))
        out.append({
            "ticker": market, "name": name_ko, "category": cat,
            "price": price,
            "change_pct": _safe_float(t.get("signed_change_rate", 0) * 100) if t.get("signed_change_rate") is not None else None,
            "change_abs": _safe_float(t.get("signed_change_price")),
            "prev_close": _safe_float(t.get("prev_closing_price")),
            "volume_24h_krw": _safe_float(t.get("acc_trade_price_24h")),
        })
    return out


def fetch_indices(cat: str) -> dict:
    """카테고리별 시세 fetch. PWA가 호출."""
    cat = cat.lower()

    if cat == "crypto_idx":
        items = fetch_crypto_indices()
    elif cat in CATEGORIES:
        items = fetch_yf_quotes(CATEGORIES[cat])
    else:
        return {"error": f"unknown category: {cat}",
                "valid": list(CATEGORIES.keys()) + ["crypto_idx"]}

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "category": cat,
        "items": items,
        "count": len([i for i in items if i.get("price") is not None]),
    }


if __name__ == "__main__":
    import json, sys
    cat = sys.argv[1] if len(sys.argv) > 1 else "us_index"
    print(json.dumps(fetch_indices(cat), indent=2, ensure_ascii=False))
