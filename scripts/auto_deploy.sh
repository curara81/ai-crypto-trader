#!/bin/bash
# v3.6: 자동 배포 — 5분 주기 polling
#
# 동작:
#   1. git fetch (조용히)
#   2. 새 커밋 없으면 종료
#   3. pytest 실행 → 실패 시 배포 안 함, 텔레그램 알림
#   4. git pull
#   5. 변경된 파일 분석 → 필요한 서비스만 재시작
#   6. PWA Service Worker 버전 갱신 (commit hash 주입)
#   7. 텔레그램 배포 완료 알림
#   8. 봇 가동 확인 → 실패 시 자동 롤백
#
# 안전장치:
#   - main 브랜치만 자동 배포 (다른 브랜치 무시)
#   - pytest 실패 시 배포 X
#   - 배포 후 30초 안에 봇 응답 없으면 롤백
#   - KILLSWITCH 활성 상태면 배포 보류

set -uo pipefail

TRADING_ROOT="${TRADING_ROOT:-$HOME/trading}"
cd "$TRADING_ROOT" || exit 1

LOG="$TRADING_ROOT/freqtrade_userdata/logs/autodeploy.log"
LOCK="/tmp/freqtrade_autodeploy.lock"
CONFIG="$TRADING_ROOT/freqtrade_userdata/config_upbit_dryrun.json"

# ── 중복 실행 방지 ────────────────────────────
if [ -f "$LOCK" ]; then
    # 5분 이상 된 lock은 stale로 간주
    if [ $(( $(date +%s) - $(stat -f %m "$LOCK") )) -lt 300 ]; then
        echo "[$(date)] 이미 실행 중 (lock)" >> "$LOG"
        exit 0
    fi
fi
trap "rm -f $LOCK" EXIT
echo $$ > "$LOCK"

# ── 헬퍼 ─────────────────────────────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"; }

tg_send() {
    local msg="$1"
    local token chat_id
    token=$(python3 -c "import json; print(json.load(open('$CONFIG'))['telegram']['token'])" 2>/dev/null)
    chat_id=$(python3 -c "import json; print(json.load(open('$CONFIG'))['telegram']['chat_id'])" 2>/dev/null)
    [ -z "$token" ] && return
    curl -s "https://api.telegram.org/bot${token}/sendMessage" \
        -d chat_id="${chat_id}" \
        -d parse_mode="HTML" \
        --data-urlencode text="${msg}" > /dev/null
}

# ── KILLSWITCH 체크 ──────────────────────────
if [ -f "$TRADING_ROOT/KILLSWITCH" ]; then
    log "KILLSWITCH 활성 — 배포 보류"
    exit 0
fi

# ── 1. fetch ──────────────────────────────────
git fetch origin main --quiet 2>/dev/null || { log "git fetch 실패"; exit 1; }

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    # 새 커밋 없음 (정상)
    exit 0
fi

log "신규 커밋 감지: $LOCAL → $REMOTE"
NEW_COMMITS=$(git log --oneline "$LOCAL..$REMOTE")
log "변경 커밋:"
echo "$NEW_COMMITS" | tee -a "$LOG"

# ── 2. pytest (안전 검증) ─────────────────────
log "pytest 실행 중..."
source "$TRADING_ROOT/ft_env/bin/activate"
cd "$TRADING_ROOT"

# 새 코드 미리 체크아웃해서 pytest (실패 시 롤백 안 필요)
git stash --quiet 2>/dev/null || true
git checkout "$REMOTE" -- scripts/ tests/ pwa/ 2>/dev/null

if ! PYTHONPATH=scripts python3 -m pytest tests/ -q --tb=no 2>&1 | tail -3 | tee -a "$LOG"; then
    log "❌ pytest 실패 — 배포 취소"
    git checkout HEAD -- scripts/ tests/ pwa/ 2>/dev/null
    tg_send "🛑 <b>자동 배포 실패</b>%0A%0Apytest 실패로 배포 취소%0A커밋: $REMOTE"
    exit 1
fi
log "✓ pytest 통과"

# 임시 체크아웃 되돌리기 (실제 pull로 처리)
git checkout HEAD -- scripts/ tests/ pwa/ 2>/dev/null

# ── 3. 변경 파일 분석 ─────────────────────────
CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE")
log "변경 파일:"
echo "$CHANGED" | sed 's/^/  /' | tee -a "$LOG"

NEED_BOT_RESTART=0
NEED_PWA_RESTART=0
NEED_KIS_RESTART=0

echo "$CHANGED" | while read -r f; do
    case "$f" in
        freqtrade_userdata/strategies/*)
            echo "BOT" ;;
        pwa/*)
            echo "PWA" ;;
        scripts/kis_stock_bot.py)
            echo "KIS" ;;
        # 봇이 import하는 모든 모듈 → BOT 재시작 (보수적)
        scripts/*.py)
            # 단, KIS bot 전용 파일은 위 case에서 처리됨
            case "$f" in
                scripts/kis_stock_bot.py|scripts/auto_deploy.sh|scripts/daily_report.sh|scripts/ml_retrain.sh|scripts/ml_retrain.py|scripts/health_monitor.py|scripts/generate_report.py|scripts/evaluation_2week.py|scripts/backup_to_icloud.sh|scripts/korean_notifier.py|scripts/telegram_ai_bot.py|scripts/secrets_helper.py)
                    : ;;  # 봇 외 스크립트는 재시작 불필요 (별도 launchd)
                *)
                    echo "BOT" ;;
            esac ;;
    esac
done > /tmp/_deploy_targets

grep -q "BOT" /tmp/_deploy_targets && NEED_BOT_RESTART=1
grep -q "PWA" /tmp/_deploy_targets && NEED_PWA_RESTART=1
grep -q "KIS" /tmp/_deploy_targets && NEED_KIS_RESTART=1
rm -f /tmp/_deploy_targets

# ── 4. git pull ───────────────────────────────
log "git pull..."
PREV_COMMIT="$LOCAL"
git pull origin main --quiet 2>&1 | tee -a "$LOG"

# ── 5. PWA Service Worker 캐시 무효화 ────────
if [ -f "$TRADING_ROOT/pwa/static/sw.js" ]; then
    SHORT_HASH=$(git rev-parse --short HEAD)
    # CACHE = "ai-trader-v1" → "ai-trader-v1-<hash>"
    sed -i.bak "s|ai-trader-v[0-9]*\(-[a-f0-9]*\)\?|ai-trader-v1-${SHORT_HASH}|" \
        "$TRADING_ROOT/pwa/static/sw.js" 2>/dev/null
    rm -f "$TRADING_ROOT/pwa/static/sw.js.bak"
    log "PWA SW 캐시 갱신: ai-trader-v1-${SHORT_HASH}"
fi

# ── 6. 필요한 서비스만 재시작 ────────────────
RESTARTED=""

if [ "$NEED_BOT_RESTART" = "1" ]; then
    log "메인 봇 재시작..."
    touch "$TRADING_ROOT/KILLSWITCH"
    launchctl unload ~/Library/LaunchAgents/com.curara.freqtrade-dryrun.plist 2>/dev/null
    sleep 3
    launchctl load ~/Library/LaunchAgents/com.curara.freqtrade-dryrun.plist
    sleep 10
    rm -f "$TRADING_ROOT/KILLSWITCH"
    RESTARTED="${RESTARTED}봇 "
fi

if [ "$NEED_PWA_RESTART" = "1" ]; then
    log "PWA 재시작..."
    launchctl unload ~/Library/LaunchAgents/com.curara.freqtrade-pwa.plist 2>/dev/null
    sleep 2
    launchctl load ~/Library/LaunchAgents/com.curara.freqtrade-pwa.plist
    RESTARTED="${RESTARTED}PWA "
fi

if [ "$NEED_KIS_RESTART" = "1" ]; then
    log "KIS 봇 재시작..."
    launchctl unload ~/Library/LaunchAgents/com.curara.kis-stockbot.plist 2>/dev/null
    sleep 2
    launchctl load ~/Library/LaunchAgents/com.curara.kis-stockbot.plist
    RESTARTED="${RESTARTED}미주 "
fi

if [ -z "$RESTARTED" ]; then
    RESTARTED="없음 (문서/config만 변경)"
fi

# ── 7. 헬스 체크 (롤백 판단) ─────────────────
if [ "$NEED_BOT_RESTART" = "1" ]; then
    sleep 5
    if ! curl -s -m 5 -u freqtrade:freqtrade http://127.0.0.1:8080/api/v1/ping | grep -q "pong"; then
        log "❌ 봇 응답 없음 → 자동 롤백"
        git reset --hard "$PREV_COMMIT" --quiet
        launchctl unload ~/Library/LaunchAgents/com.curara.freqtrade-dryrun.plist 2>/dev/null
        sleep 3
        launchctl load ~/Library/LaunchAgents/com.curara.freqtrade-dryrun.plist
        tg_send "🔴 <b>자동 배포 롤백</b>%0A%0A봇 응답 없음 → 이전 커밋으로 복원%0A문제 커밋: $REMOTE"
        exit 1
    fi
fi

# ── 8. 성공 알림 ─────────────────────────────
LATEST_MSG=$(git log -1 --pretty=%B | head -1)
log "✅ 배포 완료 — 재시작: $RESTARTED"
tg_send "✅ <b>자동 배포 완료</b>%0A%0A커밋: <code>${LATEST_MSG}</code>%0A재시작: ${RESTARTED}%0A커밋수: $(echo "$NEW_COMMITS" | wc -l | tr -d ' ')"
