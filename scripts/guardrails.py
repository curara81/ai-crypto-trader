#!/usr/bin/env python3
"""실전 트레이딩 가드레일 (Safety Rails)

dry_run=False로 전환할 때 단순 토글만으로는 위험하므로,
- 킬스위치 파일
- 일일 누적 손실 한도
- 총 노출액 / per-pair 한도
세 가지 안전망을 제공한다.

사용:
    from guardrails import KillSwitch, DailyLossGuard, PositionCap

    ks = KillSwitch()
    dlg = DailyLossGuard(max_loss_pct=2.0)
    cap = PositionCap(max_total_exposure_krw=300_000, max_per_pair_krw=100_000)

    if ks.is_active() or dlg.is_blocked(today):
        return  # 진입 차단
    if not cap.allow_new_entry(pair, stake, open_trades):
        return
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

TRADING_ROOT = os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading"))
DEFAULT_KILLSWITCH = os.path.join(TRADING_ROOT, "KILLSWITCH")
DEFAULT_STATE_DIR = os.path.join(TRADING_ROOT, "freqtrade_userdata/state")


class KillSwitch:
    """파일 존재 여부로 모든 신규 거래를 차단.

    파일 생성/삭제만으로 즉시 비상 정지 가능:
        touch ~/trading/KILLSWITCH       # 정지
        rm ~/trading/KILLSWITCH          # 재개
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path or DEFAULT_KILLSWITCH

    def is_active(self) -> bool:
        return os.path.exists(self.path)

    def reason(self) -> str:
        if not self.is_active():
            return ""
        try:
            with open(self.path) as f:
                return f.read().strip() or "no reason"
        except OSError:
            return "unreadable"

    def activate(self, reason: str = "manual") -> None:
        """프로그래매틱하게 정지 (큰 손실 감지 시 자동 발동 등)."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            f.write(f"{reason}\nactivated_at={datetime.now(timezone.utc).isoformat()}\n")
        logger.error(f"KillSwitch ACTIVATED: {reason}")


class DailyLossGuard:
    """일일 누적 손실이 임계 도달 시 신규 진입 차단.

    State는 JSON 파일에 {date: cum_pnl_pct} 형태로 저장된다.
    날짜가 바뀌면 자동 리셋.
    """

    def __init__(self, max_loss_pct: float = 2.0, state_path: Optional[str] = None):
        self.max_loss_pct = abs(max_loss_pct)  # 항상 양수로 저장
        self.state_path = state_path or os.path.join(DEFAULT_STATE_DIR, "daily_loss.json")
        self._state: dict[str, float] = self._load()

    def _load(self) -> dict[str, float]:
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w") as f:
                json.dump(self._state, f)
        except OSError as e:
            logger.warning(f"DailyLossGuard state save failed: {e}")

    def record_pnl(self, date: str, pnl_pct: float) -> None:
        """체결된 trade의 손익률(%)을 누적 기록."""
        self._state[date] = self._state.get(date, 0.0) + pnl_pct
        self._save()

    def cumulative(self, date: str) -> float:
        return self._state.get(date, 0.0)

    def is_blocked(self, date: str) -> bool:
        return self.cumulative(date) <= -self.max_loss_pct


class PositionCap:
    """총 노출액 + per-pair 한도 검증.

    open_trades 인자는 Freqtrade API /trades 응답 형식:
        [{"pair": "BTC/KRW", "stake_amount": 50000}, ...]
    """

    def __init__(self, max_total_exposure_krw: float, max_per_pair_krw: float):
        self.max_total = max_total_exposure_krw
        self.max_per_pair = max_per_pair_krw

    def current_total(self, open_trades: list[dict]) -> float:
        return sum(t.get("stake_amount", 0) for t in open_trades)

    def current_per_pair(self, pair: str, open_trades: list[dict]) -> float:
        return sum(t.get("stake_amount", 0) for t in open_trades if t.get("pair") == pair)

    def allow_new_entry(self, pair: str, new_stake_krw: float, open_trades: list[dict]) -> bool:
        if self.current_total(open_trades) + new_stake_krw > self.max_total:
            logger.info(f"PositionCap: total exposure limit hit for {pair}")
            return False
        if self.current_per_pair(pair, open_trades) + new_stake_krw > self.max_per_pair:
            logger.info(f"PositionCap: per-pair limit hit for {pair}")
            return False
        return True
