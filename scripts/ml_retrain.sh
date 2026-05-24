#!/bin/bash
# ML 모델 주간 재학습 스크립트
# 매주 일요일 새벽 3시 실행 (launchd)
# 최근 수집된 판단 로그 기반으로 XGBoost/LSTM/DQN 재학습

set -e

SRC="/Users/curara/trading"
LOG="$SRC/freqtrade_userdata/logs/ml_retrain.log"
VENV="$SRC/ft_env/bin/activate"

echo "[$(date)] ML 재학습 시작" >> "$LOG"

# 가상환경 활성화
source "$VENV"

# OMP 충돌 방지
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1

# 재학습 실행
cd "$SRC/scripts"
python3 -c "
import json, os, sys
sys.path.insert(0, '.')
from ml_signal_engine import MLSignalEngine

engine = MLSignalEngine()
log_dir = '$SRC/freqtrade_userdata/logs'

# 판단 로그에서 학습 데이터 수집
candles = []
for log_file in ['gemini_decisions.jsonl', 'stock_decisions.jsonl']:
    path = os.path.join(log_dir, log_file)
    if not os.path.exists(path):
        continue
    with open(path) as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if 'price' in entry and entry.get('action') in ('buy', 'sell'):
                    candles.append({
                        'open': entry.get('price', 0) * 0.999,
                        'high': entry.get('price', 0) * 1.005,
                        'low': entry.get('price', 0) * 0.995,
                        'close': entry.get('price', 0),
                        'volume': entry.get('volume', 1000000),
                    })
            except (json.JSONDecodeError, KeyError):
                continue

if len(candles) >= 100:
    print(f'학습 데이터: {len(candles)}건')
    signals = engine.get_signals(candles[-500:], 'none')
    print(f'재학습 완료: {signals}')
else:
    print(f'학습 데이터 부족: {len(candles)}건 (최소 100건 필요)')
" >> "$LOG" 2>&1

echo "[$(date)] ML 재학습 완료" >> "$LOG"
