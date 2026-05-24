"""실전 가드레일 모듈 테스트."""
import json
import os
import tempfile
from datetime import datetime, timezone

import pytest


def test_killswitch_blocks_when_file_exists(tmp_path):
    """킬스위치 파일 존재 시 거래가 차단되어야 한다."""
    from guardrails import KillSwitch

    ks_file = tmp_path / "KILLSWITCH"
    ks = KillSwitch(path=str(ks_file))

    assert not ks.is_active()

    ks_file.write_text("manual stop")
    assert ks.is_active()
    assert "manual stop" in ks.reason()


def test_daily_loss_limit_blocks_after_threshold(tmp_path):
    """일일 누적 손실이 임계 도달 시 신규 진입 차단."""
    from guardrails import DailyLossGuard

    state_file = tmp_path / "daily_loss.json"
    guard = DailyLossGuard(max_loss_pct=2.0, state_path=str(state_file))

    today = datetime.now(timezone.utc).date().isoformat()

    # -0.5% 손실: OK
    guard.record_pnl(today, -0.5)
    assert not guard.is_blocked(today)

    # 누적 -2.5%: 차단
    guard.record_pnl(today, -2.0)
    assert guard.is_blocked(today)


def test_daily_loss_resets_next_day(tmp_path):
    """날짜가 바뀌면 손실 누적이 리셋된다."""
    from guardrails import DailyLossGuard

    state_file = tmp_path / "daily_loss.json"
    guard = DailyLossGuard(max_loss_pct=2.0, state_path=str(state_file))

    guard.record_pnl("2026-05-23", -3.0)
    assert guard.is_blocked("2026-05-23")
    # 다음날에는 다시 거래 가능
    assert not guard.is_blocked("2026-05-24")


def test_position_cap_rejects_overweight(tmp_path):
    """총 노출액 한도 초과 시 신규 진입 거부."""
    from guardrails import PositionCap

    cap = PositionCap(max_total_exposure_krw=300_000, max_per_pair_krw=100_000)

    # 빈 상태에서 5만원 진입: OK
    open_trades = []
    assert cap.allow_new_entry("BTC/KRW", 50_000, open_trades)

    # 이미 25만원 보유 + 5만원 추가 = 30만원: OK (한도 도달)
    open_trades = [
        {"pair": "ETH/KRW", "stake_amount": 100_000},
        {"pair": "XRP/KRW", "stake_amount": 100_000},
        {"pair": "SOL/KRW", "stake_amount": 50_000},
    ]
    assert cap.allow_new_entry("BTC/KRW", 50_000, open_trades)

    # 35만원 시도: 거부
    assert not cap.allow_new_entry("BTC/KRW", 100_000, open_trades)


def test_position_cap_blocks_same_pair_double_buy(tmp_path):
    """동일 종목 추가 매수 시 per-pair 한도로 차단."""
    from guardrails import PositionCap

    cap = PositionCap(max_total_exposure_krw=1_000_000, max_per_pair_krw=100_000)

    open_trades = [{"pair": "BTC/KRW", "stake_amount": 80_000}]
    # 80k + 50k = 130k > 100k per-pair limit
    assert not cap.allow_new_entry("BTC/KRW", 50_000, open_trades)
    # 80k + 20k = 100k = limit, OK
    assert cap.allow_new_entry("BTC/KRW", 20_000, open_trades)
