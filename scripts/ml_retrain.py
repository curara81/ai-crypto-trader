#!/usr/bin/env python3
"""
ML 모델 재학습 — 실제 OHLCV 데이터 기반

Freqtrade가 다운로드한 feather/json 파일(freqtrade_userdata/data/<exchange>/)을
읽어 XGBoost와 LSTM을 학습한다. 결정 로그의 점-price만으로 가짜 OHLCV를
만드는 이전 방식과 달리 실제 5분봉을 사용한다.

전제:
    - freqtrade download-data로 미리 데이터가 받아져 있어야 한다
    - 매주 auto_reoptimize.sh가 다운로드를 수행하므로 일반적으로 충족됨

사용법:
    TRADING_ROOT=~/trading python3 ml_retrain.py
    # 또는 ml_retrain.sh 가 호출
"""

import glob
import logging
import os
import sys
from pathlib import Path

# OMP 충돌 방지 (XGBoost+PyTorch libomp)
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

TRADING_ROOT = os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading"))
SCRIPTS_DIR = os.path.join(TRADING_ROOT, "scripts")
DATA_DIR = Path(TRADING_ROOT) / "freqtrade_userdata" / "data"

sys.path.insert(0, SCRIPTS_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ml_retrain")


def _load_pair_candles(path: Path) -> list[dict]:
    """단일 pair의 feather/json 파일을 OHLCV dict 리스트로 변환한다."""
    try:
        if path.suffix == ".feather":
            import pandas as pd
            df = pd.read_feather(path)
        elif path.suffix == ".json":
            import pandas as pd
            df = pd.read_json(path)
            # Freqtrade JSON: [[date, open, high, low, close, volume], ...]
            if df.shape[1] == 6:
                df.columns = ["date", "open", "high", "low", "close", "volume"]
        else:
            return []

        # 필수 컬럼 확인
        required = {"open", "high", "low", "close", "volume"}
        if not required.issubset(df.columns):
            logger.warning(f"{path.name}: 필수 컬럼 누락 ({df.columns.tolist()})")
            return []

        candles = [
            {
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["volume"]),
            }
            for _, r in df.iterrows()
            if r["close"] and not (r["close"] != r["close"])  # NaN check
        ]
        return candles
    except Exception as e:
        logger.warning(f"{path.name} 로드 실패: {e}")
        return []


def discover_pair_files() -> list[Path]:
    """data/<exchange>/ 아래에서 5분봉 파일들을 찾는다."""
    if not DATA_DIR.exists():
        logger.error(f"데이터 디렉토리 없음: {DATA_DIR}")
        logger.error("freqtrade download-data 를 먼저 실행하세요")
        return []

    candidates = []
    for ext in ("feather", "json"):
        # 5분봉 우선, 없으면 15분봉
        for pattern in (f"**/*-5m.{ext}", f"**/*-15m.{ext}"):
            candidates.extend(DATA_DIR.glob(pattern))
        if candidates:
            break
    return sorted(set(candidates))


def main() -> int:
    files = discover_pair_files()
    if not files:
        logger.error("학습할 OHLCV 파일이 없습니다")
        return 1

    logger.info(f"발견된 pair 파일: {len(files)}개")
    candles_list = []
    for f in files:
        candles = _load_pair_candles(f)
        if len(candles) >= 200:  # 최소 200캔들 보유한 pair만 사용
            candles_list.append(candles)
            logger.info(f"  {f.name}: {len(candles)} candles")

    if not candles_list:
        logger.error("최소 200캔들 이상의 pair가 없습니다")
        return 1

    total_candles = sum(len(c) for c in candles_list)
    logger.info(f"학습 데이터 준비: {len(candles_list)} pairs / {total_candles} candles")

    # ML 엔진 import (지연 로딩으로 의존성 누락 시 명확한 에러)
    try:
        from ml_signal_engine import XGBoostSignal, LSTMPredictor
    except ImportError as e:
        logger.error(f"ml_signal_engine import 실패: {e}")
        return 2

    # ── XGBoost 학습 ──────────────────────────────────────────
    logger.info("=== XGBoost 학습 시작 ===")
    try:
        xgb = XGBoostSignal()
        features, labels = xgb.generate_bootstrap_labels(candles_list)
        if len(features) >= 100:
            xgb.train(features, labels, validate=True)
            logger.info("XGBoost 학습 완료")
        else:
            logger.warning(f"XGBoost 학습 데이터 부족: {len(features)}건 (최소 100건)")
    except Exception as e:
        logger.error(f"XGBoost 학습 실패: {e}")

    # ── LSTM 학습 ─────────────────────────────────────────────
    logger.info("=== LSTM 학습 시작 ===")
    try:
        lstm = LSTMPredictor()
        # LSTM은 캔들 시계열 자체를 받아 슬라이딩 윈도우로 학습
        lstm.train(candles_list, epochs=30, predict_ahead=5)
        logger.info("LSTM 학습 완료")
    except Exception as e:
        logger.error(f"LSTM 학습 실패: {e}")

    # DQN은 시뮬레이션 시간이 길어 주간 재학습에 부적합 → 별도 스크립트로 분리 권장
    logger.info("DQN 재학습은 스킵 (수동 실행 권장: train_from_history)")

    logger.info("=== 재학습 완료 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
