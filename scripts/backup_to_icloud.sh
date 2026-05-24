#!/bin/bash
# 트레이딩 데이터 → iCloud 자동 백업 스크립트
# 매일 새벽 4시 실행 (launchd)

set -e

SRC="/Users/curara/trading"
DST="$HOME/Library/Mobile Documents/com~apple~CloudDocs/09_소프트웨어/trading_backup"
DATE=$(date +%Y%m%d)
LOG="$SRC/freqtrade_userdata/logs/backup.log"

echo "[$(date)] 백업 시작" >> "$LOG"

# 1. ML 모델 파일 (최신만, ~15MB)
mkdir -p "$DST/models"
rsync -a "$SRC/freqtrade_userdata/models/" "$DST/models/"

# 2. 판단 로그 (JSONL)
mkdir -p "$DST/logs"
for f in gemini_decisions.jsonl stock_decisions.jsonl performance_analysis.jsonl daily_reports.jsonl; do
    [ -f "$SRC/freqtrade_userdata/logs/$f" ] && cp "$SRC/freqtrade_userdata/logs/$f" "$DST/logs/$f"
done

# 3. 전략 파일 (소스코드 백업)
mkdir -p "$DST/strategies"
rsync -a "$SRC/freqtrade_userdata/strategies/" "$DST/strategies/"

# 4. 스크립트 백업
mkdir -p "$DST/scripts"
rsync -a --include="*.py" --include="*.sh" --exclude="*" "$SRC/scripts/" "$DST/scripts/"

# 5. 설정 파일
mkdir -p "$DST/config"
for f in config_upbit_dryrun.json; do
    [ -f "$SRC/freqtrade_userdata/$f" ] && cp "$SRC/freqtrade_userdata/$f" "$DST/config/$f"
done

# 6. 주간 스냅샷 (매주 일요일만 — 로그 전체 아카이브)
if [ "$(date +%u)" = "7" ]; then
    mkdir -p "$DST/weekly_snapshots"
    tar -czf "$DST/weekly_snapshots/logs_${DATE}.tar.gz" \
        -C "$SRC/freqtrade_userdata/logs" \
        gemini_decisions.jsonl stock_decisions.jsonl 2>/dev/null || true
fi

# 용량 리포트
TOTAL=$(du -sh "$DST" 2>/dev/null | cut -f1)
echo "[$(date)] 백업 완료 — 총 $TOTAL" >> "$LOG"
