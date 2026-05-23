#!/usr/bin/env python3
"""
AI Telegram Bot for Freqtrade
Natural language Korean commands → Freqtrade API actions.
Uses Claude Haiku for intent parsing.
"""

import json
import os
import time
import requests
from datetime import datetime

# --- Config ---
FREQTRADE_API = "http://127.0.0.1:8080/api/v1"
FREQTRADE_AUTH = ("freqtrade", "freqtrade")

with open("/Users/curara/trading/freqtrade_userdata/config_upbit_dryrun.json") as f:
    config = json.load(f)

TG_TOKEN = config["telegram"]["token"]
TG_CHAT_ID = str(config["telegram"]["chat_id"])
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

LAST_UPDATE_ID = 0

# --- Freqtrade API helpers ---

def ft_get(endpoint: str, params: dict = None) -> dict | list | None:
    try:
        r = requests.get(f"{FREQTRADE_API}/{endpoint}", auth=FREQTRADE_AUTH,
                         params=params, timeout=10)
        return r.json()
    except Exception:
        return None


def ft_post(endpoint: str, data: dict = None) -> dict | None:
    try:
        r = requests.post(f"{FREQTRADE_API}/{endpoint}", auth=FREQTRADE_AUTH,
                          json=data or {}, timeout=10)
        return r.json()
    except Exception:
        return None


def fmt_krw(val: float) -> str:
    if abs(val) >= 1000:
        return f"{val:,.0f}"
    return f"{val:,.1f}"


# --- Command handlers ---

def cmd_profit() -> str:
    d = ft_get("profit")
    if not d:
        return "❌ Freqtrade API 응답 없음"

    return (
        f"📊 <b>수익 현황</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"총 거래: {d.get('trade_count', 0)}건 "
        f"(완료 {d.get('closed_trade_count', 0)}건)\n"
        f"승률: {d.get('winrate', 0) * 100:.1f}% "
        f"({d.get('winning_trades', 0)}승 {d.get('losing_trades', 0)}패)\n"
        f"누적 수익: {'+' if d.get('profit_closed_fiat', 0) >= 0 else ''}"
        f"{fmt_krw(d.get('profit_closed_fiat', 0))} KRW "
        f"({d.get('profit_closed_percent', 0):+.2f}%)\n"
        f"평균 보유: {d.get('avg_duration', '-')}\n"
        f"최고 종목: {d.get('best_pair', '-')}\n"
        f"최대 낙폭: {fmt_krw(d.get('max_drawdown_abs', 0))} KRW"
    )


def cmd_status() -> str:
    trades = ft_get("status")
    if not trades:
        return "📭 현재 보유 포지션 없음. 매수 시그널 대기중."
    if isinstance(trades, dict) and "error" in trades:
        return "📭 현재 보유 포지션 없음. 매수 시그널 대기중."

    lines = [f"📈 <b>보유 포지션</b> ({len(trades)}건)\n━━━━━━━━━━━━━━"]
    for t in trades:
        profit = t.get("profit_pct", 0)
        emoji = "🟢" if profit >= 0 else "🔴"
        profit_abs = t.get('profit_abs', 0)
        sign = "+" if profit_abs >= 0 else ""
        lines.append(
            f"{emoji} <code>{t['pair']}</code> | "
            f"{profit:+.2f}% ({sign}{fmt_krw(profit_abs)} KRW)\n"
            f"   매수: {fmt_krw(t.get('open_rate', 0))} | "
            f"투자: {fmt_krw(t.get('stake_amount', 0))}₩"
        )
    return "\n".join(lines)


def cmd_balance() -> str:
    d = ft_get("balance")
    if not d:
        return "❌ 잔고 조회 실패"

    lines = [
        f"💰 <b>잔고</b>\n━━━━━━━━━━━━━━\n"
        f"총 잔고: {fmt_krw(d.get('total', 0))} KRW"
    ]
    for c in d.get("currencies", []):
        if c.get("balance", 0) > 0 and c["currency"] != "KRW":
            lines.append(
                f"  {c['currency']}: {c['balance']:.6f} "
                f"(≈{fmt_krw(c.get('est_stake', 0))}₩)"
            )
    krw = next((c for c in d.get("currencies", []) if c["currency"] == "KRW"), None)
    if krw:
        lines.append(f"  KRW(현금): {fmt_krw(krw.get('balance', 0))}₩")
    return "\n".join(lines)


def cmd_trades() -> str:
    data = ft_get("trades", {"limit": 10})
    if not data or not data.get("trades"):
        return "📭 거래 내역 없음"

    reason_kr = {
        "roi": "목표수익", "stop_loss": "손절", "stoploss": "손절",
        "trailing_stop_loss": "트레일링", "exit_signal": "매도시그널",
        "force_exit": "강제청산",
    }

    lines = [f"📋 <b>최근 거래</b> (최대 10건)\n━━━━━━━━━━━━━━"]
    for t in data["trades"]:
        s = "⏳" if t["is_open"] else ("✅" if t.get("profit_abs", 0) >= 0 else "❌")
        p = t.get("profit_pct", 0)
        a = t.get("profit_abs", 0)
        r = reason_kr.get(t.get("exit_reason", ""), t.get("exit_reason", "진행중"))
        lines.append(
            f"{s} <code>{t['pair']:10s}</code> | "
            f"{p:+.2f}% ({fmt_krw(a)}₩) | {r}"
        )
    return "\n".join(lines)


def cmd_forceexit_all() -> str:
    result = ft_post("forceexit", {"tradeid": "all"})
    if result:
        return "⚡ 전체 포지션 강제 청산 완료"
    return "❌ 강제 청산 실패"


def cmd_start_bot() -> str:
    result = ft_post("start")
    return f"▶️ 봇 시작: {result.get('status', '?')}" if result else "❌ 시작 실패"


def cmd_stop_bot() -> str:
    result = ft_post("stop")
    return f"⏹️ 봇 정지: {result.get('status', '?')}" if result else "❌ 정지 실패"


def cmd_performance() -> str:
    perfs = ft_get("performance")
    if not perfs:
        return "📭 성과 데이터 없음"

    lines = [f"🏆 <b>종목별 성과</b>\n━━━━━━━━━━━━━━"]
    for p in perfs[:8]:
        lines.append(
            f"  <code>{p['pair']:10s}</code> | "
            f"{p.get('profit_pct', 0):+.2f}% | {p.get('count', 0)}건"
        )
    return "\n".join(lines)


def cmd_orderbook() -> str:
    """Show order book summary for all active positions + top pairs."""
    pairs = ["KRW-BTC", "KRW-ETH", "KRW-XRP", "KRW-SOL", "KRW-ADA"]
    lines = [f"📗 <b>호가창 요약</b>\n━━━━━━━━━━━━━━"]

    for market in pairs:
        try:
            r = requests.get(
                "https://api.upbit.com/v1/orderbook",
                params={"markets": market},
                timeout=5,
            )
            data = r.json()
            if not data:
                continue
            ob = data[0]
            units = ob.get("orderbook_units", [])
            if not units:
                continue

            coin = market.split("-")[1]
            best_bid = units[0]["bid_price"]
            best_ask = units[0]["ask_price"]
            spread = (best_ask - best_bid) / best_bid * 100

            total_bid = sum(u["bid_size"] for u in units[:10])
            total_ask = sum(u["ask_size"] for u in units[:10])
            ratio = total_bid / max(total_ask, 0.001)

            if ratio > 1.2:
                pressure = "🟢매수우세"
            elif ratio < 0.8:
                pressure = "🔴매도우세"
            else:
                pressure = "⚪균형"

            lines.append(
                f"<code>{coin:5s}</code> | "
                f"스프레드 {spread:.3f}% | "
                f"B/A {ratio:.2f} | {pressure}"
            )
        except Exception:
            continue

    return "\n".join(lines)


def cmd_ai_analysis() -> str:
    """Show latest AI decision analysis from JSONL log."""
    log_file = "/Users/curara/trading/freqtrade_userdata/logs/gemini_decisions.jsonl"
    try:
        decisions = {}
        with open(log_file) as f:
            for line in f:
                entry = json.loads(line.strip())
                decisions[entry["pair"]] = entry  # keep latest per pair

        if not decisions:
            return "📭 AI 판단 기록 없음"

        lines = [f"🧠 <b>AI 최신 판단</b>\n━━━━━━━━━━━━━━"]
        for pair, d in sorted(decisions.items()):
            action = d.get("action", "?")
            conf = d.get("confidence", 0)
            reason = d.get("reason", "")[:60]
            outcome = d.get("outcome", "")
            ts = d.get("timestamp", "")[-8:-3]  # HH:MM

            action_emoji = {"buy": "🟢매수", "sell": "🔴매도", "hold": "⏸관망"}.get(action, action)

            outcome_str = ""
            if outcome:
                outcome_str = f" → {outcome}"

            lines.append(
                f"<code>{pair:10s}</code> {action_emoji} "
                f"({conf:.0%}){outcome_str}\n"
                f"  └ {reason}"
            )

        return "\n".join(lines)
    except Exception:
        return "❌ AI 분석 조회 실패"


def cmd_help() -> str:
    return (
        "🤖 <b>AI 트레이딩 봇 명령어</b>\n"
        "━━━━━━━━━━━━━━\n"
        "자연어로 물어보세요!\n\n"
        "📊 현황:\n"
        '• "수익률" / "잔고" / "보유현황"\n'
        '• "거래내역" / "종목별 성과"\n'
        "🧠 AI:\n"
        '• "AI 분석" — AI 최신 판단 보기\n'
        '• "호가" — 실시간 호가창 분석\n'
        "⚡ 제어:\n"
        '• "전부 팔아" / "봇 멈춰" / "봇 시작"\n'
    )


# --- LLM Intent Parser ---

INTENT_MAP = {
    "profit": cmd_profit,
    "status": cmd_status,
    "balance": cmd_balance,
    "trades": cmd_trades,
    "performance": cmd_performance,
    "forceexit_all": cmd_forceexit_all,
    "start": cmd_start_bot,
    "stop": cmd_stop_bot,
    "help": cmd_help,
    "orderbook": cmd_orderbook,
    "ai_analysis": cmd_ai_analysis,
}

SYSTEM_PROMPT = """You are an intent classifier for a Korean crypto trading bot.
Given a Korean message, return ONLY a JSON object with the intent.

Available intents:
- "profit": 수익률, 수익, 손익, 얼마 벌었어, 성적
- "status": 현재 포지션, 뭐 들고 있어, 보유 현황, 매수중인거
- "balance": 잔고, 잔액, 돈 얼마
- "trades": 거래 내역, 최근 거래, 매수매도 기록
- "performance": 종목별 성과, 어떤 코인이 잘돼
- "forceexit_all": 전부 팔아, 다 청산, 전체 매도
- "start": 봇 시작, 봇 켜, 다시 시작
- "stop": 봇 멈춰, 봇 꺼, 봇 정지
- "help": 도움말, 뭐 할 수 있어, 명령어
- "unknown": 위에 해당 안되는 것

Respond ONLY: {"intent": "<intent>"}"""


def parse_intent(text: str) -> str:
    """Use Claude Haiku to parse Korean natural language into intent."""
    # Quick keyword matching first (saves API calls)
    text_lower = text.strip().lower()

    # Priority order: specific first, generic last
    # "얼마" removed from profit (too ambiguous)
    keyword_map = [
        ("forceexit_all", ["전부 팔", "다 팔", "청산", "전체 매도", "다 매도"]),
        ("balance", ["잔고", "잔액", "돈 얼마", "돈"]),
        ("status", ["포지션", "들고", "보유", "매수중", "뭐 사"]),
        ("trades", ["거래", "내역", "기록", "매매"]),
        ("performance", ["성과", "종목별", "어떤 코인"]),
        ("profit", ["수익", "손익", "벌었", "잃었", "성적", "수익률"]),
        ("stop", ["멈춰", "정지", "봇 꺼", "봇 멈"]),
        ("start", ["시작", "봇 켜", "다시 켜"]),
        ("help", ["도움", "명령어", "뭐 할 수", "뭐 돼", "사용법"]),
        ("orderbook", ["호가", "오더북", "매수벽", "매도벽", "스프레드"]),
        ("ai_analysis", ["ai 분석", "ai분석", "AI분석", "판단", "예측", "분석"]),
    ]

    for intent, keywords in keyword_map:
        for kw in keywords:
            if kw in text_lower:
                return intent

    # Fallback to LLM
    if not ANTHROPIC_KEY:
        return "unknown"

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 30,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": text}],
            },
            timeout=10,
        )
        data = resp.json()
        content = data["content"][0]["text"].strip()
        result = json.loads(content)
        return result.get("intent", "unknown")
    except Exception:
        return "unknown"


# --- Telegram ---

def send_tg(msg: str):
    try:
        requests.post(f"{TG_API}/sendMessage", json={
            "chat_id": TG_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception:
        pass


def get_updates(offset: int) -> list:
    try:
        r = requests.get(f"{TG_API}/getUpdates", params={
            "offset": offset, "timeout": 30, "limit": 10,
        }, timeout=35)
        return r.json().get("result", [])
    except Exception:
        return []


SLASH_MAP = {
    "/profit": "profit",
    "/daily": "profit",
    "/status": "status",
    "/balance": "balance",
    "/count": "status",
    "/performance": "performance",
    "/start": "start",
    "/stop": "stop",
    "/help": "help",
    "/status table": "status",
    "/orderbook": "orderbook",
    "/ai": "ai_analysis",
}


def handle_message(text: str):
    """Process a user message and respond."""
    # Handle slash commands directly
    if text.startswith("/"):
        cmd = text.strip().lower().split("@")[0]  # remove @botname
        intent = SLASH_MAP.get(cmd, None)
        if intent is None:
            # Unknown slash command — try natural language parse
            intent = parse_intent(text.lstrip("/"))
    else:
        intent = parse_intent(text)

    handler = INTENT_MAP.get(intent)
    if handler:
        response = handler()
    else:
        response = (
            f"🤔 무슨 말인지 잘 모르겠어요.\n"
            f'"/도움말" 또는 "뭐 할 수 있어?" 라고 물어보세요!'
        )

    send_tg(response)


# --- Main loop ---

import sys
import traceback

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


if __name__ == "__main__":
    log("AI bot starting...")

    send_tg(
        "🤖 <b>AI 트레이딩 봇 준비 완료</b>\n"
        "자연어로 명령하세요!\n"
        '"수익률 보여줘", "잔고 얼마?", "도움말" 등'
    )
    log("Startup message sent")

    # Get current update_id to skip old messages
    updates = get_updates(0)
    if updates:
        LAST_UPDATE_ID = updates[-1]["update_id"] + 1
    log(f"Initial offset: {LAST_UPDATE_ID}")

    while True:
        try:
            updates = get_updates(LAST_UPDATE_ID)
            if updates:
                log(f"Got {len(updates)} updates")

            for update in updates:
                LAST_UPDATE_ID = update["update_id"] + 1

                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")

                log(f"Message from {chat_id}: '{text}'")

                # Only respond to our chat
                if chat_id != TG_CHAT_ID or not text:
                    log(f"Skipping: chat_id mismatch or empty (expected {TG_CHAT_ID})")
                    continue

                handle_message(text)
                log(f"Handled: '{text}'")

        except KeyboardInterrupt:
            send_tg("🔴 <b>AI 봇 종료</b>")
            break
        except Exception as e:
            log(f"ERROR: {e}")
            traceback.print_exc(file=sys.stdout)
            sys.stdout.flush()
            time.sleep(5)
