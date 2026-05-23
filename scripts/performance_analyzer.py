#!/usr/bin/env python3
"""
Performance Analyzer for GeminiDecisionStrategy
- Correlates AI decisions with actual trade outcomes
- Updates JSONL with win/loss results
- Generates daily performance reports
- Sends summary to Telegram

Runs every 30 minutes via launchd.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import requests

# --- Config ---
FREQTRADE_API = "http://127.0.0.1:8080/api/v1"
FREQTRADE_AUTH = ("freqtrade", "freqtrade")

DECISION_LOG = "/Users/curara/trading/freqtrade_userdata/logs/gemini_decisions.jsonl"
ANALYSIS_LOG = "/Users/curara/trading/freqtrade_userdata/logs/performance_analysis.jsonl"
REPORT_LOG = "/Users/curara/trading/freqtrade_userdata/logs/daily_reports.jsonl"

with open("/Users/curara/trading/freqtrade_userdata/config_upbit_dryrun.json") as f:
    config = json.load(f)

TG_TOKEN = config["telegram"]["token"]
TG_CHAT_ID = str(config["telegram"]["chat_id"])
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def ft_get(endpoint: str, params: dict = None):
    try:
        r = requests.get(f"{FREQTRADE_API}/{endpoint}", auth=FREQTRADE_AUTH,
                         params=params, timeout=10)
        return r.json()
    except Exception:
        return None


def send_tg(msg: str):
    try:
        requests.post(f"{TG_API}/sendMessage", json={
            "chat_id": TG_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
        }, timeout=10)
    except Exception:
        pass


def load_decisions() -> list:
    """Load all decisions from JSONL."""
    decisions = []
    try:
        with open(DECISION_LOG) as f:
            for line in f:
                line = line.strip()
                if line:
                    decisions.append(json.loads(line))
    except FileNotFoundError:
        pass
    return decisions


def get_closed_trades() -> list:
    """Get all closed trades from Freqtrade API."""
    data = ft_get("trades", {"limit": 100})
    if not data:
        return []
    return [t for t in data.get("trades", []) if not t["is_open"]]


def correlate_decisions_with_outcomes(decisions: list, trades: list) -> list:
    """
    Match AI decisions to actual trade outcomes.
    A 'buy' decision within 10 minutes before a trade open = correlated.
    """
    results = []

    for trade in trades:
        pair = trade["pair"]
        open_date_str = trade["open_date"].replace("Z", "+00:00")
        if "+" not in open_date_str and "-" not in open_date_str[10:]:
            open_date_str += "+00:00"
        open_ts = datetime.fromisoformat(open_date_str)
        if open_ts.tzinfo is None:
            open_ts = open_ts.replace(tzinfo=timezone.utc)

        close_ts = None
        if trade.get("close_date"):
            close_date_str = trade["close_date"].replace("Z", "+00:00")
            if "+" not in close_date_str and "-" not in close_date_str[10:]:
                close_date_str += "+00:00"
            close_ts = datetime.fromisoformat(close_date_str)
            if close_ts.tzinfo is None:
                close_ts = close_ts.replace(tzinfo=timezone.utc)
        profit_pct = trade.get("profit_pct", 0)
        exit_reason = trade.get("exit_reason", "unknown")

        # Find buy decisions near trade open time
        matching_buy = None
        for d in decisions:
            if d.get("pair") != pair or d.get("action") != "buy":
                continue
            d_ts = datetime.fromisoformat(d["timestamp"])
            if d_ts.tzinfo is None:
                d_ts = d_ts.replace(tzinfo=timezone.utc)
            diff = abs((open_ts - d_ts).total_seconds())
            if diff < 600:  # within 10 minutes
                matching_buy = d
                break

        if matching_buy:
            results.append({
                "trade_id": trade.get("trade_id"),
                "pair": pair,
                "decision_time": matching_buy["timestamp"],
                "confidence": matching_buy["confidence"],
                "ai_reason": matching_buy["reason"],
                "open_rate": trade.get("open_rate", 0),
                "close_rate": trade.get("close_rate", 0),
                "profit_pct": profit_pct,
                "exit_reason": exit_reason,
                "trade_duration": trade.get("trade_duration", 0),
                "outcome": "win" if profit_pct >= 0 else "loss",
                "indicators": {
                    "rsi": matching_buy.get("rsi", 0),
                    "macd_hist": matching_buy.get("macd_hist", 0),
                    "ema_trend": matching_buy.get("ema_trend", ""),
                    "volume_ratio": matching_buy.get("volume_ratio", 0),
                }
            })

    return results


def analyze_patterns(results: list) -> dict:
    """Analyze win/loss patterns to find what works."""
    if not results:
        return {}

    analysis = {
        "total_trades": len(results),
        "wins": sum(1 for r in results if r["outcome"] == "win"),
        "losses": sum(1 for r in results if r["outcome"] == "loss"),
        "avg_profit": sum(r["profit_pct"] for r in results) / len(results),
    }

    # Win rate by confidence level
    conf_buckets = {"high(0.7+)": [], "med(0.5-0.7)": [], "low(<0.5)": []}
    for r in results:
        c = r["confidence"]
        if c >= 0.7:
            conf_buckets["high(0.7+)"].append(r)
        elif c >= 0.5:
            conf_buckets["med(0.5-0.7)"].append(r)
        else:
            conf_buckets["low(<0.5)"].append(r)

    analysis["by_confidence"] = {}
    for bucket, trades in conf_buckets.items():
        if trades:
            wins = sum(1 for t in trades if t["outcome"] == "win")
            analysis["by_confidence"][bucket] = {
                "count": len(trades),
                "winrate": wins / len(trades) * 100,
                "avg_profit": sum(t["profit_pct"] for t in trades) / len(trades),
            }

    # Win rate by pair
    by_pair = defaultdict(list)
    for r in results:
        by_pair[r["pair"]].append(r)

    analysis["by_pair"] = {}
    for pair, trades in by_pair.items():
        wins = sum(1 for t in trades if t["outcome"] == "win")
        analysis["by_pair"][pair] = {
            "count": len(trades),
            "winrate": wins / len(trades) * 100,
            "avg_profit": sum(t["profit_pct"] for t in trades) / len(trades),
        }

    # Win rate by EMA trend
    by_trend = defaultdict(list)
    for r in results:
        trend = r["indicators"].get("ema_trend", "unknown")
        by_trend[trend].append(r)

    analysis["by_ema_trend"] = {}
    for trend, trades in by_trend.items():
        wins = sum(1 for t in trades if t["outcome"] == "win")
        analysis["by_ema_trend"][trend] = {
            "count": len(trades),
            "winrate": wins / len(trades) * 100,
            "avg_profit": sum(t["profit_pct"] for t in trades) / len(trades),
        }

    # Win rate by RSI zone
    analysis["by_rsi_zone"] = {}
    rsi_zones = {"oversold(<30)": [], "neutral(30-70)": [], "overbought(70+)": []}
    for r in results:
        rsi = r["indicators"].get("rsi", 50)
        if rsi < 30:
            rsi_zones["oversold(<30)"].append(r)
        elif rsi > 70:
            rsi_zones["overbought(70+)"].append(r)
        else:
            rsi_zones["neutral(30-70)"].append(r)

    for zone, trades in rsi_zones.items():
        if trades:
            wins = sum(1 for t in trades if t["outcome"] == "win")
            analysis["by_rsi_zone"][zone] = {
                "count": len(trades),
                "winrate": wins / len(trades) * 100,
                "avg_profit": sum(t["profit_pct"] for t in trades) / len(trades),
            }

    # Exit reason analysis
    by_exit = defaultdict(list)
    for r in results:
        by_exit[r["exit_reason"]].append(r)

    analysis["by_exit_reason"] = {}
    for reason, trades in by_exit.items():
        wins = sum(1 for t in trades if t["outcome"] == "win")
        analysis["by_exit_reason"][reason] = {
            "count": len(trades),
            "winrate": wins / len(trades) * 100,
            "avg_profit": sum(t["profit_pct"] for t in trades) / len(trades),
        }

    # Best/worst trades
    sorted_by_profit = sorted(results, key=lambda x: x["profit_pct"])
    analysis["worst_trade"] = {
        "pair": sorted_by_profit[0]["pair"],
        "profit": sorted_by_profit[0]["profit_pct"],
        "reason": sorted_by_profit[0]["ai_reason"][:80],
    }
    analysis["best_trade"] = {
        "pair": sorted_by_profit[-1]["pair"],
        "profit": sorted_by_profit[-1]["profit_pct"],
        "reason": sorted_by_profit[-1]["ai_reason"][:80],
    }

    return analysis


def generate_report(analysis: dict) -> str:
    """Generate a Korean Telegram report."""
    if not analysis:
        return ""

    total = analysis["total_trades"]
    wins = analysis["wins"]
    losses = analysis["losses"]
    winrate = wins / total * 100 if total > 0 else 0
    avg = analysis["avg_profit"]

    lines = [
        f"📊 <b>AI 트레이딩 성과 분석</b>",
        f"━━━━━━━━━━━━━━",
        f"총 {total}건 | {wins}승 {losses}패 | 승률 {winrate:.1f}%",
        f"평균 수익: {avg:+.2f}%",
        "",
    ]

    # By confidence
    if analysis.get("by_confidence"):
        lines.append("<b>🎯 확신도별 성과</b>")
        for bucket, data in analysis["by_confidence"].items():
            lines.append(f"  {bucket}: {data['count']}건, 승률 {data['winrate']:.0f}%, 평균 {data['avg_profit']:+.2f}%")
        lines.append("")

    # By pair
    if analysis.get("by_pair"):
        lines.append("<b>💰 종목별 성과</b>")
        for pair, data in sorted(analysis["by_pair"].items(), key=lambda x: x[1]["avg_profit"], reverse=True):
            emoji = "🟢" if data["avg_profit"] >= 0 else "🔴"
            lines.append(f"  {emoji} {pair}: {data['count']}건, 승률 {data['winrate']:.0f}%, {data['avg_profit']:+.2f}%")
        lines.append("")

    # By RSI zone
    if analysis.get("by_rsi_zone"):
        lines.append("<b>📈 RSI 구간별 성과</b>")
        for zone, data in analysis["by_rsi_zone"].items():
            lines.append(f"  {zone}: {data['count']}건, 승률 {data['winrate']:.0f}%")
        lines.append("")

    # By exit reason
    if analysis.get("by_exit_reason"):
        lines.append("<b>🚪 청산 사유별</b>")
        for reason, data in analysis["by_exit_reason"].items():
            lines.append(f"  {reason}: {data['count']}건, 승률 {data['winrate']:.0f}%, {data['avg_profit']:+.2f}%")
        lines.append("")

    # Best/worst
    best = analysis.get("best_trade", {})
    worst = analysis.get("worst_trade", {})
    if best:
        lines.append(f"🏆 최고: {best['pair']} {best['profit']:+.2f}%")
    if worst:
        lines.append(f"💀 최악: {worst['pair']} {worst['profit']:+.2f}%")

    # Learning insights
    lines.append("")
    lines.append("<b>🧠 학습 인사이트</b>")

    # Check if high confidence trades perform better
    by_conf = analysis.get("by_confidence", {})
    high = by_conf.get("high(0.7+)", {})
    med = by_conf.get("med(0.5-0.7)", {})
    if high and med:
        if high.get("winrate", 0) > med.get("winrate", 0):
            lines.append("  ✅ 높은 확신도 = 높은 승률 (AI 판단 신뢰도 양호)")
        else:
            lines.append("  ⚠️ 확신도와 승률 불일치 — 판단 기준 재검토 필요")

    # Check EMA trend correlation
    by_trend = analysis.get("by_ema_trend", {})
    bull = by_trend.get("bull", {})
    bear = by_trend.get("bear", {})
    if bull and bear:
        if bull.get("winrate", 0) > bear.get("winrate", 0) + 10:
            lines.append("  ✅ 상승장 매수가 하락장 매수보다 수익 우수 — 추세 추종 유효")
        elif bear.get("winrate", 0) > bull.get("winrate", 0):
            lines.append("  🔄 역추세 매수가 더 좋은 성과 — 반등 매매 강화 고려")

    return "\n".join(lines)


def save_analysis(results: list, analysis: dict):
    """Save analysis results to JSONL."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trade_count": len(results),
        "analysis": analysis,
    }
    try:
        with open(ANALYSIS_LOG, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"Save error: {e}")


def update_decision_outcomes(decisions_file: str, results: list):
    """Update JSONL decisions with trade outcomes (win/loss)."""
    if not results:
        return

    # Build lookup: decision_time+pair -> outcome
    outcome_map = {}
    for r in results:
        key = f"{r['decision_time']}_{r['pair']}"
        outcome_map[key] = f"{r['outcome']}({r['profit_pct']:+.2f}%)"

    # Rewrite file with outcomes added
    try:
        updated_lines = []
        with open(decisions_file) as f:
            for line in f:
                entry = json.loads(line.strip())
                key = f"{entry.get('timestamp', '')}_{entry.get('pair', '')}"
                if key in outcome_map and "outcome" not in entry:
                    entry["outcome"] = outcome_map[key]
                updated_lines.append(json.dumps(entry, ensure_ascii=False))

        with open(decisions_file, "w") as f:
            f.write("\n".join(updated_lines) + "\n")

        log(f"Updated {len(outcome_map)} decision outcomes")
    except Exception as e:
        log(f"Update error: {e}")


# --- Main ---

if __name__ == "__main__":
    log("Performance analyzer starting...")

    while True:
        try:
            decisions = load_decisions()
            trades = get_closed_trades()

            log(f"Loaded {len(decisions)} decisions, {len(trades)} closed trades")

            if decisions and trades:
                # Correlate
                results = correlate_decisions_with_outcomes(decisions, trades)
                log(f"Correlated {len(results)} decision-trade pairs")

                if results:
                    # Analyze patterns
                    analysis = analyze_patterns(results)

                    # Update decisions with outcomes
                    update_decision_outcomes(DECISION_LOG, results)

                    # Save analysis
                    save_analysis(results, analysis)

                    # Generate report
                    report = generate_report(analysis)

                    # Check if we should send report (every 6 hours)
                    should_report = False
                    try:
                        with open(REPORT_LOG) as f:
                            lines = f.readlines()
                            if lines:
                                last = json.loads(lines[-1].strip())
                                last_ts = datetime.fromisoformat(last["timestamp"])
                                if (datetime.now(timezone.utc) - last_ts).total_seconds() > 21600:  # 6h
                                    should_report = True
                            else:
                                should_report = True
                    except FileNotFoundError:
                        should_report = True

                    if should_report and report:
                        send_tg(report)
                        with open(REPORT_LOG, "a") as f:
                            f.write(json.dumps({
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "trades": len(results),
                                "winrate": analysis.get("wins", 0) / max(analysis.get("total_trades", 1), 1) * 100,
                            }) + "\n")
                        log("Report sent to Telegram")

            log("Sleeping 30 minutes...")
            time.sleep(1800)  # 30 minutes

        except KeyboardInterrupt:
            log("Shutting down")
            break
        except Exception as e:
            log(f"ERROR: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(60)
