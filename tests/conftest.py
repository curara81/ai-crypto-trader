"""pytest 공통 fixtures."""
import os
import sys
from pathlib import Path

# scripts 디렉토리를 path에 추가
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 테스트용 TRADING_ROOT
os.environ.setdefault("TRADING_ROOT", "/tmp/trading_test")
os.makedirs("/tmp/trading_test/freqtrade_userdata/models", exist_ok=True)
os.makedirs("/tmp/trading_test/freqtrade_userdata/logs", exist_ok=True)
