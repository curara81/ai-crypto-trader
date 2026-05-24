#!/usr/bin/env python3
"""v4.1: 외부 데이터 자동 수집 — 초기 학습 데이터셋 구축.

수집 대상:
  - CoinGecko: 글로벌 가격/시총/거래량 (top 100 coins)
  - alternative.me: Fear & Greed 일별 시계열
  - Upbit: 모든 KRW 마켓 24h 통계 + 시간봉 캔들 (상위 20개)
  - CoinGecko Trending: 24h 트렌딩 검색어

저장:
  ~/trading/freqtrade_userdata/data_collection/
    market_snapshots.jsonl   - 5분마다 시장 전체 스냅샷
    upbit_movers.jsonl       - 5분마다 상승률 변화
    fear_greed_daily.jsonl   - 일별 F&G
    trending_hourly.jsonl    - 시간별 트렌딩

목적:
  1. ML 모델 학습 데이터 누적 (수개월간)
  2. 분석 시점에 과거 비교 컨텍스트 제공
  3. BigQuery 적재 준비 (v5.0 계획)
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

TRADING_ROOT = Path(os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading")))
DATA_DIR = TRADING_ROOT / "freqtrade_userdata" / "data_collection"
DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("data_collector")


def append_jsonl(path: Path, obj: dict) -> None:
    """JSONL 끝에 한 줄 추가 + 100MB 도달 시 로테이션."""
    try:
        if path.exists() and path.stat().st_size > 100 * 1024 * 1024:
            for i in range(2, 0, -1):
                src = path.with_suffix(f".{i}.jsonl")
                if src.exists():
                    src.rename(path.with_suffix(f".{i + 1}.jsonl"))
            path.rename(path.with_suffix(".1.jsonl"))
        with open(path, "a") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning(f"append_jsonl 실패 ({path.name}): {e}")


# ─── 수집기 ──────────────────────────────────
def collect_upbit_snapshot() -> dict:
    """Upbit 전 KRW 마켓 5분 스냅샷."""
    try:
        r = requests.get("https://api.upbit.com/v1/market/all", timeout=10)
        markets = [m["market"] for m in r.json() if m["market"].startswith("KRW-")]
        # 100개씩 ticker 호출
        all_tickers = []
        for i in range(0, len(markets), 100):
            chunk = ",".join(markets[i:i+100])
            r = requests.get("https://api.upbit.com/v1/ticker",
                            params={"markets": chunk}, timeout=10)
            all_tickers.extend(r.json())

        snapshot = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "market_count": len(all_tickers),
            "total_volume_krw": sum(t.get("acc_trade_price_24h", 0) for t in all_tickers),
            # 상위 30개만 상세 저장 (스토리지 절약)
            "top_30": sorted(
                [{
                    "s": t["market"].replace("KRW-", ""),
                    "p": t.get("trade_price", 0),
                    "c": t.get("signed_change_rate", 0) * 100,
                    "v": t.get("acc_trade_price_24h", 0) / 1e8,
                } for t in all_tickers],
                key=lambda x: -x["v"],
            )[:30],
        }
        return snapshot
    except Exception as e:
        logger.warning(f"Upbit 스냅샷 실패: {e}")
        return {}


def collect_coingecko_global() -> dict:
    """CoinGecko 글로벌 시장 데이터."""
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=10)
        d = r.json().get("data", {})
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "total_market_cap_usd": d.get("total_market_cap", {}).get("usd", 0),
            "total_volume_usd": d.get("total_volume", {}).get("usd", 0),
            "btc_dominance": d.get("market_cap_percentage", {}).get("btc", 0),
            "eth_dominance": d.get("market_cap_percentage", {}).get("eth", 0),
            "active_cryptocurrencies": d.get("active_cryptocurrencies", 0),
            "markets": d.get("markets", 0),
        }
    except Exception as e:
        logger.warning(f"CoinGecko global 실패: {e}")
        return {}


def collect_fear_greed() -> dict:
    """alternative.me Fear & Greed."""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        d = r.json().get("data", [{}])[0]
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "score": int(d.get("value", 0)),
            "classification": d.get("value_classification", "?"),
            "timestamp_source": d.get("timestamp", ""),
        }
    except Exception as e:
        logger.warning(f"Fear & Greed 실패: {e}")
        return {}


def collect_coingecko_trending() -> dict:
    """CoinGecko 트렌딩 (24h 인기 검색어)."""
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
        d = r.json()
        coins = [
            {
                "rank": i,
                "id": c["item"]["id"],
                "symbol": c["item"]["symbol"],
                "name": c["item"]["name"],
                "market_cap_rank": c["item"].get("market_cap_rank"),
                "score": c["item"].get("score", 0),
            }
            for i, c in enumerate(d.get("coins", [])[:10])
        ]
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "trending_coins": coins,
        }
    except Exception as e:
        logger.warning(f"Trending 실패: {e}")
        return {}


# ─── 메인 ────────────────────────────────────
def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    if mode in ("all", "snapshot"):
        s = collect_upbit_snapshot()
        if s:
            append_jsonl(DATA_DIR / "market_snapshots.jsonl", s)
            logger.info(f"Upbit 스냅샷 저장: {s['market_count']}개 마켓, "
                       f"총거래 {s['total_volume_krw']/1e12:.1f}T KRW")

    if mode in ("all", "global"):
        g = collect_coingecko_global()
        if g:
            append_jsonl(DATA_DIR / "global_market.jsonl", g)
            logger.info(f"Global: BTC.D {g['btc_dominance']:.1f}%, "
                       f"시총 ${g['total_market_cap_usd']/1e12:.2f}T")

    if mode in ("all", "fg"):
        f = collect_fear_greed()
        if f:
            append_jsonl(DATA_DIR / "fear_greed_daily.jsonl", f)
            logger.info(f"F&G: {f['score']} ({f['classification']})")

    if mode in ("all", "trending"):
        t = collect_coingecko_trending()
        if t:
            append_jsonl(DATA_DIR / "trending_hourly.jsonl", t)
            logger.info(f"Trending {len(t['trending_coins'])}개")

    return 0


if __name__ == "__main__":
    sys.exit(main())
