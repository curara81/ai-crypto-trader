#!/bin/bash
# Weekly auto-reoptimization: downloads fresh data, runs Hyperopt, restarts bot
# Designed to run via launchd every Sunday 04:00 KST

set -euo pipefail

TRADING_DIR="$HOME/trading"
VENV="$TRADING_DIR/ft_env/bin/activate"
CONFIG="$TRADING_DIR/freqtrade_userdata/config_upbit_dryrun.json"
USERDIR="$TRADING_DIR/freqtrade_userdata"
STRATEGY="AdvancedStrategyV2"
LOGFILE="$USERDIR/logs/reoptimize_$(date +%Y%m%d_%H%M%S).log"
PLIST="$HOME/Library/LaunchAgents/com.curara.freqtrade-dryrun.plist"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOGFILE"; }

source "$VENV"
cd "$TRADING_DIR"

log "=== Auto-reoptimization started ==="

# 1. Download latest 90 days of data
END_DATE=$(date -u +%Y%m%d)
START_DATE=$(date -u -v-90d +%Y%m%d)
log "Downloading data: $START_DATE - $END_DATE"

freqtrade download-data \
  --config "$CONFIG" \
  --userdir "$USERDIR" \
  --timerange "$START_DATE-$END_DATE" \
  --timeframe 15m \
  >> "$LOGFILE" 2>&1 || log "WARNING: Data download had issues, continuing with existing data"

# 2. Run Hyperopt (300 epochs, enough for weekly tune)
log "Running Hyperopt (300 epochs)..."
freqtrade hyperopt \
  --strategy "$STRATEGY" \
  --config "$CONFIG" \
  --userdir "$USERDIR" \
  --hyperopt-loss SharpeHyperOptLossDaily \
  --spaces buy sell \
  --epochs 300 \
  --timerange "$START_DATE-$END_DATE" \
  -j 4 \
  >> "$LOGFILE" 2>&1

if [ $? -eq 0 ]; then
  log "Hyperopt completed. Parameters saved to ${STRATEGY}.json"
else
  log "ERROR: Hyperopt failed. Keeping previous parameters."
  exit 1
fi

# 3. Restart bot with new parameters
log "Restarting Freqtrade..."
launchctl unload "$PLIST" 2>/dev/null || true
sleep 3
launchctl load "$PLIST"
sleep 10

# 4. Verify bot is running
if launchctl list | grep -q freqtrade; then
  log "Bot restarted successfully with updated parameters."
else
  log "ERROR: Bot failed to restart!"
  exit 1
fi

# 5. Send Telegram notification
TOKEN=$(python3 -c "import json; c=json.load(open('$CONFIG')); print(c['telegram']['token'])")
CHAT_ID=$(python3 -c "import json; c=json.load(open('$CONFIG')); print(c['telegram']['chat_id'])")

BEST_LINE=$(grep "Best result:" "$LOGFILE" | tail -1 || echo "No best result found")
MSG="🔄 Weekly Reoptimization Complete%0A📅 Data: ${START_DATE}-${END_DATE}%0A📊 ${BEST_LINE}%0A✅ Bot restarted with new params"

curl -s "https://api.telegram.org/bot${TOKEN}/sendMessage?chat_id=${CHAT_ID}&text=${MSG}" > /dev/null 2>&1

log "=== Auto-reoptimization finished ==="
