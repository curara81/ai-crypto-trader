#!/bin/bash
# 일일 성과 리포트 생성 + 텔레그램 전송 + iCloud 보관
# 매일 09:00 KST에 launchd에서 실행

set -e

TRADING_ROOT="${TRADING_ROOT:-$HOME/trading}"
CONFIG="${FREQTRADE_CONFIG:-$TRADING_ROOT/freqtrade_userdata/config_upbit_dryrun.json}"
LOG="$TRADING_ROOT/freqtrade_userdata/logs/daily_report.log"
TODAY=$(date +%Y%m%d)
REPORT_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/_trading_reports"
REPORT_FILE="$REPORT_DIR/report_${TODAY}.md"

echo "[$(date)] Daily report start" >> "$LOG"

mkdir -p "$REPORT_DIR"
source "$TRADING_ROOT/ft_env/bin/activate"

# 1) 리포트 생성 (최근 1일)
python3 "$TRADING_ROOT/scripts/generate_report.py" --days 1 --out "$REPORT_FILE" 2>> "$LOG"

# 2) 어제 vs 그저께 비교 위해 최근 7일 리포트도 생성
WEEKLY_FILE="$REPORT_DIR/weekly_${TODAY}.md"
python3 "$TRADING_ROOT/scripts/generate_report.py" --days 7 --out "$WEEKLY_FILE" 2>> "$LOG"

# 3) Freqtrade API에서 핵심 지표 추출
PROFIT_JSON=$(curl -s -u freqtrade:freqtrade http://127.0.0.1:8080/api/v1/profit 2>/dev/null || echo "{}")
SUMMARY=$(python3 <<PYEOF
import json
d = json.loads('''$PROFIT_JSON''')
if not d:
    print("Freqtrade API 응답 없음")
else:
    print(f"누적: {d.get('trade_count', 0)} trades")
    print(f"손익: {d.get('profit_all_fiat', 0):+,.0f} KRW ({d.get('profit_all_percent', 0):+.2f}%)")
    print(f"승률: closed {d.get('closed_trade_count', 0)}건 / win rate 추정")
    bp = d.get('best_pair', '')
    if bp:
        print(f"Best: {bp} ({d.get('best_pair_profit_ratio', 0)*100:+.2f}%)")
PYEOF
)

# 4) 텔레그램 전송
TG_TOKEN=$(python3 -c "import json; print(json.load(open('$CONFIG'))['telegram']['token'])")
TG_CHAT=$(python3 -c "import json; print(json.load(open('$CONFIG'))['telegram']['chat_id'])")

MSG="📊 일일 리포트 ($TODAY)

$SUMMARY

상세: ~/iCloud Drive/_trading_reports/
- report_${TODAY}.md (1일)
- weekly_${TODAY}.md (7일)"

curl -s "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
  -d chat_id="${TG_CHAT}" \
  -d text="${MSG}" \
  > /dev/null 2>&1

# 5) 30일 이상 된 리포트 정리
find "$REPORT_DIR" -name "report_*.md" -mtime +30 -delete 2>/dev/null || true
find "$REPORT_DIR" -name "weekly_*.md" -mtime +60 -delete 2>/dev/null || true

echo "[$(date)] Daily report done — $REPORT_FILE" >> "$LOG"
