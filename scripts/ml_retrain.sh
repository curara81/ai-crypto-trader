#!/bin/bash
# ML 모델 주간 재학습 wrapper
# 매주 일요일 새벽 3시 실행 (launchd)
# 실제 학습 로직은 ml_retrain.py 참조 (Freqtrade OHLCV feather 기반)

set -e

TRADING_ROOT="${TRADING_ROOT:-$HOME/trading}"
LOG="$TRADING_ROOT/freqtrade_userdata/logs/ml_retrain.log"
VENV="$TRADING_ROOT/ft_env/bin/activate"

echo "[$(date)] ML 재학습 시작" >> "$LOG"

# 가상환경 활성화
source "$VENV"

# OMP 충돌 방지 (macOS XGBoost+PyTorch libomp 충돌)
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export TRADING_ROOT

cd "$TRADING_ROOT/scripts"
python3 ml_retrain.py >> "$LOG" 2>&1

echo "[$(date)] ML 재학습 완료" >> "$LOG"
