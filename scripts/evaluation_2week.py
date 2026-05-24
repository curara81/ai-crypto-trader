#!/usr/bin/env python3
"""2주 누적 데이터 부트스트랩 재평가 — 실전 전환 가능 여부 판단.

기준 (모두 충족 시 실전 전환 검토 가능):
  1. Sharpe > 0
  2. 승률 > 45%
  3. 평균 손익 ≥ 0%
  4. 부트스트랩 2주 시뮬레이션에서 수익 확률 > 60%
  5. v3.3/v3.4 이후 trades 200건 이상

결과를 텔레그램으로 전송 + iCloud 저장.
"""
import base64
import json
import logging
import os
import random
import statistics
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

TRADING_ROOT = os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading"))
CONFIG_PATH = os.path.join(TRADING_ROOT, "freqtrade_userdata/config_upbit_dryrun.json")
REPORT_DIR = os.path.expanduser("~/Library/Mobile Documents/com~apple~CloudDocs/_trading_reports")

# v3.3 적용 시점 (이 시점 이후 trades만 평가)
V33_DEPLOY_TS = "2026-05-24T14:00:00"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("eval")


def fetch_trades() -> list[dict]:
    auth = base64.b64encode(b"freqtrade:freqtrade").decode()
    req = urllib.request.Request(
        "http://127.0.0.1:8080/api/v1/trades?limit=500",
        headers={"Authorization": f"Basic {auth}"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r).get("trades", [])


def send_telegram(msg: str) -> None:
    try:
        with open(CONFIG_PATH) as f:
            c = json.load(f)
        token = c["telegram"]["token"]
        chat_id = str(c["telegram"]["chat_id"])
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}).encode(),
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


def main() -> int:
    trades = fetch_trades()
    closed = [
        t for t in trades
        if not t["is_open"]
        and t.get("profit_ratio") is not None
        and t.get("close_date", "") >= V33_DEPLOY_TS
    ]

    if len(closed) < 50:
        msg = (
            f"⏳ <b>2주 재평가 보류</b>\n\n"
            f"v3.3 이후 trades: {len(closed)}건 (최소 50건 필요)\n"
            f"더 데이터 모인 후 재실행 권장.\n\n"
            f"수동 실행: <code>python3 scripts/evaluation_2week.py</code>"
        )
        logger.warning(msg)
        send_telegram(msg)
        return 0

    returns = [t["profit_ratio"] * 100 for t in closed]
    mean_r = statistics.mean(returns)
    median_r = statistics.median(returns)
    sd = statistics.stdev(returns) if len(returns) > 1 else 0
    wins = sum(1 for r in returns if r > 0)
    winrate = wins / len(returns) * 100
    sharpe = mean_r / sd if sd > 0 else 0

    # 부트스트랩 2주 시뮬레이션
    first_ts = min(t.get("open_timestamp", 0) for t in closed) / 1000
    last_ts = max(t.get("close_timestamp", 0) for t in closed) / 1000
    days = (last_ts - first_ts) / 86400
    tpd = len(closed) / max(days, 0.5)
    n_2w = int(tpd * 14)

    random.seed(42)
    sims = []
    for _ in range(10000):
        sample = [random.choice(returns) for _ in range(n_2w)]
        sims.append(sum(sample) / 3)  # max 3 concurrent positions
    sims.sort()
    p50 = sims[5000]
    prob_profit = sum(1 for s in sims if s > 0) / 100

    # 통과 조건
    conditions = {
        "Sharpe > 0": sharpe > 0,
        "승률 > 45%": winrate > 45,
        "평균 손익 ≥ 0%": mean_r >= 0,
        "수익 확률 > 60%": prob_profit > 60,
        "샘플 ≥ 200": len(closed) >= 200,
    }
    passed = sum(conditions.values())

    verdict = "✅ <b>실전 전환 가능</b>" if passed == 5 else \
              "⚠️ <b>실전 전환 보류</b>" if passed >= 3 else \
              "❌ <b>실전 전환 불가</b>"

    detail = "\n".join([
        f"  {'✅' if v else '❌'} {k}" for k, v in conditions.items()
    ])

    msg = (
        f"📊 <b>2주 누적 재평가 ({len(closed)} trades)</b>\n\n"
        f"{verdict} ({passed}/5)\n\n"
        f"<b>통계</b>\n"
        f"  평균: {mean_r:+.3f}% / 중간: {median_r:+.3f}%\n"
        f"  승률: {winrate:.1f}% / Sharpe: {sharpe:.3f}\n"
        f"  일평균 trades: {tpd:.1f}\n\n"
        f"<b>2주 예측 (중앙값)</b>\n"
        f"  {p50:+.2f}% / 수익 확률 {prob_profit:.1f}%\n\n"
        f"<b>조건 충족</b>\n{detail}\n\n"
        f"<b>다음 단계</b>\n"
    )

    if passed == 5:
        msg += (
            "  → 5만원으로 소액 실전 시작 가능\n"
            "  → docs/실전전환_가이드.md 참조"
        )
    elif passed >= 3:
        msg += (
            "  → 1주 더 대기, 필터 조정 검토\n"
            "  → 부족한 조건 분석 후 v3.6 검토"
        )
    else:
        msg += (
            "  → 전략 자체 재설계 필요\n"
            "  → Gemini 의존도 축소, 룰 기반 전환 검토"
        )

    logger.info(msg)
    send_telegram(msg)

    # iCloud 저장
    Path(REPORT_DIR).mkdir(parents=True, exist_ok=True)
    out_file = Path(REPORT_DIR) / f"eval_2week_{datetime.now().strftime('%Y%m%d')}.md"
    out_file.write_text(msg.replace("<b>", "**").replace("</b>", "**").replace("<code>", "`").replace("</code>", "`"))
    logger.info(f"리포트 저장: {out_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
