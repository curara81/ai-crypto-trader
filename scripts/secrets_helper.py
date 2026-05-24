#!/usr/bin/env python3
"""macOS Keychain 시크릿 헬퍼.

API 키를 평문 config 파일이나 plist 에 저장하지 않고
macOS Keychain 의 generic-password 항목으로 안전 보관한다.

사용법:
    # 키 등록 (1회)
    python3 secrets_helper.py set GEMINI_API_KEY
    # → 프롬프트에서 키 입력

    # 키 조회
    python3 secrets_helper.py get GEMINI_API_KEY

    # 코드에서 사용
    from secrets_helper import get_secret
    api_key = get_secret("GEMINI_API_KEY")
    # → 1) Keychain 조회
    #   2) 없으면 환경변수 폴백
    #   3) 둘 다 없으면 None

Linux/CI 환경에서는 자동으로 환경변수만 사용한다.
"""
import getpass
import os
import platform
import subprocess
import sys
from typing import Optional

SERVICE_NAME = "ai-crypto-trader"


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _keychain_get(key: str) -> Optional[str]:
    if not _is_macos():
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE_NAME, "-a", key, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _keychain_set(key: str, value: str) -> bool:
    if not _is_macos():
        return False
    # 기존 항목 있으면 삭제 (덮어쓰기)
    subprocess.run(
        ["security", "delete-generic-password", "-s", SERVICE_NAME, "-a", key],
        capture_output=True,
    )
    result = subprocess.run(
        ["security", "add-generic-password", "-s", SERVICE_NAME, "-a", key, "-w", value],
        capture_output=True,
    )
    return result.returncode == 0


def get_secret(key: str) -> Optional[str]:
    """1) Keychain → 2) env var 순으로 조회."""
    val = _keychain_get(key)
    if val:
        return val
    return os.environ.get(key)


def set_secret(key: str, value: str) -> bool:
    """Keychain에 저장 (macOS만)."""
    return _keychain_set(key, value)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1

    cmd, key = sys.argv[1], sys.argv[2]

    if cmd == "get":
        val = get_secret(key)
        if val:
            print(val)
            return 0
        print(f"키 '{key}' 없음 (Keychain + env 모두)", file=sys.stderr)
        return 2

    elif cmd == "set":
        if not _is_macos():
            print("Keychain은 macOS만 지원합니다", file=sys.stderr)
            return 3
        value = getpass.getpass(f"{key} 값 입력 (보이지 않음): ")
        if set_secret(key, value):
            print(f"✓ Keychain에 저장: {key}")
            return 0
        print(f"저장 실패", file=sys.stderr)
        return 4

    elif cmd == "list":
        # ai-crypto-trader 서비스의 모든 항목 조회는 security CLI로 직접 불가
        # 대신 잘 알려진 키들을 시도
        print(f"등록된 시크릿 (서비스 = {SERVICE_NAME}):")
        known_keys = ["GEMINI_API_KEY", "TAVILY_API_KEY", "ANTHROPIC_API_KEY",
                      "KIS_APP_KEY", "KIS_APP_SECRET", "UPBIT_API_KEY", "UPBIT_SECRET"]
        for k in known_keys:
            if _keychain_get(k):
                print(f"  ✓ {k}")
        return 0

    else:
        print(f"알 수 없는 명령: {cmd}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
