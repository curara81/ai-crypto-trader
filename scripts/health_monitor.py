#!/usr/bin/env python3
"""봇 헬스 모니터 — 30분 주기로 launchd에서 실행.

체크 항목:
1. 봇 프로세스 살아있는지 (launchctl)
2. KILLSWITCH 활성 여부
3. 최근 30분 로그에서 가드레일 발동 / Gemini 에러
4. 일일 누적 손익 임계 근접 여부 (-1.5% 도달 시 경고)
5. ML 모델 파일 존재 여부

이상 발견 시 텔레그램 알림. 정상이면 침묵 (스팸 방지).
"""
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

TRADING_ROOT = os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading"))
USERDATA_DIR = Path(TRADING_ROOT) / "freqtrade_userdata"
LOG_DIR = USERDATA_DIR / "logs"
STATE_DIR = USERDATA_DIR / "state"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("health_monitor")

CONFIG_PATH = os.environ.get(
    "FREQTRADE_CONFIG",
    str(USERDATA_DIR / "config_upbit_dryrun.json"),
)


def _load_telegram() -> tuple[str, str]:
    try:
        with open(CONFIG_PATH) as f:
            c = json.load(f)
        return c["telegram"]["token"], str(c["telegram"]["chat_id"])
    except Exception:
        return "", ""


def send_telegram(msg: str) -> None:
    token, chat_id = _load_telegram()
    if not token or not chat_id:
        logger.warning("Telegram 설정 없음 — 알림 스킵")
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")


def check_process_alive() -> list[str]:
    """launchctl list로 freqtrade-dryrun 살아있는지 확인."""
    issues = []
    try:
        result = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=10
        )
        if "com.curara.freqtrade-dryrun" not in result.stdout:
            issues.append("⛔ freqtrade-dryrun 서비스 미등록")
            return issues
        # PID 확인
        for line in result.stdout.split("\n"):
            if "com.curara.freqtrade-dryrun" in line:
                parts = line.split()
                if parts[0] == "-":
                    issues.append("⛔ freqtrade-dryrun PID 없음 (재시작 대기 중)")
    except Exception as e:
        issues.append(f"⚠️ launchctl 확인 실패: {e}")
    return issues


def check_killswitch() -> list[str]:
    ks = Path(TRADING_ROOT) / "KILLSWITCH"
    if ks.exists():
        try:
            reason = ks.read_text().strip()[:100]
            return [f"🛑 KILLSWITCH 활성: {reason}"]
        except Exception:
            return ["🛑 KILLSWITCH 활성 (사유 읽기 실패)"]
    return []


def check_recent_log_errors(window_minutes: int = 30) -> list[str]:
    """최근 N분 launchd_stderr.log에서 에러/가드레일 감지."""
    issues = []
    log_file = LOG_DIR / "launchd_stderr.log"
    if not log_file.exists():
        return ["⚠️ launchd_stderr.log 없음"]

    cutoff = datetime.now() - timedelta(minutes=window_minutes)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M")

    gemini_errors = 0
    guardrail_blocks = 0
    network_errors = 0

    try:
        # 큰 로그라 tail로 마지막 5000줄만
        result = subprocess.run(
            ["tail", "-n", "5000", str(log_file)],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.split("\n"):
            # 타임스탬프 추출
            m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", line)
            if not m or m.group(1) < cutoff_str:
                continue

            if "Gemini" in line and ("error" in line.lower() or "ERROR" in line):
                gemini_errors += 1
            if "Guardrails blocked" in line or "KillSwitch active" in line:
                guardrail_blocks += 1
            if "ConnectionError" in line or "Timeout" in line:
                network_errors += 1
    except Exception as e:
        return [f"⚠️ 로그 읽기 실패: {e}"]

    if gemini_errors >= 5:
        issues.append(f"⚠️ Gemini 에러 {gemini_errors}건 (지난 {window_minutes}분)")
    if guardrail_blocks >= 3:
        issues.append(f"🚨 가드레일 차단 {guardrail_blocks}건 (지난 {window_minutes}분)")
    if network_errors >= 10:
        issues.append(f"⚠️ 네트워크 에러 {network_errors}건 (지난 {window_minutes}분)")

    return issues


def check_daily_loss() -> list[str]:
    """오늘 누적 손실이 임계 근접인지 확인."""
    state_file = STATE_DIR / "daily_loss.json"
    if not state_file.exists():
        return []
    try:
        with open(state_file) as f:
            state = json.load(f)
        today = datetime.now(timezone.utc).date().isoformat()
        cum = state.get(today, 0.0)
        max_loss = float(os.environ.get("DAILY_MAX_LOSS_PCT", "2.0"))
        # -1.5% 도달 시 (임계 75%) 경고
        if cum <= -max_loss * 0.75:
            return [f"⚠️ 일일 누적 손실 {cum:+.2f}% (임계 -{max_loss}% 의 75% 도달)"]
    except Exception:
        pass
    return []


def check_ml_models() -> list[str]:
    """ML 모델 파일이 존재하고 최근에 갱신되었는지."""
    issues = []
    models_dir = USERDATA_DIR / "models"
    required = ["xgb_signal.json", "lstm_predictor.pt", "dqn_trader.pt"]
    for f in required:
        path = models_dir / f
        if not path.exists():
            issues.append(f"⚠️ ML 모델 없음: {f}")
            continue
        # 14일 이상 안 갱신되면 경고 (주간 재학습이 안 돌고 있음)
        age_days = (datetime.now().timestamp() - path.stat().st_mtime) / 86400
        if age_days > 14:
            issues.append(f"⚠️ ML 모델 오래됨: {f} ({age_days:.0f}일 전)")
    return issues


# ─── v7.3: 공유용 PWA(8089) FD 누수 워치독 ──────────────
# 장시간 가동 시 CLOSE_WAIT 소켓/파이프가 누적돼 maxfiles 한도에 부딪히면
# yfinance·Vertex AI·DNS가 줄줄이 실패한다(2026-06 장애 원인). 30분 주기
# 헬스체크에서 FD를 감시하고, 누수 신호가 보이거나 36시간 이상 가동됐으면
# pwa_public을 자동 재시작(launchctl kickstart)해 누수를 리셋한다.
PWA_PUBLIC_LABEL = "com.curara.freqtrade-pwa-public"
PWA_GUI_DOMAIN = "gui/501"
PWA_CLOSE_WAIT_LIMIT = 120     # CLOSE_WAIT 소켓 누수 임계
PWA_NET_FD_LIMIT = 1500        # 소켓+파이프 총합 임계 (maxfiles 8192 대비 여유)
PWA_MAX_UPTIME_SEC = 36 * 3600  # 36시간마다 예방적 재시작


def _service_pid(label: str):
    try:
        result = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            if label in line:
                pid = line.split()[0]
                return int(pid) if pid.isdigit() else None
    except Exception:
        return None
    return None


def _pwa_fd_stats(pid: int) -> tuple[int, int]:
    """(CLOSE_WAIT 수, 소켓+파이프 수). 메모리 맵 라이브러리는 제외."""
    try:
        result = subprocess.run(
            ["lsof", "-p", str(pid)], capture_output=True, text=True, timeout=25
        )
        lines = result.stdout.splitlines()[1:]
        close_wait = sum(1 for ln in lines if "CLOSE_WAIT" in ln)
        net_fds = sum(
            1 for ln in lines
            if ("TCP" in ln or "PIPE" in ln or "IPv4" in ln or "IPv6" in ln)
        )
        return close_wait, net_fds
    except Exception:
        return -1, -1


def _service_uptime_sec(pid: int) -> int:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "etimes="],
            capture_output=True, text=True, timeout=10,
        )
        return int(result.stdout.strip())
    except Exception:
        return -1


def _kickstart_pwa() -> bool:
    try:
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"{PWA_GUI_DOMAIN}/{PWA_PUBLIC_LABEL}"],
            capture_output=True, text=True, timeout=20,
        )
        return True
    except Exception as e:
        logger.error(f"pwa_public kickstart 실패: {e}")
        return False


def check_pwa_watchdog() -> list[str]:
    """공유용 PWA의 FD 누수 감시 + 자동 재시작."""
    issues = []
    pid = _service_pid(PWA_PUBLIC_LABEL)
    if not pid:
        issues.append("⚠️ pwa_public(8089) 미가동")
        return issues
    close_wait, net_fds = _pwa_fd_stats(pid)
    uptime = _service_uptime_sec(pid)
    reasons = []
    if close_wait >= PWA_CLOSE_WAIT_LIMIT:
        reasons.append(f"CLOSE_WAIT {close_wait}")
    if net_fds >= PWA_NET_FD_LIMIT:
        reasons.append(f"소켓/파이프 {net_fds}")
    if 0 < PWA_MAX_UPTIME_SEC <= uptime:
        reasons.append(f"가동 {uptime // 3600}시간")
    if reasons:
        if _kickstart_pwa():
            note = "♻️ PWA(8089) 자동 재시작 — " + ", ".join(reasons)
            logger.info(note)
            send_telegram(note)
        else:
            issues.append("⚠️ pwa_public 자동 재시작 실패")
    return issues


def main() -> int:
    issues = []
    issues += check_process_alive()
    issues += check_killswitch()
    issues += check_recent_log_errors(window_minutes=30)
    issues += check_daily_loss()
    issues += check_ml_models()
    issues += check_pwa_watchdog()

    if not issues:
        logger.info("✓ All healthy")
        return 0

    msg = f"<b>🩺 봇 헬스체크 알림</b>\n\n" + "\n".join(issues)
    msg += f"\n\n_체크 시각: {datetime.now().strftime('%H:%M')}_"
    logger.warning(f"이슈 {len(issues)}건 감지 — 텔레그램 알림 전송")
    send_telegram(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
