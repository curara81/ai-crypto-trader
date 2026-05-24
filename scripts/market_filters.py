#!/usr/bin/env python3
"""시장 필터 — v3.4

자료 기반 검증된 4개 필터:
  1. FearGreedFilter   — alternative.me API, Extreme Greed 시 매수 차단
  2. SpreadFilter      — 호가창 spread > 임계 시 차단 (슬리피지 누적 방지)
  3. RegimeGate        — ADX 기반 하드 게이트 (전환기 02-06 KST 차단)
  4. TimeWindowFilter  — 한국 새벽 저유동성 시간대 차단

각 필터는 `should_block(context) -> tuple[bool, str]` 반환:
  (True, "사유") = 매수 차단
  (False, "")   = 통과
"""
import json
import logging
import os
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)


class FearGreedFilter:
    """alternative.me Fear & Greed Index 게이트.

    무료 API, 일 1회 갱신. 캐시 6시간.

    규칙:
      - score >= 75 (Extreme Greed): 매수 차단 (천장 가능성)
      - score <= 25 (Extreme Fear):  매수 우대 (역사적 랠리 선행)
      - 25 < score < 75:             통과
    """

    URL = "https://api.alternative.me/fng/?limit=1"
    CACHE_TTL = 6 * 3600  # 6시간

    def __init__(self, max_greed: int = 75):
        self.max_greed = max_greed
        self._cache: Optional[dict] = None
        self._cache_ts: float = 0

    def get_score(self) -> Optional[int]:
        now = time.time()
        if self._cache and (now - self._cache_ts) < self.CACHE_TTL:
            return self._cache.get("value")
        try:
            resp = requests.get(self.URL, timeout=5)
            data = resp.json().get("data", [])
            if not data:
                return None
            entry = data[0]
            self._cache = {
                "value": int(entry["value"]),
                "classification": entry.get("value_classification", ""),
            }
            self._cache_ts = now
            logger.info(
                f"Fear&Greed: {self._cache['value']} ({self._cache['classification']})"
            )
            return self._cache["value"]
        except Exception as e:
            logger.warning(f"Fear&Greed API 실패 (필터 비활성): {e}")
            return None

    def should_block(self, **_kwargs) -> tuple[bool, str]:
        score = self.get_score()
        if score is None:
            return (False, "")  # API 실패 시 통과 (보수적이 아닌 fail-open)
        if score >= self.max_greed:
            return (True, f"Fear&Greed {score} >= {self.max_greed} (Extreme Greed)")
        return (False, "")

    def is_extreme_fear(self) -> bool:
        """우대 신호용 — 극공포 = 매수 기회."""
        score = self.get_score()
        return score is not None and score <= 25


class SpreadFilter:
    """호가창 spread 게이트.

    spread = (best_ask - best_bid) / best_bid × 100 (%)
    누적 슬리피지가 평균 -0.15% 손실의 주요 원인이므로
    spread가 큰 순간은 매수 보류.
    """

    def __init__(self, max_spread_pct: float = 0.15):
        self.max = max_spread_pct

    def should_block(self, orderbook: Optional[dict] = None, **_kwargs) -> tuple[bool, str]:
        """orderbook: {"best_bid": float, "best_ask": float}"""
        if not orderbook:
            return (False, "")  # 데이터 없으면 통과
        try:
            bid = float(orderbook["best_bid"])
            ask = float(orderbook["best_ask"])
            if bid <= 0:
                return (False, "")
            spread = (ask - bid) / bid * 100
            if spread > self.max:
                return (True, f"Spread {spread:.3f}% > {self.max}%")
            return (False, "")
        except (KeyError, ValueError, TypeError):
            return (False, "")


class RegimeGate:
    """ADX 기반 시장 레짐 하드 게이트.

    자료 (Vantixs, QuantVPS): 레짐 분리 = 단일 임팩트 최대.
    동호님 봇은 이미 regime 분류 있지만 LLM 권고 수준 → 하드 게이트로 격상.

    규칙:
      ADX > 25 (강한 추세): 평균회귀 진입 차단 (반대 방향 매수 금지)
      ADX < 20 (횡보):     트렌드추종 진입 차단 (잘못된 돌파 매수 금지)
      ADX 20-25 (전환기):   ★ 모든 매수 차단 ★ (가장 위험)
    """

    def __init__(self, ranging_max: float = 20.0, trending_min: float = 25.0):
        self.ranging_max = ranging_max
        self.trending_min = trending_min

    def should_block(
        self,
        adx: Optional[float] = None,
        signal_type: str = "unknown",
        **_kwargs,
    ) -> tuple[bool, str]:
        """signal_type: 'trend' | 'mean_reversion' | 'unknown'

        unknown 으로 들어오는 일반 AI 매수도 전환기엔 차단.
        """
        if adx is None:
            return (False, "")
        # 전환기: 모두 차단
        if self.ranging_max <= adx <= self.trending_min:
            return (True, f"전환기 (ADX={adx:.0f}, 위험)")
        # 추세장에서 평균회귀: 차단
        if adx > self.trending_min and signal_type == "mean_reversion":
            return (True, f"추세장(ADX={adx:.0f})에 평균회귀 부적합")
        # 횡보장에서 트렌드추종: 차단
        if adx < self.ranging_max and signal_type == "trend":
            return (True, f"횡보장(ADX={adx:.0f})에 트렌드추종 부적합")
        return (False, "")


class TimeWindowFilter:
    """한국 새벽 저유동성 시간대 차단.

    Upbit 한국 유저 기반 → KST 02-06시 거래량 급감 → 슬리피지 큼.
    """

    def __init__(self, block_start_hour: int = 2, block_end_hour: int = 6, tz: str = "Asia/Seoul"):
        self.start = block_start_hour
        self.end = block_end_hour
        self.tz = ZoneInfo(tz)

    def should_block(self, **_kwargs) -> tuple[bool, str]:
        now = datetime.now(self.tz)
        hour = now.hour
        # start ~ end (end 미포함) 차단
        if self.start <= hour < self.end:
            return (True, f"저유동성 시간대 (KST {hour:02d}시)")
        return (False, "")


class FilterChain:
    """4개 필터를 순차 체크. 하나라도 차단이면 전체 차단."""

    def __init__(self, filters: list):
        self.filters = filters

    def check(self, **context) -> tuple[bool, list[str]]:
        """모든 필터 체크.

        Returns: (blocked, reasons[])
          blocked = True 면 어디서 막혔는지 모든 사유 반환
        """
        reasons = []
        for f in self.filters:
            blocked, reason = f.should_block(**context)
            if blocked:
                reasons.append(reason)
        return (len(reasons) > 0, reasons)
