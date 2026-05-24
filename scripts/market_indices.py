#!/usr/bin/env python3
"""v5.0: 시장 지수/ETF/외환/원자재/코인 지수 시세 모듈.

investing.com 스타일의 카테고리별 종목 시세를 한 번에 fetch.

카테고리 (cat):
  - us_index   : S&P 500, NASDAQ 100, DOW, Russell 2000, VIX, DXY, 10Y
  - etf        : SPY, QQQ, VOO, IWM, ARKK, GLD, SLV, TLT, HYG, BITO, XLK, XLF, XLE
  - fx         : USD/KRW, EUR/USD, USD/JPY, GBP/USD, USD/CNY
  - commodity  : Gold, Silver, WTI, Brent, NatGas, Copper, Corn
  - crypto_idx : BTC, ETH, SOL, XRP, ADA, DOGE, AVAX, MATIC (Upbit KRW)
  - macro      : DXY, VIX, US10Y, US2Y, Gold (코인 매크로용)
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

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
}


def fetch_yf_quotes(symbols: list[tuple[str, str, str]]) -> list[dict]:
    """yfinance로 다수 종목 시세 일괄 조회.

    각 종목: (ticker, display_name_ko, category_ko)
    """
    if not _YF_OK or not symbols:
        return []
    tickers_str = " ".join(s[0] for s in symbols)
    try:
        data = yf.download(
            tickers_str, period="5d", interval="1d",
            group_by="ticker", progress=False, threads=True,
        )
    except Exception as e:
        logger.warning(f"yf.download 실패: {e}")
        return []

    out = []
    for ticker, name_ko, cat_ko in symbols:
        try:
            df = data[ticker] if ticker in data.columns.get_level_values(0).unique() else None
            if df is None or df.empty or len(df) < 2:
                out.append({
                    "ticker": ticker, "name": name_ko, "category": cat_ko,
                    "price": None, "change_pct": None, "change_abs": None,
                    "error": "no data",
                })
                continue
            last = df.iloc[-1]
            prev = df.iloc[-2]
            price = float(last["Close"])
            prev_close = float(prev["Close"])
            change_abs = price - prev_close
            change_pct = (price / prev_close - 1) * 100 if prev_close > 0 else 0
            out.append({
                "ticker": ticker, "name": name_ko, "category": cat_ko,
                "price": price,
                "change_pct": change_pct,
                "change_abs": change_abs,
                "prev_close": prev_close,
            })
        except Exception as e:
            out.append({
                "ticker": ticker, "name": name_ko, "category": cat_ko,
                "price": None, "change_pct": None, "error": str(e)[:80],
            })
    return out


def fetch_crypto_indices() -> list[dict]:
    """Upbit KRW 마켓 주요 코인 시세 (BTC, ETH, XRP, SOL, ADA, DOGE, AVAX, MATIC)."""
    majors = [
        ("KRW-BTC",   "비트코인",      "BTC"),
        ("KRW-ETH",   "이더리움",      "ETH"),
        ("KRW-SOL",   "솔라나",        "SOL"),
        ("KRW-XRP",   "리플",          "XRP"),
        ("KRW-ADA",   "카르다노",      "ADA"),
        ("KRW-DOGE",  "도지코인",      "DOGE"),
        ("KRW-AVAX",  "아발란체",      "AVAX"),
        ("KRW-MATIC", "폴리곤",        "MATIC"),
        ("KRW-DOT",   "폴카닷",        "DOT"),
        ("KRW-LINK",  "체인링크",      "LINK"),
        ("KRW-TRX",   "트론",          "TRX"),
        ("KRW-ATOM",  "코스모스",      "ATOM"),
    ]
    try:
        markets_str = ",".join(m[0] for m in majors)
        r = requests.get(
            "https://api.upbit.com/v1/ticker",
            params={"markets": markets_str},
            timeout=10,
        )
        tickers = {t["market"]: t for t in r.json()}
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
        out.append({
            "ticker": market, "name": name_ko, "category": cat,
            "price": float(t.get("trade_price", 0)),
            "change_pct": float(t.get("signed_change_rate", 0)) * 100,
            "change_abs": float(t.get("signed_change_price", 0)),
            "prev_close": float(t.get("prev_closing_price", 0)),
            "volume_24h_krw": float(t.get("acc_trade_price_24h", 0)),
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
