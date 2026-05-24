"""MLSignalEngine smoke tests — 모델 없이도 안전하게 동작하는지 검증."""
import math
import random

import pytest


def _make_candles(n: int, base_price: float = 100.0, trend: float = 0.0) -> list[dict]:
    """N개의 가상 OHLCV 캔들 생성 (테스트용)."""
    random.seed(42)
    candles = []
    price = base_price
    for i in range(n):
        price *= 1 + trend + random.uniform(-0.01, 0.01)
        high = price * (1 + abs(random.uniform(0, 0.005)))
        low = price * (1 - abs(random.uniform(0, 0.005)))
        open_p = price * (1 + random.uniform(-0.002, 0.002))
        candles.append({
            "open": open_p,
            "high": high,
            "low": low,
            "close": price,
            "volume": random.uniform(1000, 10000),
        })
    return candles


def test_feature_engine_basic():
    """FeatureEngine이 정상 캔들로 23개 피처를 계산."""
    from ml_signal_engine import FeatureEngine

    fe = FeatureEngine()
    candles = _make_candles(60)
    features = fe.compute_features(candles)

    assert isinstance(features, dict)
    assert len(features) >= 20
    # 핵심 피처 존재 확인
    for name in ["rsi", "ema_trend", "macd_hist_norm", "bb_pct_b", "adx", "volume_ratio"]:
        assert name in features, f"{name} 누락"

    # 범위 검증
    assert 0 <= features["rsi"] <= 100, f"RSI 범위 이탈: {features['rsi']}"
    assert -1.0 <= features["ema_trend"] <= 1.0


def test_feature_engine_handles_short_data():
    """짧은 캔들 시퀀스에도 크래시하지 않고 빈 dict 반환."""
    from ml_signal_engine import FeatureEngine

    fe = FeatureEngine()
    features = fe.compute_features(_make_candles(3))
    # 5개 미만이면 빈 dict
    assert features == {}


def test_features_to_array_consistent_size():
    """피처 배열은 항상 FEATURE_NAMES 길이와 동일."""
    from ml_signal_engine import FeatureEngine
    import numpy as np

    fe = FeatureEngine()
    features = fe.compute_features(_make_candles(60))
    arr = fe.features_to_array(features)

    assert arr.shape == (len(FeatureEngine.FEATURE_NAMES),)
    assert arr.dtype == np.float32
    assert not np.isnan(arr).any(), "피처에 NaN 포함"


def test_xgboost_predict_without_model():
    """모델 미학습 상태에서도 중립 시그널 반환 (크래시 X)."""
    from ml_signal_engine import XGBoostSignal, FeatureEngine

    # 존재하지 않는 경로로 강제 → 모델 로드 실패 시나리오
    xgb = XGBoostSignal(model_path="/tmp/nonexistent/xgb.json")
    fe = FeatureEngine()
    features = fe.compute_features(_make_candles(60))

    result = xgb.predict(features)
    assert result["signal"] == "hold"
    assert result["strength"] == 0.0
    assert "status" in result  # 미학습 상태 명시


def test_ml_engine_full_signal_without_models():
    """3개 모델 모두 미학습 → consensus가 hold + 0 confidence."""
    from ml_signal_engine import MLSignalEngine

    engine = MLSignalEngine(models_dir="/tmp/nonexistent_models_dir")
    candles = _make_candles(60)

    signals = engine.get_signals(candles)
    assert "xgboost" in signals
    assert "lstm" in signals
    assert "rl" in signals
    assert "consensus" in signals

    consensus = signals["consensus"]
    assert consensus["action"] == "hold"
    assert consensus["confidence"] == 0.0
    assert "model_scores" in consensus


def test_consensus_buy_when_all_models_agree():
    """3개 모델 점수가 모두 양수면 consensus가 buy."""
    from ml_signal_engine import MLSignalEngine

    engine = MLSignalEngine(models_dir="/tmp/nonexistent_models_dir")

    # 가상의 강한 buy 시그널
    fake_xgb = {"signal": "buy", "buy_prob": 0.9, "sell_prob": 0.05, "hold_prob": 0.05, "strength": 0.9}
    fake_lstm = {"direction": "up", "predicted_change_pct": 2.5, "confidence": 0.8}
    fake_rl = {"action": "buy", "confidence": 0.7, "q_values": {"buy": 1.5, "sell": -0.3, "hold": 0.1}}

    consensus = engine._compute_consensus(fake_xgb, fake_lstm, fake_rl)
    assert consensus["action"] == "buy"
    assert consensus["confidence"] > 0.3
    assert consensus["weighted_score"] > 0


def test_consensus_hold_when_signals_weak():
    """모든 시그널이 약하면 consensus가 hold (가중점수 |score| < 0.15)."""
    from ml_signal_engine import MLSignalEngine

    engine = MLSignalEngine(models_dir="/tmp/nonexistent_models_dir")

    # 매우 약한 시그널 — 가중점수 절대값 0.15 미만이어야 함
    fake_xgb = {"signal": "buy", "buy_prob": 0.2, "sell_prob": 0.4, "hold_prob": 0.4, "strength": 0.2}
    fake_lstm = {"direction": "down", "predicted_change_pct": -0.1, "confidence": 0.2}
    fake_rl = {"action": "hold", "confidence": 0.0, "q_values": {}}

    consensus = engine._compute_consensus(fake_xgb, fake_lstm, fake_rl)
    # weighted = 0.4*0.2 + 0.3*(-0.006) + 0 = ~0.078 → hold
    assert consensus["action"] == "hold", f"got {consensus}"
    assert abs(consensus["weighted_score"]) < 0.15
