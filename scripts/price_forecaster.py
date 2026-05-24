#!/usr/bin/env python3
"""v3.9: 가격 예측 검증 레이어.

원래 계획: TimesFM (Google 시계열 foundation model)
실제 구현: Gemini 2.5 Pro forecast (TimesFM이 Python 3.12 미지원 + 큰 의존성)

목적:
  Gemini Flash가 매수 결정하면 → Gemini Pro에게 별도로
  "이 코인 12시간 후 오를 가능성?" 검증 질문 → 동의 시에만 매수 진입

장점:
  - 추가 의존성 0 (이미 Vertex AI 설정됨)
  - Pro 모델은 reasoning 강해서 텍스트 시계열 예측 유의미
  - GCP 크레딧 사용 (목표 부합)
  - 1초 응답
"""
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from google import genai as _genai
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False


class PriceForecaster:
    """Vertex AI Gemini Pro로 가격 방향 예측."""

    def __init__(self, project: Optional[str] = None, location: Optional[str] = None,
                 model: str = "gemini-2.5-flash", cache_ttl: int = 600):
        self.project = project or os.environ.get("GCP_PROJECT", "timesfm-personal-lab")
        self.location = location or os.environ.get("GCP_LOCATION", "us-central1")
        self.model = model
        self.cache_ttl = cache_ttl
        self._cache: dict = {}  # {pair: {"forecast": ..., "ts": ...}}
        self._client = None
        if _AVAILABLE:
            try:
                self._client = _genai.Client(vertexai=True, project=self.project, location=self.location)
            except Exception as e:
                logger.warning(f"PriceForecaster Vertex 초기화 실패: {e}")
                self._client = None

    def predict(self, pair: str, recent_candles: list[dict],
                horizon_hours: int = 12) -> dict:
        """가격 방향 예측.

        Args:
            pair: "BTC/KRW" 등
            recent_candles: 최근 OHLCV 캔들 (50-100개 권장)
            horizon_hours: 예측 시간 (1-24)

        Returns:
            {
                "direction": "up"|"down"|"neutral",
                "confidence": 0.0-1.0,
                "expected_change_pct": float,
                "reasoning": str,
                "agrees_with_buy": bool,  # buy 결정과 동의?
                "error": Optional[str],
            }
        """
        if not self._client:
            return {"direction": "neutral", "confidence": 0, "error": "no_client"}

        # 캐시 확인
        now = time.time()
        cached = self._cache.get(pair)
        if cached and (now - cached["ts"]) < self.cache_ttl:
            return cached["forecast"]

        if len(recent_candles) < 20:
            return {"direction": "neutral", "confidence": 0, "error": "insufficient_data"}

        # 최근 50개만 사용 (토큰 절약)
        candles = recent_candles[-50:]
        first = candles[0]
        last = candles[-1]
        current_price = last["close"]
        change_pct = (last["close"] / first["close"] - 1) * 100

        # 가격 시퀀스 (정규화)
        prices = [c["close"] for c in candles]
        max_p, min_p = max(prices), min(prices)
        range_pct = (max_p - min_p) / current_price * 100

        # 볼륨
        volumes = [c.get("volume", 0) for c in candles]
        avg_vol = sum(volumes) / len(volumes) if volumes else 0
        recent_vol = volumes[-1] if volumes else 0
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1

        # 단순화한 가격 시퀀스 (15개 샘플)
        sample_indices = [int(i * (len(candles)-1) / 14) for i in range(15)]
        price_samples = [candles[i]["close"] for i in sample_indices]
        seq_str = " → ".join(f"{p:.4f}" for p in price_samples)

        prompt = f"""You are a quantitative time-series analyst.

PAIR: {pair}
CURRENT PRICE: {current_price:.6f}
RECENT WINDOW: last 50 × 5min candles ({len(candles)*5} min total)

PRICE SEQUENCE (15 samples evenly spaced):
{seq_str}

STATISTICS:
- Total change: {change_pct:+.2f}%
- Range: {range_pct:.2f}% ({min_p:.4f} to {max_p:.4f})
- Recent volume: {vol_ratio:.2f}x average

TASK: Predict price direction over the NEXT {horizon_hours} HOURS.

Respond in JSON:
{{"direction": "up" or "down" or "neutral",
  "confidence": 0.0-1.0,
  "expected_change_pct": <signed float, e.g., +1.5 or -0.8>,
  "reasoning": "<one sentence>"}}

Be calibrated. If pattern is unclear, say neutral with low confidence."""

        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={
                    "temperature": 0.1,
                    "max_output_tokens": 1500,  # v3.9.1: thinking 토큰 여유
                    "response_mime_type": "application/json",
                },
            )
            text = (response.text or "").strip()
            if not text:
                raise ValueError("empty response")
            # 마크다운 펜스 제거
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            # 첫 { 부터 마지막 } 까지만 추출 (방어적 파싱)
            if "{" in text and "}" in text:
                text = text[text.index("{"): text.rindex("}") + 1]
            try:
                result = json.loads(text)
            except json.JSONDecodeError as je:
                logger.warning(f"Forecast {pair} JSON 파싱 실패 ({je}), raw[:200]={text[:200]!r}")
                return {"direction": "neutral", "confidence": 0, "error": "json_parse"}
            forecast = {
                "direction": result.get("direction", "neutral").lower(),
                "confidence": max(0.0, min(1.0, float(result.get("confidence", 0)))),
                "expected_change_pct": float(result.get("expected_change_pct", 0)),
                "reasoning": result.get("reasoning", "")[:200],
                "error": None,
            }
            self._cache[pair] = {"forecast": forecast, "ts": now}
            logger.info(
                f"Forecast {pair}: {forecast['direction']} "
                f"({forecast['expected_change_pct']:+.2f}% in {horizon_hours}h, "
                f"conf={forecast['confidence']:.2f})"
            )
            return forecast
        except Exception as e:
            logger.warning(f"Forecast {pair} 실패: {e}")
            return {"direction": "neutral", "confidence": 0, "error": str(e)[:100]}

    def agrees_with_buy(self, forecast: dict, min_confidence: float = 0.5) -> bool:
        """매수 결정에 forecast가 동의하는가?"""
        if forecast.get("error"):
            return True  # 예측 실패 시 매수 차단 안 함 (fail-open)
        direction = forecast.get("direction", "neutral")
        conf = forecast.get("confidence", 0)
        # up 방향이고 신뢰도 충분, 또는 neutral은 통과
        if direction == "up":
            return True
        if direction == "neutral":
            return True
        # down 인데 신뢰도 높으면 차단
        if direction == "down" and conf >= min_confidence:
            return False
        return True
