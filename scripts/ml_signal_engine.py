#!/usr/bin/env python3
"""
ML/DL 트레이딩 시그널 엔진 (ML Signal Engine)

3단계 하이브리드 ML 시스템:
  Phase 1: XGBoost 매수/매도/관망 분류기
  Phase 2: LSTM 가격 방향 예측기
  Phase 3: DQN 강화학습 에이전트

기존 Gemini LLM 판단에 ML 시그널을 주입하여
진입/퇴출 타이밍 정확도를 높이는 것이 목표.

사용법:
    engine = MLSignalEngine()
    signals = engine.get_signals(candles)
    prompt_text = engine.format_for_llm_prompt(signals)
    # -> prompt_text를 Gemini 프롬프트에 삽입

의존성: xgboost, torch (CPU), scikit-learn, numpy
"""

# macOS에서 XGBoost(libomp)와 PyTorch의 OpenMP 충돌 방지
import os as _os
_os.environ.setdefault("OMP_NUM_THREADS", "1")
_os.environ.setdefault("MKL_NUM_THREADS", "1")

import json
import logging
import math
import os
import pickle
import sys
import threading
import warnings
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

warnings.filterwarnings("ignore", category=FutureWarning)

# ─── 로깅 설정 ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ml_signal_engine")

# ─── 상수 ──────────────────────────────────────────────────────
TRADING_ROOT = os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading"))
DEFAULT_MODELS_DIR = os.path.join(TRADING_ROOT, "freqtrade_userdata/models")
DEFAULT_DECISION_LOG = os.path.join(TRADING_ROOT, "freqtrade_userdata/logs/gemini_decisions.jsonl")


# =====================================================================
# Phase 0: Feature Engineering
# =====================================================================
class FeatureEngine:
    """원시 OHLCV 캔들 데이터를 ML 피처로 변환한다.

    캔들 형식 (dict):
        {"open": float, "high": float, "low": float, "close": float,
         "volume": float/int, "date": str (optional)}
    """

    # ── 기본 연산 ───────────────────────────────────────────────
    @staticmethod
    def _ema(data: list[float], period: int) -> float:
        if not data:
            return 0.0
        if len(data) < period:
            return sum(data) / len(data)
        k = 2.0 / (period + 1)
        result = sum(data[:period]) / period
        for val in data[period:]:
            result = val * k + result * (1 - k)
        return result

    @staticmethod
    def _ema_series(data: list[float], period: int) -> list[float]:
        """전체 EMA 시리즈를 반환한다."""
        if not data:
            return []
        series = []
        if len(data) < period:
            for i in range(len(data)):
                series.append(sum(data[: i + 1]) / (i + 1))
            return series
        k = 2.0 / (period + 1)
        result = sum(data[:period]) / period
        for i in range(period):
            series.append(sum(data[: i + 1]) / (i + 1))
        for val in data[period:]:
            result = val * k + result * (1 - k)
            series.append(result)
        return series

    @staticmethod
    def _sma(data: list[float], period: int) -> float:
        if not data:
            return 0.0
        if len(data) < period:
            return sum(data) / len(data)
        return sum(data[-period:]) / period

    @staticmethod
    def _rsi(closes: list[float], period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))
        if len(gains) < period:
            return 50.0
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _stoch_rsi(closes: list[float], rsi_period: int = 14, stoch_period: int = 14) -> float:
        """Stochastic RSI: RSI의 상대적 위치 (0~100)"""
        if len(closes) < rsi_period + stoch_period:
            return 50.0
        rsi_values = []
        for i in range(stoch_period + rsi_period, len(closes) + 1):
            subset = closes[:i]
            rsi_values.append(FeatureEngine._rsi(subset, rsi_period))
        if len(rsi_values) < stoch_period:
            return 50.0
        recent = rsi_values[-stoch_period:]
        rsi_min = min(recent)
        rsi_max = max(recent)
        if rsi_max == rsi_min:
            return 50.0
        current_rsi = rsi_values[-1]
        return ((current_rsi - rsi_min) / (rsi_max - rsi_min)) * 100.0

    def _macd(self, closes: list[float]) -> tuple[float, float, float]:
        """MACD, Signal, Histogram을 반환한다."""
        ema12 = self._ema(closes, 12)
        ema26 = self._ema(closes, 26)
        macd_val = ema12 - ema26

        # MACD 시그널 라인을 위한 시리즈 계산
        macd_series = []
        start = max(26, len(closes) - 34)
        for i in range(start, len(closes)):
            subset = closes[: i + 1]
            e12 = self._ema(subset, 12)
            e26 = self._ema(subset, 26)
            macd_series.append(e12 - e26)

        signal = self._ema(macd_series, 9) if len(macd_series) >= 9 else macd_val
        histogram = macd_val - signal
        return macd_val, signal, histogram

    def _bollinger(self, closes: list[float], period: int = 20, num_std: float = 2.0):
        """볼린저 밴드 (upper, middle, lower, %b)를 반환한다."""
        sma = self._sma(closes, period)
        if len(closes) >= period:
            variance = sum((c - sma) ** 2 for c in closes[-period:]) / period
            std = variance ** 0.5
        else:
            std = 0.0
        upper = sma + num_std * std
        lower = sma - num_std * std
        # %B: 가격이 밴드 내 어디에 위치하는지 (0=하단, 1=상단)
        bb_width = upper - lower
        if bb_width > 0:
            pct_b = (closes[-1] - lower) / bb_width
        else:
            pct_b = 0.5
        return upper, sma, lower, pct_b

    def _atr(self, candles: list[dict], period: int = 14) -> float:
        """ATR (Average True Range) 계산"""
        if len(candles) < 2:
            return 0.0
        tr_vals = []
        for i in range(1, len(candles)):
            h = candles[i]["high"]
            low = candles[i]["low"]
            pc = candles[i - 1]["close"]
            tr = max(h - low, abs(h - pc), abs(low - pc))
            tr_vals.append(tr)
        return self._sma(tr_vals, period)

    def _obv(self, candles: list[dict]) -> float:
        """On-Balance Volume"""
        if len(candles) < 2:
            return 0.0
        obv = 0.0
        for i in range(1, len(candles)):
            if candles[i]["close"] > candles[i - 1]["close"]:
                obv += candles[i].get("volume", 0)
            elif candles[i]["close"] < candles[i - 1]["close"]:
                obv -= candles[i].get("volume", 0)
        return obv

    def _adx(self, candles: list[dict], period: int = 14) -> float:
        """간이 ADX (Average Directional Index) — 추세 강도"""
        if len(candles) < period + 1:
            return 25.0  # 중립
        plus_dm_list = []
        minus_dm_list = []
        tr_list = []
        for i in range(1, len(candles)):
            h = candles[i]["high"]
            l = candles[i]["low"]
            ph = candles[i - 1]["high"]
            pl = candles[i - 1]["low"]
            pc = candles[i - 1]["close"]
            up_move = h - ph
            down_move = pl - l
            plus_dm = up_move if (up_move > down_move and up_move > 0) else 0
            minus_dm = down_move if (down_move > up_move and down_move > 0) else 0
            plus_dm_list.append(plus_dm)
            minus_dm_list.append(minus_dm)
            tr = max(h - l, abs(h - pc), abs(l - pc))
            tr_list.append(tr)

        if len(tr_list) < period:
            return 25.0

        # Wilder smoothing
        atr = sum(tr_list[:period]) / period
        plus_dm_avg = sum(plus_dm_list[:period]) / period
        minus_dm_avg = sum(minus_dm_list[:period]) / period

        for i in range(period, len(tr_list)):
            atr = (atr * (period - 1) + tr_list[i]) / period
            plus_dm_avg = (plus_dm_avg * (period - 1) + plus_dm_list[i]) / period
            minus_dm_avg = (minus_dm_avg * (period - 1) + minus_dm_list[i]) / period

        if atr == 0:
            return 25.0
        plus_di = (plus_dm_avg / atr) * 100
        minus_di = (minus_dm_avg / atr) * 100
        di_sum = plus_di + minus_di
        if di_sum == 0:
            return 25.0
        dx = abs(plus_di - minus_di) / di_sum * 100
        return dx

    # ── 메인 피처 계산 ──────────────────────────────────────────
    def compute_features(self, candles: list[dict]) -> dict:
        """캔들 리스트에서 ML 피처 딕셔너리를 생성한다.

        최소 30개 캔들 필요. 부족 시 가능한 만큼 계산하고 나머지는 0.
        """
        if len(candles) < 5:
            logger.warning("피처 계산 불가: 캔들 5개 미만")
            return {}

        closes = [c["close"] for c in candles]
        volumes = [c.get("volume", 0) for c in candles]
        price = closes[-1]

        # EMA
        ema9 = self._ema(closes, 9)
        ema21 = self._ema(closes, 21)
        ema50 = self._ema(closes, 50) if len(closes) >= 50 else self._ema(closes, len(closes))

        # EMA 트렌드 수치화 (-1=강한 하락, 0=혼합, 1=강한 상승)
        if ema9 > ema21 > ema50:
            ema_trend_val = 1.0
        elif ema9 < ema21 < ema50:
            ema_trend_val = -1.0
        else:
            ema_trend_val = 0.0

        # 가격 대비 EMA 위치
        ema9_dist = (price - ema9) / price * 100 if price else 0
        ema21_dist = (price - ema21) / price * 100 if price else 0

        # RSI
        rsi = self._rsi(closes)
        stoch_rsi = self._stoch_rsi(closes)

        # MACD
        macd_val, macd_signal, macd_hist = self._macd(closes)
        # MACD를 가격 대비 정규화
        macd_norm = macd_val / price * 100 if price else 0
        macd_hist_norm = macd_hist / price * 100 if price else 0

        # 볼린저 밴드
        bb_upper, bb_middle, bb_lower, bb_pct_b = self._bollinger(closes)
        bb_width = (bb_upper - bb_lower) / bb_middle * 100 if bb_middle else 0

        # ATR
        atr = self._atr(candles)
        atr_pct = atr / price * 100 if price else 0

        # 볼륨
        vol_sma20 = self._sma(volumes, 20)
        volume_ratio = volumes[-1] / vol_sma20 if vol_sma20 > 0 else 1.0

        # OBV 변화율
        obv = self._obv(candles)
        obv_prev = self._obv(candles[:-5]) if len(candles) > 5 else 0
        obv_change = (obv - obv_prev) / abs(obv_prev) * 100 if obv_prev != 0 else 0

        # ADX (추세 강도)
        adx = self._adx(candles)

        # 가격 변화율
        def pct_change(n):
            if len(closes) > n and closes[-n - 1] != 0:
                return (closes[-1] / closes[-n - 1] - 1) * 100
            return 0.0

        price_chg_1 = pct_change(1)
        price_chg_3 = pct_change(3)
        price_chg_5 = pct_change(5)
        price_chg_10 = pct_change(10)
        price_chg_20 = pct_change(20) if len(closes) > 20 else 0.0

        # 캔들 패턴 (간이)
        body = candles[-1]["close"] - candles[-1]["open"]
        total_range = candles[-1]["high"] - candles[-1]["low"]
        body_ratio = body / total_range if total_range > 0 else 0  # 양수=양봉, 음수=음봉
        upper_shadow = (candles[-1]["high"] - max(candles[-1]["open"], candles[-1]["close"])) / total_range if total_range > 0 else 0
        lower_shadow = (min(candles[-1]["open"], candles[-1]["close"]) - candles[-1]["low"]) / total_range if total_range > 0 else 0

        # 변동성 (최근 N봉 수익률 표준편차)
        if len(closes) >= 20:
            returns = [(closes[i] / closes[i - 1] - 1) for i in range(max(1, len(closes) - 20), len(closes))]
            volatility = (sum((r - sum(returns) / len(returns)) ** 2 for r in returns) / len(returns)) ** 0.5 * 100
        else:
            volatility = 0.0

        # 고가/저가 대비 현재 가격 위치 (최근 20봉)
        recent = candles[-min(20, len(candles)):]
        high_20 = max(c["high"] for c in recent)
        low_20 = min(c["low"] for c in recent)
        price_position = (price - low_20) / (high_20 - low_20) if high_20 != low_20 else 0.5

        return {
            # 이동평균
            "ema_trend": ema_trend_val,
            "ema9_dist_pct": ema9_dist,
            "ema21_dist_pct": ema21_dist,
            # 오실레이터
            "rsi": rsi,
            "stoch_rsi": stoch_rsi,
            # MACD (정규화)
            "macd_norm": macd_norm,
            "macd_hist_norm": macd_hist_norm,
            # 볼린저
            "bb_pct_b": bb_pct_b,
            "bb_width": bb_width,
            # 추세 강도
            "adx": adx,
            "atr_pct": atr_pct,
            # 볼륨
            "volume_ratio": volume_ratio,
            "obv_change_pct": obv_change,
            # 가격 변화
            "price_chg_1": price_chg_1,
            "price_chg_3": price_chg_3,
            "price_chg_5": price_chg_5,
            "price_chg_10": price_chg_10,
            "price_chg_20": price_chg_20,
            # 캔들 패턴
            "body_ratio": body_ratio,
            "upper_shadow": upper_shadow,
            "lower_shadow": lower_shadow,
            # 변동성
            "volatility": volatility,
            # 가격 위치
            "price_position_20": price_position,
        }

    # 정의된 피처 이름 (순서 보장용)
    FEATURE_NAMES = [
        "ema_trend", "ema9_dist_pct", "ema21_dist_pct",
        "rsi", "stoch_rsi",
        "macd_norm", "macd_hist_norm",
        "bb_pct_b", "bb_width",
        "adx", "atr_pct",
        "volume_ratio", "obv_change_pct",
        "price_chg_1", "price_chg_3", "price_chg_5", "price_chg_10", "price_chg_20",
        "body_ratio", "upper_shadow", "lower_shadow",
        "volatility", "price_position_20",
    ]

    def features_to_array(self, features: dict) -> np.ndarray:
        """피처 딕셔너리를 정렬된 numpy 배열로 변환한다."""
        return np.array([features.get(name, 0.0) for name in self.FEATURE_NAMES], dtype=np.float32)

    def compute_feature_sequence(self, candles: list[dict], seq_len: int = 30) -> list[dict]:
        """LSTM 입력용: 최근 seq_len개 시점의 피처 시퀀스를 반환한다."""
        if len(candles) < seq_len + 20:
            # 최소 피처 계산에 필요한 여유분 확보
            start = 0
        else:
            start = len(candles) - seq_len - 20

        sequence = []
        for end_idx in range(max(20, start + 20), len(candles) + 1):
            subset = candles[:end_idx]
            feat = self.compute_features(subset)
            if feat:
                sequence.append(feat)

        return sequence[-seq_len:]

    def features_sequence_to_tensor(self, sequence: list[dict]) -> np.ndarray:
        """피처 시퀀스를 (seq_len, n_features) 형태 numpy 배열로 변환한다."""
        arrays = [self.features_to_array(f) for f in sequence]
        return np.array(arrays, dtype=np.float32)


# =====================================================================
# Phase 1: XGBoost 매수/매도 분류기
# =====================================================================
class XGBoostSignal:
    """XGBoost 기반 매수/매도/관망 확률 분류기.

    소량 데이터에서도 동작하며, 초기에는 기술적 규칙으로
    부트스트랩 레이블을 생성하여 학습할 수 있다.
    """

    LABEL_MAP = {"buy": 0, "sell": 1, "hold": 2}
    LABEL_INV = {0: "buy", 1: "sell", 2: "hold"}

    def __init__(self, model_path: Optional[str] = None):
        self.model_path = model_path or os.path.join(DEFAULT_MODELS_DIR, "xgb_signal.json")
        self.scaler_path = self.model_path.replace(".json", "_scaler.pkl")
        self.model = None
        self.scaler = None
        self.feature_engine = FeatureEngine()
        self._lock = threading.Lock()
        self._load_model()

    def _load_model(self):
        """저장된 모델 로드 시도"""
        try:
            if os.path.exists(self.model_path):
                import xgboost as xgb
                self.model = xgb.XGBClassifier()
                self.model.load_model(self.model_path)
                logger.info(f"XGBoost 모델 로드 완료: {self.model_path}")
            if os.path.exists(self.scaler_path):
                with open(self.scaler_path, "rb") as f:
                    self.scaler = pickle.load(f)
                logger.info("XGBoost 스케일러 로드 완료")
        except Exception as e:
            logger.warning(f"XGBoost 모델 로드 실패: {e}")
            self.model = None
            self.scaler = None

    def _save_model(self):
        """모델과 스케일러를 디스크에 저장한다."""
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        if self.model is not None:
            self.model.save_model(self.model_path)
            logger.info(f"XGBoost 모델 저장 완료: {self.model_path}")
        if self.scaler is not None:
            with open(self.scaler_path, "wb") as f:
                pickle.dump(self.scaler, f)

    def train(self, features_list: list[dict], labels: list[str], validate: bool = True):
        """레이블된 데이터로 XGBoost를 학습한다.

        Args:
            features_list: 피처 딕셔너리 리스트
            labels: "buy" / "sell" / "hold" 문자열 리스트
            validate: True이면 20%를 검증 데이터로 분리
        """
        import xgboost as xgb
        from sklearn.preprocessing import StandardScaler
        from sklearn.model_selection import train_test_split

        X = np.array([self.feature_engine.features_to_array(f) for f in features_list])
        y = np.array([self.LABEL_MAP.get(l, 2) for l in labels])

        # NaN / Inf 처리
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        # 정규화
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X)

        # 클래스 가중치 (불균형 처리)
        unique, counts = np.unique(y, return_counts=True)
        total = len(y)
        class_weights = {cls: total / (len(unique) * cnt) for cls, cnt in zip(unique, counts)}
        sample_weights = np.array([class_weights.get(yi, 1.0) for yi in y])

        params = {
            "n_estimators": 200,
            "max_depth": 5,
            "learning_rate": 0.05,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
            "objective": "multi:softprob",
            "num_class": 3,
            "eval_metric": "mlogloss",
            "random_state": 42,
            "n_jobs": -1,
        }

        if validate and len(X_scaled) > 50:
            X_train, X_val, y_train, y_val, w_train, w_val = train_test_split(
                X_scaled, y, sample_weights, test_size=0.2, random_state=42, stratify=y
            )
            self.model = xgb.XGBClassifier(**params)
            self.model.fit(
                X_train, y_train,
                sample_weight=w_train,
                eval_set=[(X_val, y_val)],
                verbose=False,
            )
            # 검증 정확도
            val_pred = self.model.predict(X_val)
            accuracy = (val_pred == y_val).mean()
            logger.info(f"XGBoost 학습 완료 — 검증 정확도: {accuracy:.1%} (학습={len(X_train)}, 검증={len(X_val)})")
        else:
            self.model = xgb.XGBClassifier(**params)
            self.model.fit(X_scaled, y, sample_weight=sample_weights, verbose=False)
            logger.info(f"XGBoost 학습 완료 — 데이터 {len(X_scaled)}건 (검증 없음)")

        with self._lock:
            self._save_model()

    def predict(self, features: dict) -> dict:
        """단일 시점 피처로 매수/매도/관망 확률을 예측한다.

        모델이 없으면 중립 시그널을 반환한다.
        """
        if self.model is None or self.scaler is None:
            return {
                "buy_prob": 0.33, "sell_prob": 0.33, "hold_prob": 0.34,
                "signal": "hold", "strength": 0.0,
                "status": "모델 미학습 — 중립 시그널 반환",
            }

        X = self.feature_engine.features_to_array(features).reshape(1, -1)
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        with self._lock:
            X_scaled = self.scaler.transform(X)
            proba = self.model.predict_proba(X_scaled)[0]

        buy_p, sell_p, hold_p = float(proba[0]), float(proba[1]), float(proba[2])
        best_idx = int(np.argmax(proba))
        signal = self.LABEL_INV[best_idx]
        strength = float(proba[best_idx])

        return {
            "buy_prob": round(buy_p, 4),
            "sell_prob": round(sell_p, 4),
            "hold_prob": round(hold_p, 4),
            "signal": signal,
            "strength": round(strength, 4),
        }

    def generate_bootstrap_labels(self, candles_list: list[list[dict]]) -> tuple[list[dict], list[str]]:
        """기술적 규칙으로 부트스트랩 레이블을 생성한다.

        ⚠️ LOOK-AHEAD WARNING ⚠️
        이 함수는 미래 5봉 수익률을 레이블 정답으로 사용한다 (look-ahead).
        반드시 **과거 히스토리컬 데이터로 오프라인 학습**할 때만 호출하라.
        라이브 트레이딩 루프나 실시간 추론 코드에서 절대 호출 금지.
        호출 시점 t의 candles[t+5] 가 존재해야 하므로 라이브에서는 동작 불가.

        여러 종목 / 시점의 캔들 리스트를 받아 학습 데이터를 만든다.
        각 candles_list 항목은 단일 종목의 전체 캔들 시계열이다.

        규칙:
            - RSI < 30 + EMA 상승 추세 → buy
            - RSI > 70 + EMA 하락 추세 → sell
            - 미래 5봉 수익률 > +1.5% → buy   (look-ahead)
            - 미래 5봉 수익률 < -1.5% → sell  (look-ahead)
            - 그 외 → hold
        """
        all_features = []
        all_labels = []
        fe = self.feature_engine

        for candles in candles_list:
            if len(candles) < 35:
                continue

            # 슬라이딩 윈도우로 각 시점에서 피처 + 미래 수익률 계산
            for i in range(30, len(candles) - 5):
                subset = candles[:i + 1]
                features = fe.compute_features(subset)
                if not features:
                    continue

                # 미래 5봉 수익률
                future_close = candles[i + 5]["close"]
                current_close = candles[i]["close"]
                if current_close == 0:
                    continue
                future_return = (future_close / current_close - 1) * 100

                # 규칙 기반 레이블
                rsi = features["rsi"]
                ema_trend = features["ema_trend"]

                if (rsi < 30 and ema_trend >= 0) or future_return > 1.5:
                    label = "buy"
                elif (rsi > 70 and ema_trend <= 0) or future_return < -1.5:
                    label = "sell"
                else:
                    label = "hold"

                all_features.append(features)
                all_labels.append(label)

        logger.info(
            f"부트스트랩 레이블 생성 완료: {len(all_features)}건 "
            f"(buy={all_labels.count('buy')}, sell={all_labels.count('sell')}, "
            f"hold={all_labels.count('hold')})"
        )
        return all_features, all_labels

    def generate_labels_from_outcomes(self, jsonl_path: str) -> tuple[list[dict], list[str]]:
        """JSONL 판단 로그에서 실제 결과 기반 학습 레이블을 생성한다.

        outcome 필드가 있는 항목만 사용:
            - "win(+X%)" → buy 시그널이 정답 → buy 레이블
            - "loss(-X%)" → sell 시그널이 정답 → sell 레이블
            - outcome 없음 → 스킵
        """
        all_features = []
        all_labels = []
        fe = self.feature_engine

        try:
            with open(jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    outcome = entry.get("outcome", "")
                    if not outcome:
                        continue

                    # JSONL의 피처 필드에서 직접 피처 구성
                    features = {}
                    rsi = entry.get("rsi")
                    if rsi is None:
                        continue

                    # JSONL에 기록된 피처 사용 (일부만 있으므로 간이 버전)
                    ema_trend_str = entry.get("ema_trend", "")
                    if ema_trend_str == "bull":
                        ema_trend_val = 1.0
                    elif ema_trend_str == "bear":
                        ema_trend_val = -1.0
                    else:
                        ema_trend_val = 0.0

                    macd_hist = entry.get("macd_hist", 0)
                    price = entry.get("price", 1)
                    vol_ratio = entry.get("volume_ratio", 1.0)

                    # 사용 가능한 피처로 채우기
                    for name in FeatureEngine.FEATURE_NAMES:
                        features[name] = 0.0
                    features["rsi"] = rsi
                    features["ema_trend"] = ema_trend_val
                    features["macd_hist_norm"] = macd_hist / price * 100 if price else 0
                    features["volume_ratio"] = vol_ratio

                    # 레이블 결정
                    action = entry.get("action", "hold")
                    if "win" in outcome:
                        # 원래 action이 맞았으므로 그대로 레이블
                        label = action if action in ("buy", "sell") else "hold"
                    elif "loss" in outcome:
                        # 원래 action이 틀렸으므로 반대 레이블
                        if action == "buy":
                            label = "sell"
                        elif action == "sell":
                            label = "buy"
                        else:
                            label = "hold"
                    else:
                        label = "hold"

                    all_features.append(features)
                    all_labels.append(label)

        except FileNotFoundError:
            logger.warning(f"JSONL 파일 없음: {jsonl_path}")
        except Exception as e:
            logger.warning(f"JSONL 파싱 실패: {e}")

        logger.info(
            f"Outcome 기반 레이블 생성: {len(all_features)}건 "
            f"(buy={all_labels.count('buy')}, sell={all_labels.count('sell')}, "
            f"hold={all_labels.count('hold')})"
        )
        return all_features, all_labels


# =====================================================================
# Phase 2: LSTM 가격 방향 예측기
# =====================================================================
class LSTMPredictor:
    """PyTorch LSTM으로 향후 가격 변화 방향과 크기를 예측한다."""

    def __init__(self, model_path: Optional[str] = None, input_size: int = 23, hidden_size: int = 64, seq_len: int = 30):
        self.model_path = model_path or os.path.join(DEFAULT_MODELS_DIR, "lstm_predictor.pt")
        self.scaler_path = self.model_path.replace(".pt", "_scaler.pkl")
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.seq_len = seq_len
        self.model = None
        self.scaler = None
        self._lock = threading.Lock()
        self._load_model()

    def _build_model(self):
        """LSTM 모델을 생성한다."""
        import torch
        import torch.nn as nn

        class PriceLSTM(nn.Module):
            def __init__(self, input_size, hidden_size):
                super().__init__()
                self.lstm = nn.LSTM(
                    input_size=input_size,
                    hidden_size=hidden_size,
                    num_layers=2,
                    batch_first=True,
                    dropout=0.2,
                )
                self.fc = nn.Sequential(
                    nn.Linear(hidden_size, 32),
                    nn.ReLU(),
                    nn.Dropout(0.1),
                    nn.Linear(32, 2),  # [predicted_change_pct, confidence_logit]
                )

            def forward(self, x):
                # x: (batch, seq_len, input_size)
                lstm_out, _ = self.lstm(x)
                # 마지막 타임스텝만 사용
                last_hidden = lstm_out[:, -1, :]
                out = self.fc(last_hidden)
                return out

        return PriceLSTM(self.input_size, self.hidden_size)

    def _load_model(self):
        """저장된 LSTM 모델 로드"""
        try:
            import torch
            if os.path.exists(self.model_path):
                self.model = self._build_model()
                state = torch.load(self.model_path, map_location="cpu", weights_only=True)
                self.model.load_state_dict(state)
                self.model.eval()
                logger.info(f"LSTM 모델 로드 완료: {self.model_path}")
            if os.path.exists(self.scaler_path):
                with open(self.scaler_path, "rb") as f:
                    self.scaler = pickle.load(f)
        except Exception as e:
            logger.warning(f"LSTM 모델 로드 실패: {e}")
            self.model = None

    def _save_model(self):
        """모델 저장"""
        import torch
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        if self.model is not None:
            torch.save(self.model.state_dict(), self.model_path)
            logger.info(f"LSTM 모델 저장 완료: {self.model_path}")
        if self.scaler is not None:
            with open(self.scaler_path, "wb") as f:
                pickle.dump(self.scaler, f)

    def train(self, candles_list: list[list[dict]], epochs: int = 50, lr: float = 0.001, predict_ahead: int = 5):
        """캔들 데이터로 LSTM을 학습한다.

        Args:
            candles_list: 여러 종목의 캔들 시계열 리스트
            epochs: 학습 에포크
            lr: 학습률
            predict_ahead: 미래 N봉 가격 변화를 예측
        """
        import torch
        import torch.nn as nn
        from sklearn.preprocessing import StandardScaler

        fe = FeatureEngine()

        # 데이터 준비: 시퀀스 → 미래 수익률
        all_X = []
        all_y = []

        for candles in candles_list:
            if len(candles) < self.seq_len + predict_ahead + 20:
                continue

            # 전체 시계열에서 슬라이딩 윈도우
            for end_idx in range(self.seq_len + 20, len(candles) - predict_ahead):
                # 시퀀스 피처
                seq_features = []
                valid = True
                for t in range(end_idx - self.seq_len, end_idx):
                    subset = candles[:t + 1]
                    feat = fe.compute_features(subset)
                    if not feat:
                        valid = False
                        break
                    seq_features.append(fe.features_to_array(feat))

                if not valid or len(seq_features) != self.seq_len:
                    continue

                # 타겟: 미래 수익률
                current_price = candles[end_idx - 1]["close"]
                future_price = candles[end_idx - 1 + predict_ahead]["close"]
                if current_price == 0:
                    continue
                future_return = (future_price / current_price - 1) * 100

                all_X.append(np.array(seq_features))
                all_y.append(future_return)

        if len(all_X) < 20:
            logger.warning(f"LSTM 학습 데이터 부족: {len(all_X)}건 (최소 20건 필요)")
            return

        X = np.array(all_X, dtype=np.float32)  # (N, seq_len, n_features)
        y = np.array(all_y, dtype=np.float32)   # (N,)

        # NaN 처리
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        # 피처 정규화 (각 피처별로)
        N, S, F = X.shape
        X_flat = X.reshape(-1, F)
        self.scaler = StandardScaler()
        X_flat_scaled = self.scaler.fit_transform(X_flat)
        X = X_flat_scaled.reshape(N, S, F).astype(np.float32)

        # 학습/검증 분리
        split_idx = int(len(X) * 0.8)
        X_train, X_val = X[:split_idx], X[split_idx:]
        y_train, y_val = y[:split_idx], y[split_idx:]

        # PyTorch 텐서로 변환
        X_train_t = torch.FloatTensor(X_train)
        y_train_t = torch.FloatTensor(y_train)
        X_val_t = torch.FloatTensor(X_val)
        y_val_t = torch.FloatTensor(y_val)

        # 모델 생성
        self.model = self._build_model()
        optimizer = torch.optim.Adam(self.model.parameters(), lr=lr, weight_decay=1e-5)
        loss_fn = nn.MSELoss()

        # 학습
        best_val_loss = float("inf")
        patience_counter = 0
        patience = 10

        self.model.train()
        batch_size = min(64, len(X_train))

        for epoch in range(epochs):
            # 미니배치 학습
            indices = np.random.permutation(len(X_train))
            epoch_loss = 0.0
            n_batches = 0

            for start in range(0, len(X_train), batch_size):
                batch_idx = indices[start:start + batch_size]
                X_batch = X_train_t[batch_idx]
                y_batch = y_train_t[batch_idx]

                optimizer.zero_grad()
                output = self.model(X_batch)
                pred_change = output[:, 0]
                loss = loss_fn(pred_change, y_batch)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
                optimizer.step()

                epoch_loss += loss.item()
                n_batches += 1

            # 검증
            self.model.eval()
            with torch.no_grad():
                val_output = self.model(X_val_t)
                val_loss = loss_fn(val_output[:, 0], y_val_t).item()
            self.model.train()

            if val_loss < best_val_loss:
                best_val_loss = val_loss
                patience_counter = 0
            else:
                patience_counter += 1

            if (epoch + 1) % 10 == 0:
                logger.info(
                    f"LSTM 에포크 {epoch + 1}/{epochs} — "
                    f"학습 손실: {epoch_loss / n_batches:.4f}, "
                    f"검증 손실: {val_loss:.4f}"
                )

            if patience_counter >= patience:
                logger.info(f"조기 종료 (에포크 {epoch + 1})")
                break

        self.model.eval()

        # 방향 예측 정확도 계산
        with torch.no_grad():
            val_pred = self.model(X_val_t)[:, 0].numpy()
        direction_accuracy = np.mean((val_pred > 0) == (y_val > 0))
        logger.info(
            f"LSTM 학습 완료 — 방향 정확도: {direction_accuracy:.1%} "
            f"(학습={len(X_train)}, 검증={len(X_val)})"
        )

        with self._lock:
            self._save_model()

    def predict(self, sequence: list[dict]) -> dict:
        """피처 시퀀스로 미래 가격 변화를 예측한다.

        모델이 없거나 torch 미설치 시 중립 예측을 반환한다.
        """
        try:
            import torch
        except ImportError:
            return {
                "predicted_change_pct": 0.0,
                "direction": "neutral",
                "confidence": 0.0,
                "status": "torch 미설치 — 중립 예측",
            }

        if self.model is None or self.scaler is None:
            return {
                "predicted_change_pct": 0.0,
                "direction": "neutral",
                "confidence": 0.0,
                "status": "모델 미학습 — 중립 예측 반환",
            }

        fe = FeatureEngine()
        seq_arr = fe.features_sequence_to_tensor(sequence)

        # 시퀀스 길이 부족 시 패딩
        if len(seq_arr) < self.seq_len:
            pad_len = self.seq_len - len(seq_arr)
            pad = np.zeros((pad_len, seq_arr.shape[1]), dtype=np.float32)
            seq_arr = np.concatenate([pad, seq_arr], axis=0)
        elif len(seq_arr) > self.seq_len:
            seq_arr = seq_arr[-self.seq_len:]

        # 정규화
        seq_flat = seq_arr.reshape(-1, seq_arr.shape[-1])
        seq_flat = np.nan_to_num(seq_flat, nan=0.0, posinf=0.0, neginf=0.0)
        seq_scaled = self.scaler.transform(seq_flat)
        seq_arr = seq_scaled.reshape(1, self.seq_len, -1).astype(np.float32)

        with self._lock:
            self.model.eval()
            with torch.no_grad():
                output = self.model(torch.FloatTensor(seq_arr))

        pred_change = float(output[0, 0])
        confidence_logit = float(output[0, 1])
        confidence = 1.0 / (1.0 + math.exp(-confidence_logit))  # sigmoid

        # 방향 판단
        if abs(pred_change) < 0.2:
            direction = "neutral"
        elif pred_change > 0:
            direction = "up"
        else:
            direction = "down"

        return {
            "predicted_change_pct": round(pred_change, 4),
            "direction": direction,
            "confidence": round(confidence, 4),
        }


# =====================================================================
# Phase 3: DQN 강화학습 에이전트
# =====================================================================
class RLTrader:
    """DQN (Deep Q-Network) 기반 매매 타이밍 최적화 에이전트.

    State: 기술 지표 + 현재 포지션 정보
    Actions: buy (0), sell (1), hold (2)
    Reward: 실현 수익률
    """

    ACTION_MAP = {0: "buy", 1: "sell", 2: "hold"}
    STATE_SIZE = 25  # 23 피처 + position_flag + unrealized_pnl

    def __init__(self, model_path: Optional[str] = None, gamma: float = 0.95, epsilon: float = 0.1):
        self.model_path = model_path or os.path.join(DEFAULT_MODELS_DIR, "dqn_trader.pt")
        self.gamma = gamma
        self.epsilon = epsilon
        self.memory = deque(maxlen=10000)
        self.model = None
        self.target_model = None
        self._lock = threading.Lock()
        self._step_count = 0
        self._load_model()

    def _build_model(self):
        """DQN 네트워크 생성"""
        import torch.nn as nn

        return nn.Sequential(
            nn.Linear(self.STATE_SIZE, 128),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(128, 64),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(64, 32),
            nn.ReLU(),
            nn.Linear(32, 3),  # Q-values for buy, sell, hold
        )

    def _load_model(self):
        """모델 로드"""
        try:
            import torch
            if os.path.exists(self.model_path):
                self.model = self._build_model()
                state = torch.load(self.model_path, map_location="cpu", weights_only=True)
                self.model.load_state_dict(state)
                self.model.eval()
                self.target_model = self._build_model()
                self.target_model.load_state_dict(self.model.state_dict())
                self.target_model.eval()
                logger.info(f"DQN 모델 로드 완료: {self.model_path}")
        except Exception as e:
            logger.warning(f"DQN 모델 로드 실패: {e}")
            self.model = None

    def _save_model(self):
        """모델 저장"""
        import torch
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        if self.model is not None:
            torch.save(self.model.state_dict(), self.model_path)
            logger.info(f"DQN 모델 저장 완료: {self.model_path}")

    def _make_state(self, features: dict, position: Optional[dict] = None) -> np.ndarray:
        """피처 딕셔너리 + 포지션 정보를 상태 벡터로 변환한다."""
        fe = FeatureEngine()
        feat_arr = fe.features_to_array(features)

        # 포지션 정보 추가
        if position and position.get("qty", 0) > 0:
            pos_flag = 1.0
            entry_price = position.get("avg_price", position.get("entry_price", 0))
            current_price = position.get("current_price", 0)
            unrealized_pnl = (current_price / entry_price - 1) * 100 if entry_price > 0 else 0.0
        else:
            pos_flag = 0.0
            unrealized_pnl = 0.0

        state = np.concatenate([feat_arr, [pos_flag, unrealized_pnl]])
        state = np.nan_to_num(state, nan=0.0, posinf=0.0, neginf=0.0)
        return state.astype(np.float32)

    def get_action(self, features: dict, position: Optional[dict] = None, explore: bool = False) -> dict:
        """현재 상태에서 최적 행동을 반환한다.

        모델이 없거나 torch 미설치 시 중립 행동을 반환한다.
        """
        try:
            import torch
        except ImportError:
            return {
                "action": "hold",
                "q_values": {"buy": 0.0, "sell": 0.0, "hold": 0.0},
                "confidence": 0.0,
                "status": "torch 미설치 — 관망",
            }

        if self.model is None:
            return {
                "action": "hold",
                "q_values": {"buy": 0.0, "sell": 0.0, "hold": 0.0},
                "confidence": 0.0,
                "status": "모델 미학습 — 관망 반환",
            }

        state = self._make_state(features, position)

        # Q값은 항상 계산 (로깅/분석용)
        with self._lock:
            self.model.eval()
            with torch.no_grad():
                q_vals = self.model(torch.FloatTensor(state).unsqueeze(0))[0]
            q_vals_np = q_vals.numpy()

        # epsilon-greedy (학습 중일 때만)
        if explore and np.random.random() < self.epsilon:
            action_idx = np.random.randint(3)
        else:
            action_idx = int(torch.argmax(q_vals).item())

        q_dict = {
            "buy": round(float(q_vals_np[0]), 4),
            "sell": round(float(q_vals_np[1]), 4),
            "hold": round(float(q_vals_np[2]), 4),
        }

        action = self.ACTION_MAP[action_idx]

        # 신뢰도: Q값 차이 기반
        q_arr = np.array(list(q_dict.values()))
        q_range = q_arr.max() - q_arr.min()
        confidence = min(1.0, q_range / (abs(q_arr.max()) + 1e-8))

        return {
            "action": action,
            "q_values": q_dict,
            "confidence": round(confidence, 4),
        }

    def train_step(self, state: np.ndarray, action: int, reward: float, next_state: np.ndarray, done: bool = False):
        """단일 경험을 메모리에 저장하고 배치 학습한다."""
        import torch
        import torch.nn as nn

        self.memory.append((state, action, reward, next_state, done))

        if len(self.memory) < 64:
            return

        # 미니배치 샘플링
        batch_size = min(64, len(self.memory))
        indices = np.random.choice(len(self.memory), batch_size, replace=False)
        batch = [self.memory[i] for i in indices]

        states = torch.FloatTensor(np.array([b[0] for b in batch]))
        actions = torch.LongTensor([b[1] for b in batch])
        rewards = torch.FloatTensor([b[2] for b in batch])
        next_states = torch.FloatTensor(np.array([b[3] for b in batch]))
        dones = torch.FloatTensor([float(b[4]) for b in batch])

        # 현재 Q값
        self.model.train()
        current_q = self.model(states).gather(1, actions.unsqueeze(1)).squeeze()

        # 타겟 Q값 (Double DQN)
        with torch.no_grad():
            next_actions = self.model(next_states).argmax(1)
            next_q = self.target_model(next_states).gather(1, next_actions.unsqueeze(1)).squeeze()
            target_q = rewards + self.gamma * next_q * (1 - dones)

        # 손실 계산 및 역전파
        loss_fn = nn.SmoothL1Loss()
        loss = loss_fn(current_q, target_q)

        optimizer = torch.optim.Adam(self.model.parameters(), lr=0.0005)
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
        optimizer.step()

        # 타겟 네트워크 주기적 업데이트
        self._step_count += 1
        if self._step_count % 100 == 0:
            self.target_model.load_state_dict(self.model.state_dict())

    def train_from_history(self, candles_list: list[list[dict]], initial_capital: float = 10000):
        """과거 캔들 데이터로 강화학습 에이전트를 학습한다.

        시뮬레이션: 각 시점에서 buy/sell/hold 행동을 취하고
        실현 수익을 보상으로 사용한다.
        """
        import torch

        self.model = self._build_model()
        self.target_model = self._build_model()
        self.target_model.load_state_dict(self.model.state_dict())

        fe = FeatureEngine()
        total_episodes = 0
        total_reward = 0.0

        for candles in candles_list:
            if len(candles) < 50:
                continue

            capital = initial_capital
            position = None  # {"entry_price": float, "qty": float}
            prev_state = None
            prev_action = None

            for i in range(30, len(candles)):
                subset = candles[:i + 1]
                features = fe.compute_features(subset)
                if not features:
                    continue

                pos_dict = None
                if position:
                    pos_dict = {
                        "qty": position["qty"],
                        "avg_price": position["entry_price"],
                        "current_price": candles[i]["close"],
                    }

                state = self._make_state(features, pos_dict)
                action_result = self.get_action(features, pos_dict, explore=True)
                action_idx = {"buy": 0, "sell": 1, "hold": 2}[action_result["action"]]

                # 행동 실행 및 보상 계산
                reward = 0.0
                price = candles[i]["close"]

                if action_result["action"] == "buy" and position is None:
                    qty = capital * 0.5 / price if price > 0 else 0
                    if qty > 0:
                        position = {"entry_price": price, "qty": qty}
                        reward = -0.001  # 거래 비용 패널티
                elif action_result["action"] == "sell" and position is not None:
                    pnl = (price / position["entry_price"] - 1) * 100
                    reward = pnl  # 수익률이 보상
                    capital += position["qty"] * (price - position["entry_price"])
                    position = None
                elif action_result["action"] == "hold":
                    if position is not None:
                        # 포지션 보유 중 미실현 수익 변화
                        unrealized = (price / position["entry_price"] - 1) * 100
                        reward = unrealized * 0.01  # 소량의 홀딩 보상
                    else:
                        reward = 0.0

                # 이전 상태가 있으면 학습
                if prev_state is not None:
                    self.train_step(prev_state, prev_action, reward, state, done=False)

                prev_state = state
                prev_action = action_idx
                total_reward += reward

            # 에피소드 종료 (미청산 포지션 정리)
            if position is not None and prev_state is not None:
                final_price = candles[-1]["close"]
                pnl = (final_price / position["entry_price"] - 1) * 100
                self.train_step(prev_state, prev_action, pnl, prev_state, done=True)
                total_reward += pnl

            total_episodes += 1

        if total_episodes > 0:
            logger.info(
                f"DQN 학습 완료 — {total_episodes}개 에피소드, "
                f"누적 보상: {total_reward:.2f}, "
                f"경험 메모리: {len(self.memory)}건"
            )
            with self._lock:
                self._save_model()
        else:
            logger.warning("DQN 학습 데이터 없음")


# =====================================================================
# 통합 ML 시그널 엔진
# =====================================================================
class MLSignalEngine:
    """XGBoost + LSTM + DQN을 통합하여 단일 인터페이스로 제공한다.

    사용법:
        engine = MLSignalEngine()
        signals = engine.get_signals(candles, current_position)
        llm_text = engine.format_for_llm_prompt(signals)
    """

    def __init__(self, models_dir: str = DEFAULT_MODELS_DIR):
        self.models_dir = models_dir
        os.makedirs(models_dir, exist_ok=True)

        self.feature_engine = FeatureEngine()
        self.xgb = XGBoostSignal(
            model_path=os.path.join(models_dir, "xgb_signal.json")
        )
        self.lstm = LSTMPredictor(
            model_path=os.path.join(models_dir, "lstm_predictor.pt")
        )
        self.rl = RLTrader(
            model_path=os.path.join(models_dir, "dqn_trader.pt")
        )
        logger.info(f"MLSignalEngine 초기화 완료 (모델 경로: {models_dir})")

    def get_signals(self, candles: list[dict], current_position: Optional[dict] = None) -> dict:
        """단일 자산에 대한 통합 ML 시그널을 반환한다.

        Args:
            candles: OHLCV 캔들 리스트 (최소 30개 권장)
            current_position: 현재 포지션 정보 (없으면 None)

        Returns:
            모든 모델의 시그널 + 컨센서스 + 피처 요약
        """
        features = self.feature_engine.compute_features(candles)
        if not features:
            return self._empty_signal("피처 계산 불가 (캔들 부족)")

        # Phase 1: XGBoost
        xgb_signal = self.xgb.predict(features)

        # Phase 2: LSTM
        sequence = self.feature_engine.compute_feature_sequence(candles)
        lstm_pred = self.lstm.predict(sequence)

        # Phase 3: RL
        rl_action = self.rl.get_action(features, current_position)

        # 컨센서스 계산
        consensus = self._compute_consensus(xgb_signal, lstm_pred, rl_action)

        # 피처 요약
        features_summary = self._summarize_features(features)

        return {
            "xgboost": xgb_signal,
            "lstm": lstm_pred,
            "rl": rl_action,
            "consensus": consensus,
            "features_summary": features_summary,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _empty_signal(self, reason: str) -> dict:
        """빈 시그널 (피처 계산 불가 시)"""
        neutral = {"signal": "hold", "confidence": 0.0, "status": reason}
        return {
            "xgboost": neutral,
            "lstm": {"direction": "neutral", "predicted_change_pct": 0.0, "confidence": 0.0, "status": reason},
            "rl": {"action": "hold", "q_values": {}, "confidence": 0.0, "status": reason},
            "consensus": {"action": "hold", "confidence": 0.0, "agreement": "N/A", "reason": reason},
            "features_summary": {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _compute_consensus(self, xgb: dict, lstm: dict, rl: dict) -> dict:
        """3개 모델의 가중 투표로 컨센서스를 계산한다.

        가중치:
            XGBoost: 0.4 (가장 안정적, 소량 데이터에서도 동작)
            LSTM:    0.3 (방향 예측)
            RL:      0.3 (타이밍 최적화)
        """
        W_XGB = 0.4
        W_LSTM = 0.3
        W_RL = 0.3

        # 각 모델의 시그널을 점수로 변환 (-1=sell, 0=hold, +1=buy)
        scores = {}

        # XGBoost
        xgb_signal = xgb.get("signal", "hold")
        if xgb_signal == "buy":
            xgb_score = xgb.get("buy_prob", 0.5)
        elif xgb_signal == "sell":
            xgb_score = -xgb.get("sell_prob", 0.5)
        else:
            xgb_score = 0.0
        scores["xgboost"] = xgb_score

        # LSTM
        lstm_direction = lstm.get("direction", "neutral")
        lstm_change = lstm.get("predicted_change_pct", 0.0)
        lstm_conf = lstm.get("confidence", 0.0)
        if lstm_direction == "up":
            lstm_score = min(1.0, abs(lstm_change) / 3.0) * lstm_conf
        elif lstm_direction == "down":
            lstm_score = -min(1.0, abs(lstm_change) / 3.0) * lstm_conf
        else:
            lstm_score = 0.0
        scores["lstm"] = lstm_score

        # RL
        rl_action = rl.get("action", "hold")
        rl_conf = rl.get("confidence", 0.0)
        if rl_action == "buy":
            rl_score = rl_conf
        elif rl_action == "sell":
            rl_score = -rl_conf
        else:
            rl_score = 0.0
        scores["rl"] = rl_score

        # 가중 합산
        weighted_score = (
            W_XGB * scores["xgboost"]
            + W_LSTM * scores["lstm"]
            + W_RL * scores["rl"]
        )

        # 최종 판단
        if weighted_score > 0.15:
            action = "buy"
        elif weighted_score < -0.15:
            action = "sell"
        else:
            action = "hold"

        # 동의율 계산
        directions = []
        for s in scores.values():
            if s > 0.1:
                directions.append("buy")
            elif s < -0.1:
                directions.append("sell")
            else:
                directions.append("hold")

        agree_count = directions.count(action)
        agreement = f"{agree_count}/3 모델 동의"

        # 종합 신뢰도
        confidence = abs(weighted_score)

        return {
            "action": action,
            "confidence": round(confidence, 4),
            "weighted_score": round(weighted_score, 4),
            "agreement": agreement,
            "model_scores": {k: round(v, 4) for k, v in scores.items()},
        }

    def _summarize_features(self, features: dict) -> dict:
        """주요 피처를 사람이 읽기 쉬운 형태로 요약한다."""
        rsi = features.get("rsi", 50)
        ema_trend = features.get("ema_trend", 0)
        bb_pct_b = features.get("bb_pct_b", 0.5)
        adx = features.get("adx", 25)
        vol_ratio = features.get("volume_ratio", 1.0)
        atr_pct = features.get("atr_pct", 0)

        # RSI 해석
        if rsi < 25:
            rsi_status = "극도 과매도"
        elif rsi < 30:
            rsi_status = "과매도"
        elif rsi > 75:
            rsi_status = "극도 과매수"
        elif rsi > 70:
            rsi_status = "과매수"
        elif 45 <= rsi <= 55:
            rsi_status = "중립"
        else:
            rsi_status = "정상 범위"

        # EMA 해석
        ema_map = {1.0: "강한 상승 추세", -1.0: "강한 하락 추세", 0.0: "혼합/횡보"}
        ema_status = ema_map.get(ema_trend, "불확실")

        # BB 해석
        if bb_pct_b > 0.95:
            bb_status = "상단 밴드 돌파 (과매수)"
        elif bb_pct_b > 0.8:
            bb_status = "상단 근접"
        elif bb_pct_b < 0.05:
            bb_status = "하단 밴드 돌파 (과매도)"
        elif bb_pct_b < 0.2:
            bb_status = "하단 근접"
        else:
            bb_status = "중간 영역"

        # ADX 해석
        if adx > 40:
            adx_status = "매우 강한 추세"
        elif adx > 25:
            adx_status = "추세 존재"
        else:
            adx_status = "추세 약함/횡보"

        return {
            "rsi": round(rsi, 1),
            "rsi_status": rsi_status,
            "ema_trend": ema_status,
            "bb_position": bb_status,
            "bb_pct_b": round(bb_pct_b, 3),
            "adx": round(adx, 1),
            "adx_status": adx_status,
            "volume_ratio": round(vol_ratio, 2),
            "volatility_pct": round(atr_pct, 2),
            "price_changes": {
                "1d": round(features.get("price_chg_1", 0), 2),
                "3d": round(features.get("price_chg_3", 0), 2),
                "5d": round(features.get("price_chg_5", 0), 2),
                "10d": round(features.get("price_chg_10", 0), 2),
                "20d": round(features.get("price_chg_20", 0), 2),
            },
        }

    def format_for_llm_prompt(self, signals: dict) -> str:
        """ML 시그널을 Gemini 프롬프트에 삽입할 텍스트로 변환한다.

        Returns:
            LLM 프롬프트에 바로 삽입 가능한 문자열
        """
        xgb = signals.get("xgboost", {})
        lstm = signals.get("lstm", {})
        rl = signals.get("rl", {})
        consensus = signals.get("consensus", {})
        summary = signals.get("features_summary", {})

        lines = ["## ML Signal Analysis (Machine Learning 분석)"]

        # XGBoost
        xgb_sig = xgb.get("signal", "N/A").upper()
        xgb_str = xgb.get("strength", 0)
        buy_p = xgb.get("buy_prob", 0)
        sell_p = xgb.get("sell_prob", 0)
        hold_p = xgb.get("hold_prob", 0)
        lines.append(
            f"- XGBoost 분류기: {xgb_sig} (강도={xgb_str:.0%}) "
            f"[매수={buy_p:.0%}, 매도={sell_p:.0%}, 관망={hold_p:.0%}]"
        )

        # LSTM
        lstm_dir = lstm.get("direction", "N/A")
        lstm_change = lstm.get("predicted_change_pct", 0)
        lstm_conf = lstm.get("confidence", 0)
        dir_symbol = {"up": "+", "down": "-", "neutral": "~"}.get(lstm_dir, "?")
        lines.append(
            f"- LSTM 가격 예측: {dir_symbol}{abs(lstm_change):.2f}% "
            f"(방향={lstm_dir}, 신뢰도={lstm_conf:.0%})"
        )

        # RL
        rl_act = rl.get("action", "N/A").upper()
        rl_conf = rl.get("confidence", 0)
        q_vals = rl.get("q_values", {})
        q_str = ", ".join(f"{k}={v:.3f}" for k, v in q_vals.items()) if q_vals else "N/A"
        lines.append(
            f"- RL 에이전트: {rl_act} (신뢰도={rl_conf:.0%}) [Q-values: {q_str}]"
        )

        # Consensus
        cons_act = consensus.get("action", "N/A").upper()
        cons_conf = consensus.get("confidence", 0)
        cons_agree = consensus.get("agreement", "N/A")
        cons_score = consensus.get("weighted_score", 0)
        lines.append(
            f"- **컨센서스: {cons_act}** (신뢰도={cons_conf:.0%}, "
            f"가중 점수={cons_score:+.3f}, {cons_agree})"
        )

        # 피처 요약
        if summary:
            lines.append("")
            lines.append("### 기술적 지표 요약")
            lines.append(f"- RSI: {summary.get('rsi', 'N/A')} ({summary.get('rsi_status', '')})")
            lines.append(f"- EMA 추세: {summary.get('ema_trend', 'N/A')}")
            lines.append(f"- 볼린저 위치: {summary.get('bb_position', 'N/A')} (%B={summary.get('bb_pct_b', 'N/A')})")
            lines.append(f"- ADX 추세강도: {summary.get('adx', 'N/A')} ({summary.get('adx_status', '')})")
            lines.append(f"- 거래량 비율: {summary.get('volume_ratio', 'N/A')}x (20일 평균 대비)")
            lines.append(f"- 변동성 (ATR%): {summary.get('volatility_pct', 'N/A')}%")
            pc = summary.get("price_changes", {})
            if pc:
                lines.append(
                    f"- 가격 변화: 1d={pc.get('1d', 0):+.2f}%, "
                    f"5d={pc.get('5d', 0):+.2f}%, "
                    f"10d={pc.get('10d', 0):+.2f}%, "
                    f"20d={pc.get('20d', 0):+.2f}%"
                )

        return "\n".join(lines)

    def auto_bootstrap(self, candles_by_symbol: dict):
        """여러 종목의 캔들 데이터로 모든 모델을 부트스트랩 학습한다.

        Args:
            candles_by_symbol: {"BTC": [candle1, candle2, ...], "ETH": [...], ...}
        """
        candles_list = list(candles_by_symbol.values())

        logger.info(f"=== 부트스트랩 학습 시작 ({len(candles_list)}개 종목) ===")

        # Phase 1: XGBoost 부트스트랩
        logger.info("--- Phase 1: XGBoost 부트스트랩 ---")
        features, labels = self.xgb.generate_bootstrap_labels(candles_list)
        if features:
            self.xgb.train(features, labels)

        # Phase 2: LSTM 학습
        logger.info("--- Phase 2: LSTM 학습 ---")
        self.lstm.train(candles_list)

        # Phase 3: DQN 학습
        logger.info("--- Phase 3: DQN 학습 ---")
        self.rl.train_from_history(candles_list)

        logger.info("=== 부트스트랩 학습 완료 ===")

    def retrain_all(self, jsonl_path: str = DEFAULT_DECISION_LOG, candles_by_symbol: Optional[dict] = None):
        """모든 모델을 최신 데이터로 재학습한다.

        1. JSONL outcome 데이터가 있으면 XGBoost를 outcome 기반으로 재학습
        2. 캔들 데이터가 있으면 LSTM과 DQN도 재학습
        """
        logger.info("=== 모델 재학습 시작 ===")

        # XGBoost: outcome 기반 재학습
        outcome_features, outcome_labels = self.xgb.generate_labels_from_outcomes(jsonl_path)
        if outcome_features:
            self.xgb.train(outcome_features, outcome_labels)
        else:
            logger.info("Outcome 데이터 없음, XGBoost 재학습 스킵")

        # LSTM, DQN: 캔들 데이터 필요
        if candles_by_symbol:
            candles_list = list(candles_by_symbol.values())
            self.lstm.train(candles_list)
            self.rl.train_from_history(candles_list)
        else:
            logger.info("캔들 데이터 없음, LSTM/DQN 재학습 스킵")

        logger.info("=== 모델 재학습 완료 ===")


# =====================================================================
# 데모 / 테스트 실행
# =====================================================================
def _generate_synthetic_candles(base_price: float = 100.0, n: int = 200, volatility: float = 0.02) -> list[dict]:
    """데모용 합성 캔들 데이터를 생성한다."""
    np.random.seed(42)
    candles = []
    price = base_price

    for i in range(n):
        change = np.random.normal(0, volatility)
        # 간헐적 추세 삽입
        if 50 <= i <= 80:
            change += 0.005  # 상승 추세
        elif 120 <= i <= 150:
            change -= 0.005  # 하락 추세

        open_price = price
        close_price = price * (1 + change)
        high_price = max(open_price, close_price) * (1 + abs(np.random.normal(0, volatility * 0.5)))
        low_price = min(open_price, close_price) * (1 - abs(np.random.normal(0, volatility * 0.5)))
        volume = int(np.random.lognormal(10, 1))

        candles.append({
            "date": f"2026-{(i // 30) + 1:02d}-{(i % 30) + 1:02d}",
            "open": round(open_price, 2),
            "high": round(high_price, 2),
            "low": round(low_price, 2),
            "close": round(close_price, 2),
            "volume": volume,
        })
        price = close_price

    return candles


def _load_real_candles_from_jsonl(jsonl_path: str) -> dict[str, list[dict]]:
    """JSONL 판단 로그에서 종목별 가격 히스토리를 추출한다 (제한적).

    주의: JSONL에는 OHLCV가 완전하지 않으므로 close만 사용한다.
    """
    price_history: dict[str, list] = {}

    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                pair = entry.get("pair", entry.get("symbol", ""))
                price = entry.get("price", 0)
                if not pair or not price:
                    continue

                if pair not in price_history:
                    price_history[pair] = []

                price_history[pair].append({
                    "date": entry.get("timestamp", "")[:10],
                    "open": price,
                    "high": price * 1.005,
                    "low": price * 0.995,
                    "close": price,
                    "volume": 1000,
                })
    except FileNotFoundError:
        pass

    return price_history


def main():
    """데모 실행: 합성 데이터로 전체 파이프라인을 테스트한다."""
    print("=" * 70)
    print("  ML/DL 트레이딩 시그널 엔진 — 데모 실행")
    print("=" * 70)
    print()

    # 1. 합성 캔들 데이터 생성
    print("[1/5] 합성 캔들 데이터 생성...")
    candles_btc = _generate_synthetic_candles(base_price=110000, n=200, volatility=0.02)
    candles_eth = _generate_synthetic_candles(base_price=3000, n=200, volatility=0.025)
    candles_sol = _generate_synthetic_candles(base_price=130, n=200, volatility=0.03)
    candles_by_symbol = {"BTC": candles_btc, "ETH": candles_eth, "SOL": candles_sol}
    print(f"   생성 완료: {len(candles_by_symbol)}개 종목, 각 {len(candles_btc)}개 캔들")
    print()

    # 2. 엔진 초기화
    print("[2/5] MLSignalEngine 초기화...")
    engine = MLSignalEngine()
    print()

    # 3. 부트스트랩 학습
    print("[3/5] 부트스트랩 학습 (3개 모델 동시 학습)...")
    engine.auto_bootstrap(candles_by_symbol)
    print()

    # 4. 시그널 예측
    print("[4/5] 시그널 예측 실행...")
    signals = engine.get_signals(candles_btc, current_position=None)
    print()

    # 5. LLM 프롬프트 출력
    print("[5/5] LLM 프롬프트용 시그널 텍스트:")
    print("-" * 50)
    prompt_text = engine.format_for_llm_prompt(signals)
    print(prompt_text)
    print("-" * 50)
    print()

    # 포지션 보유 중 시나리오 테스트
    print("[추가] 포지션 보유 중 시그널 테스트...")
    position = {"qty": 0.5, "avg_price": 108000, "current_price": 110000}
    signals_with_pos = engine.get_signals(candles_btc, current_position=position)
    print(f"   포지션 보유 시 컨센서스: {signals_with_pos['consensus']['action'].upper()} "
          f"(신뢰도={signals_with_pos['consensus']['confidence']:.0%})")
    print()

    # JSONL 기반 재학습 테스트
    if os.path.exists(DEFAULT_DECISION_LOG):
        print("[추가] JSONL outcome 기반 재학습...")
        engine.retrain_all(DEFAULT_DECISION_LOG, candles_by_symbol)
    else:
        print(f"[참고] JSONL 파일 없음 ({DEFAULT_DECISION_LOG}), 재학습 스킵")

    print()
    print("=" * 70)
    print("  데모 완료")
    print()
    print("  통합 방법:")
    print("    from ml_signal_engine import MLSignalEngine")
    print("    engine = MLSignalEngine()")
    print("    signals = engine.get_signals(candles)")
    print("    prompt_text = engine.format_for_llm_prompt(signals)")
    print("    # -> prompt_text를 Gemini 프롬프트에 삽입")
    print("=" * 70)


if __name__ == "__main__":
    main()
