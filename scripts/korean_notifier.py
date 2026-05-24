#!/usr/bin/env python3
"""
Korean Telegram Notifier for Freqtrade
Polls Freqtrade API and sends Korean-formatted trade notifications.
Runs as a daemon alongside Freqtrade.
"""

import json
import os
import time
import requests
from datetime import datetime

FREQTRADE_API = "http://127.0.0.1:8080/api/v1"
FREQTRADE_AUTH = ("freqtrade", "freqtrade")

TRADING_ROOT = os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading"))
CONFIG_PATH = os.environ.get("FREQTRADE_CONFIG", os.path.join(TRADING_ROOT, "freqtrade_userdata/config_upbit_dryrun.json"))

with open(CONFIG_PATH) as f:
    config = json.load(f)

TG_TOKEN = config["telegram"]["token"]
TG_CHAT_ID = config["telegram"]["chat_id"]
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

known_trades: dict[int, dict] = {}
last_trade_count = 0
bot_was_running = True


def send_tg(msg: str):
    try:
        requests.post(f"{TG_API}/sendMessage", json={
            "chat_id": TG_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception:
        pass


def fmt_krw(val: float) -> str:
    if abs(val) >= 1000:
        return f"{val:,.0f}"
    return f"{val:,.1f}"


def check_open_trades():
    global known_trades
    try:
        resp = requests.get(f"{FREQTRADE_API}/status", auth=FREQTRADE_AUTH, timeout=5)
        trades = resp.json()
    except Exception:
        return

    current_ids = set()
    for t in trades:
        tid = t["trade_id"]
        current_ids.add(tid)

        if tid not in known_trades:
            pair = t["pair"]
            stake = t.get("stake_amount", 0)
            rate = t.get("open_rate", 0)
            msg = (
                f"📈 <b>매수 체결</b>\n"
                f"종목: <code>{pair}</code>\n"
                f"매수가: {fmt_krw(rate)} KRW\n"
                f"투자금: {fmt_krw(stake)} KRW\n"
                f"시간: {datetime.now().strftime('%H:%M:%S')}"
            )
            send_tg(msg)
            known_trades[tid] = {"pair": pair, "open_rate": rate}

    closed = set(known_trades.keys()) - current_ids
    for tid in closed:
        del known_trades[tid]


def check_closed_trades():
    global last_trade_count
    try:
        resp = requests.get(f"{FREQTRADE_API}/profit", auth=FREQTRADE_AUTH, timeout=5)
        data = resp.json()
    except Exception:
        return

    count = data.get("closed_trade_count", 0)
    if count > last_trade_count and last_trade_count > 0:
        try:
            resp = requests.get(
                f"{FREQTRADE_API}/trades",
                auth=FREQTRADE_AUTH,
                params={"limit": 1, "offset": 0},
                timeout=5,
            )
            trades_data = resp.json()
            recent = trades_data.get("trades", [])
            if recent:
                t = recent[0]
                pair = t.get("pair", "?")
                profit = t.get("profit_pct", 0)
                profit_abs = t.get("profit_abs", 0)
                exit_reason = t.get("exit_reason", "?")

                reason_kr = {
                    "roi": "목표수익 도달",
                    "stop_loss": "손절",
                    "stoploss": "손절",
                    "trailing_stop_loss": "트레일링 스탑",
                    "exit_signal": "매도 시그널",
                    "force_exit": "강제 청산",
                }.get(exit_reason, exit_reason)

                emoji = "💰" if profit_abs >= 0 else "📉"
                sign = "+" if profit_abs >= 0 else ""

                msg = (
                    f"{emoji} <b>매도 체결</b>\n"
                    f"종목: <code>{pair}</code>\n"
                    f"수익률: {sign}{profit:.2f}%\n"
                    f"수익금: {sign}{fmt_krw(profit_abs)} KRW\n"
                    f"사유: {reason_kr}\n"
                    f"시간: {datetime.now().strftime('%H:%M:%S')}"
                )
                send_tg(msg)
        except Exception:
            pass

    last_trade_count = count


_failure_streak = 0  # 연속 실패 카운터 (재시작 false alarm 방지)
_GRACE_RETRIES = 12  # 5초 간격 폴링 가정 시 60초 grace period


def check_bot_health():
    """v3.5: grace period로 봇 재시작 시 false alarm 차단.

    1회 실패만으로 알림 보내지 않고 _GRACE_RETRIES (60초) 동안
    연속 실패한 경우에만 알림 발송. 정상 응답 시 카운터 리셋.
    """
    global bot_was_running, _failure_streak
    try:
        resp = requests.get(f"{FREQTRADE_API}/health", auth=FREQTRADE_AUTH, timeout=5)
        data = resp.json()
        is_running = data.get("last_process_ts", 0) > 0
        _failure_streak = 0  # 정상 응답 → 카운터 리셋
        if not is_running and bot_was_running:
            send_tg("⚠️ <b>봇 이상 감지</b>\nFreqtrade 응답 없음. 확인 필요.")
        bot_was_running = is_running
    except Exception:
        _failure_streak += 1
        if _failure_streak >= _GRACE_RETRIES and bot_was_running:
            # 60초 이상 연속 실패한 경우만 진짜 알림
            send_tg(f"🔴 <b>봇 연결 끊김</b>\nFreqtrade API {_failure_streak * 5}초 이상 응답 없음.")
            bot_was_running = False
            _failure_streak = 0  # 알림 후 리셋 (스팸 방지)


def send_daily_summary():
    try:
        resp = requests.get(f"{FREQTRADE_API}/profit", auth=FREQTRADE_AUTH, timeout=5)
        data = resp.json()

        total_profit = data.get("profit_closed_fiat", 0)
        trade_count = data.get("closed_trade_count", 0)
        win_rate = data.get("winrate", 0) * 100
        best_pair = data.get("best_pair", "-")

        resp2 = requests.get(f"{FREQTRADE_API}/balance", auth=FREQTRADE_AUTH, timeout=5)
        bal = resp2.json()
        total_bal = bal.get("total", 0)

        msg = (
            f"📊 <b>일일 리포트</b> ({datetime.now().strftime('%m/%d')})\n"
            f"━━━━━━━━━━━━━━\n"
            f"총 잔고: {fmt_krw(total_bal)} KRW\n"
            f"누적 수익: {'+' if total_profit >= 0 else ''}{fmt_krw(total_profit)} KRW\n"
            f"총 거래: {trade_count}건\n"
            f"승률: {win_rate:.1f}%\n"
            f"최고 종목: {best_pair}\n"
            f"━━━━━━━━━━━━━━\n"
            f"전략: LLMSentimentStrategy"
        )
        send_tg(msg)
    except Exception:
        pass


if __name__ == "__main__":
    send_tg("🟢 <b>한국어 알림 봇 시작</b>\n매수/매도/일일리포트를 한국어로 알려드립니다.")

    loop_count = 0
    daily_sent = False

    while True:
        try:
            check_open_trades()
            check_closed_trades()

            if loop_count % 6 == 0:
                check_bot_health()

            now = datetime.now()
            if now.hour == 9 and now.minute < 2 and not daily_sent:
                send_daily_summary()
                daily_sent = True
            if now.hour == 10:
                daily_sent = False

            loop_count += 1
            time.sleep(15)

        except KeyboardInterrupt:
            send_tg("🔴 <b>한국어 알림 봇 종료</b>")
            break
        except Exception:
            time.sleep(30)
