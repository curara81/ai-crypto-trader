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


class LossStreakGuard:
    """연속 손실 보호 (v3.7) — N연패 후 일정 시간 거래 차단.

    심리적 손실 회피와 통계적 평균회귀 모두 활용:
    - 봇이 연속으로 지면 시장 상황이 전략과 안 맞는 상태일 가능성 ↑
    - 짧은 휴식 후 시장 변화 보고 재진입

    State는 JSON 파일에 {pause_until_ts: float, recent_trades: [bool,...]} 저장.
    """

    def __init__(
        self,
        max_consecutive_losses: int = 3,
        pause_minutes: int = 60,
        state_path: Optional[str] = None,
    ):
        self.max_losses = max_consecutive_losses
        self.pause_minutes = pause_minutes
        self.state_path = state_path or os.path.join(DEFAULT_STATE_DIR, "loss_streak.json")
        self._state = self._load()

    def _load(self) -> dict:
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"pause_until_ts": 0, "recent_outcomes": []}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w") as f:
                json.dump(self._state, f)
        except OSError as e:
            logger.warning(f"LossStreakGuard 저장 실패: {e}")

    def record_outcome(self, profit_pct: float) -> None:
        """체결된 trade 결과를 기록. profit_pct < 0 이면 loss."""
        is_win = profit_pct > 0
        outcomes = self._state.get("recent_outcomes", [])
        outcomes.append(is_win)
        outcomes = outcomes[-10:]  # 최근 10건만 유지
        self._state["recent_outcomes"] = outcomes

        # 연속 손실 카운트
        consecutive_losses = 0
        for w in reversed(outcomes):
            if w:
                break
            consecutive_losses += 1

        if consecutive_losses >= self.max_losses:
            from datetime import datetime, timezone, timedelta
            pause_until = datetime.now(timezone.utc) + timedelta(minutes=self.pause_minutes)
            self._state["pause_until_ts"] = pause_until.timestamp()
            logger.warning(
                f"LossStreakGuard 발동: {consecutive_losses}연패 → "
                f"{self.pause_minutes}분 거래 차단 (until {pause_until.strftime('%H:%M')})"
            )
        self._save()

    def is_blocked(self) -> bool:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).timestamp()
        return self._state.get("pause_until_ts", 0) > now

    def remaining_minutes(self) -> int:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).timestamp()
        until = self._state.get("pause_until_ts", 0)
        if until <= now:
            return 0
        return int((until - now) / 60) + 1


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
