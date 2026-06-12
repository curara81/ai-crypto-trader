#!/bin/bash
# 트레이딩 데이터 → iCloud 자동 백업 스크립트
# 매일 새벽 4시 실행 (launchd)

set -e

SRC="${TRADING_ROOT:-$HOME/trading}"
DST="${TRADING_BACKUP_DST:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/09_소프트웨어/trading_backup}"
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

# 7. 비대 로그 정리 (v7.2: launchd가 FD를 잡고 있어 rename 로테이션 불가 → in-place truncate)
#    launchd_stderr.log가 929MB까지 자라 디스크를 채우고 ML 재학습을 죽인 사고 재발 방지
for biglog in launchd_stderr.log kis_stockbot_stdout.log kis_stockbot_stderr.log pwa_stderr.log us_paper_bot_stderr.log; do
    f="$SRC/freqtrade_userdata/logs/$biglog"
    if [ -f "$f" ] && [ "$(stat -f%z "$f")" -gt 104857600 ]; then  # 100MB 초과 시
        tail -c 10485760 "$f" > "$f.tmp" && cat "$f.tmp" > "$f" && rm -f "$f.tmp"
        echo "[$(date)] 로그 절단: $biglog (100MB 초과 → 10MB 유지)" >> "$LOG"
    fi
done

# 용량 리포트
TOTAL=$(du -sh "$DST" 2>/dev/null | cut -f1)
echo "[$(date)] 백업 완료 — 총 $TOTAL" >> "$LOG"
