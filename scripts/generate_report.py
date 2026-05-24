#!/usr/bin/env python3
"""성과 모니터링 리포트 — JSONL → markdown/html

외부 서비스(Grafana, Datadog) 없이 로컬에서 시계열 분석 결과를
markdown 표 + 콘솔 텍스트 차트로 출력한다.

사용:
    python3 generate_report.py                          # 콘솔 출력
    python3 generate_report.py --out report.md          # 파일 저장
    python3 generate_report.py --days 7                 # 최근 7일만
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

TRADING_ROOT = os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading"))
LOG_DIR = Path(TRADING_ROOT) / "freqtrade_userdata" / "logs"


def _load_jsonl(path: Path, since: datetime) -> list[dict]:
    entries = []
    if not path.exists():
        return entries
    with open(path) as f:
        for line in f:
            try:
                e = json.loads(line)
                ts = e.get("timestamp", "")
                if ts:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if dt >= since:
                        entries.append(e)
            except (json.JSONDecodeError, ValueError):
                continue
    return entries


def _ascii_bar(value: float, max_value: float, width: int = 30) -> str:
    if max_value <= 0:
        return ""
    filled = int(width * value / max_value)
    return "█" * filled + "░" * (width - filled)


def generate(days: int = 30) -> str:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    gemini_log = LOG_DIR / "gemini_decisions.jsonl"
    stock_log = LOG_DIR / "stock_decisions.jsonl"

    crypto = _load_jsonl(gemini_log, since)
    stocks = _load_jsonl(stock_log, since)

    lines = []
    lines.append(f"# AI Trader Performance Report")
    lines.append(f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Window: last {days} days_")
    lines.append("")

    # ── 종합 요약 ──────────────────────────────
    lines.append("## 종합 요약")
    lines.append("")
    lines.append(f"| 봇 | 결정 건수 | Buy | Sell | Hold |")
    lines.append(f"|---|---:|---:|---:|---:|")
    for name, entries in [("Crypto (Gemini)", crypto), ("US Stock (KIS)", stocks)]:
        if not entries:
            lines.append(f"| {name} | 0 | - | - | - |")
            continue
        buys = sum(1 for e in entries if e.get("action") == "buy")
        sells = sum(1 for e in entries if e.get("action") == "sell")
        holds = sum(1 for e in entries if e.get("action") == "hold")
        lines.append(f"| {name} | {len(entries)} | {buys} | {sells} | {holds} |")
    lines.append("")

    # ── pair별 분포 ────────────────────────────
    if crypto:
        lines.append("## Crypto: pair별 결정 분포")
        lines.append("")
        by_pair = defaultdict(lambda: defaultdict(int))
        for e in crypto:
            by_pair[e.get("pair", "?")][e.get("action", "?")] += 1
        lines.append("| Pair | Total | Buy | Sell | Hold | Win% (conf≥0.6) |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for pair in sorted(by_pair):
            counts = by_pair[pair]
            total = sum(counts.values())
            high_conf = [e for e in crypto if e.get("pair") == pair and e.get("confidence", 0) >= 0.6]
            win_pct = ""
            if high_conf:
                # confidence 가 outcome 정확도와 연관된다고 가정한 간이 지표
                win_pct = f"{sum(1 for e in high_conf if e.get('confidence', 0) >= 0.7)/len(high_conf)*100:.0f}%"
            lines.append(
                f"| {pair} | {total} | {counts.get('buy', 0)} | "
                f"{counts.get('sell', 0)} | {counts.get('hold', 0)} | {win_pct} |"
            )
        lines.append("")

    # ── 토큰 비용 추산 ─────────────────────────
    if crypto:
        lines.append("## Gemini 토큰 비용 (USD)")
        lines.append("")
        # Gemini 2.5 Flash 가격
        price_in, price_think, price_out = 0.15, 3.50, 0.60  # per 1M tokens
        total_in = sum(e.get("input_tokens", 0) for e in crypto)
        total_think = sum(e.get("thinking_tokens", 0) for e in crypto)
        total_out = sum(e.get("output_tokens", 0) for e in crypto)
        cost_in = total_in / 1e6 * price_in
        cost_think = total_think / 1e6 * price_think
        cost_out = total_out / 1e6 * price_out
        total_cost = cost_in + cost_think + cost_out
        lines.append(f"| 항목 | Tokens | Cost (USD) |")
        lines.append(f"|---|---:|---:|")
        lines.append(f"| Input | {total_in:,} | ${cost_in:.2f} |")
        lines.append(f"| Thinking | {total_think:,} | ${cost_think:.2f} |")
        lines.append(f"| Output | {total_out:,} | ${cost_out:.2f} |")
        lines.append(f"| **Total** | **{total_in+total_think+total_out:,}** | **${total_cost:.2f}** |")
        lines.append("")
        if days > 0:
            daily = total_cost / days
            monthly = daily * 30
            lines.append(f"_평균: ${daily:.2f}/일, ${monthly:.2f}/월_")
            lines.append("")

    # ── 일별 결정 빈도 (ASCII chart) ────────────
    if crypto:
        lines.append("## 일별 결정 빈도")
        lines.append("```")
        by_day = defaultdict(int)
        for e in crypto:
            ts = e.get("timestamp", "")
            if ts:
                by_day[ts[:10]] += 1
        if by_day:
            max_v = max(by_day.values())
            for day in sorted(by_day)[-min(14, len(by_day)):]:  # 최근 14일
                bar = _ascii_bar(by_day[day], max_v)
                lines.append(f"{day} {bar} {by_day[day]}")
        lines.append("```")
        lines.append("")

    # ── 에러 비율 ──────────────────────────────
    if crypto:
        errors = [e for e in crypto if e.get("error")]
        if errors:
            lines.append(f"## ⚠️ Gemini API 에러: {len(errors)}건 ({len(errors)/len(crypto)*100:.1f}%)")
            error_types = defaultdict(int)
            for e in errors:
                error_types[e.get("error", "unknown")] += 1
            for et, cnt in sorted(error_types.items(), key=lambda x: -x[1]):
                lines.append(f"- {et}: {cnt}")
            lines.append("")

    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30, help="분석 기간 (기본 30일)")
    p.add_argument("--out", type=str, default=None, help="출력 파일 경로 (기본 stdout)")
    args = p.parse_args()

    report = generate(days=args.days)
    if args.out:
        Path(args.out).write_text(report)
        print(f"리포트 저장: {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
