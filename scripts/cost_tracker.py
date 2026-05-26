#!/usr/bin/env python3
"""LLM API 비용 추적기.

모든 LLM 호출의 토큰 사용량과 비용을 JSON 파일에 기록.
일별/주별/월별 비용 집계 및 리포트 생성.

사용:
    tracker = CostTracker()
    tracker.record(provider="gemini-vertex", model="gemini-2.5-flash",
                   input_tokens=1000, output_tokens=200, thinking_tokens=500)
    print(tracker.daily_report())
"""
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TRADING_ROOT = os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading"))
COST_LOG_DIR = os.path.join(TRADING_ROOT, "freqtrade_userdata/logs")
COST_LOG_FILE = os.path.join(COST_LOG_DIR, "llm_costs.jsonl")

# 가격표 (USD per 1M tokens, 2025-05 기준)
PRICING = {
    "gemini-2.5-flash": {"input": 0.15, "thinking": 3.50, "output": 0.60},
    "gemini-2.5-flash-lite": {"input": 0.075, "thinking": 0.0, "output": 0.30},
    "gemini-2.5-pro": {"input": 1.25, "thinking": 10.00, "output": 5.00},
    "claude-sonnet-4-6": {"input": 3.00, "thinking": 0.0, "output": 15.00},
    "claude-haiku-4-5": {"input": 0.80, "thinking": 0.0, "output": 4.00},
    "ollama-local": {"input": 0.0, "thinking": 0.0, "output": 0.0},
}


def _compute_cost(model: str, input_tokens: int, output_tokens: int,
                  thinking_tokens: int = 0) -> float:
    prices = PRICING.get(model, PRICING.get("gemini-2.5-flash"))
    cost = (
        input_tokens * prices["input"] / 1_000_000
        + thinking_tokens * prices["thinking"] / 1_000_000
        + output_tokens * prices["output"] / 1_000_000
    )
    return round(cost, 6)


class CostTracker:

    def __init__(self, log_file: str = COST_LOG_FILE):
        self._log_file = log_file
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        self._session_cost = 0.0
        self._session_calls = 0

    def record(self, provider: str, model: str,
               input_tokens: int = 0, output_tokens: int = 0,
               thinking_tokens: int = 0, symbol: str = "",
               latency_ms: int = 0) -> float:
        cost = _compute_cost(model, input_tokens, output_tokens, thinking_tokens)
        self._session_cost += cost
        self._session_calls += 1

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "thinking_tokens": thinking_tokens,
            "cost_usd": cost,
            "symbol": symbol,
            "latency_ms": latency_ms,
        }
        try:
            with open(self._log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            logger.warning(f"Cost log write failed: {e}")
        return cost

    def _load_entries(self, since: Optional[datetime] = None) -> list[dict]:
        entries = []
        try:
            with open(self._log_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    if since:
                        ts = datetime.fromisoformat(entry["ts"])
                        if ts < since:
                            continue
                    entries.append(entry)
        except FileNotFoundError:
            pass
        return entries

    def daily_report(self, date_str: Optional[str] = None) -> str:
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        since = datetime.fromisoformat(f"{date_str}T00:00:00+00:00")
        until = since + timedelta(days=1)

        entries = [e for e in self._load_entries(since)
                   if datetime.fromisoformat(e["ts"]) < until]

        if not entries:
            return f"[{date_str}] 호출 없음"

        total_cost = sum(e["cost_usd"] for e in entries)
        total_calls = len(entries)
        by_provider = {}
        for e in entries:
            p = e["provider"]
            by_provider.setdefault(p, {"calls": 0, "cost": 0.0})
            by_provider[p]["calls"] += 1
            by_provider[p]["cost"] += e["cost_usd"]

        lines = [f"📊 LLM 비용 리포트 [{date_str}]",
                 f"총 호출: {total_calls}회 | 총 비용: ${total_cost:.4f}"]
        for p, v in sorted(by_provider.items()):
            lines.append(f"  {p}: {v['calls']}회 ${v['cost']:.4f}")
        return "\n".join(lines)

    def weekly_report(self) -> str:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        entries = self._load_entries(since)
        total = sum(e["cost_usd"] for e in entries)
        return f"📊 주간 LLM 비용: ${total:.4f} ({len(entries)}회 호출)"

    @property
    def session_cost(self) -> float:
        return self._session_cost

    @property
    def session_calls(self) -> int:
        return self._session_calls
