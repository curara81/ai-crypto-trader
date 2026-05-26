#!/usr/bin/env python3
"""
미국 주식 AI 트레이딩 봇 (KIS 모의투자)

한국투자증권 API(python-kis)를 통해 미국 주식 모의투자를 수행하는 봇.
Gemini 2.5 Flash AI가 매수/매도/관망을 판단하고,
Tavily API로 뉴스 수집, JSONL로 판단 이력 기록,
텔레그램으로 한국어 알림을 전송한다.

동작 시간: 미국 장시간 (23:30~06:00 KST = 9:30~16:00 EST)
분석 주기: 5분마다 (장시간 중)
프리마켓 분석: 23:00 KST

Required env vars:
    - KIS_APP_KEY, KIS_APP_SECRET
    - GEMINI_API_KEY
    - TAVILY_API_KEY
"""

import json
import logging
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta, date
from typing import Optional

import requests

# ML Signal Engine 임포트
try:
    from ml_signal_engine import MLSignalEngine
    _ML_ENGINE = MLSignalEngine()
    logging.getLogger("kis_stock_bot").info("MLSignalEngine 로드 성공")
except Exception as _ml_err:
    _ML_ENGINE = None
    logging.getLogger("kis_stock_bot").warning(f"MLSignalEngine 로드 실패: {_ml_err}")

# v7.0: RAG 파이프라인
try:
    from news_rag import NewsRAG
    _NEWS_RAG = NewsRAG()
    logging.getLogger("kis_stock_bot").info(f"NewsRAG 로드 {'성공' if _NEWS_RAG.is_ready else '실패(비활성)'}")
except Exception as _rag_err:
    _NEWS_RAG = None
    logging.getLogger("kis_stock_bot").warning(f"NewsRAG 로드 실패: {_rag_err}")

# v7.0: 멀티에이전트 (경량 QuickCrew)
try:
    from trading_crew import QuickCrew
    _QUICK_CREW = QuickCrew()
    logging.getLogger("kis_stock_bot").info(f"QuickCrew 로드 {'성공' if _QUICK_CREW.is_ready else '실패'}")
except Exception as _crew_err:
    _QUICK_CREW = None
    logging.getLogger("kis_stock_bot").warning(f"QuickCrew 로드 실패: {_crew_err}")

# v7.0: 비용 추적
try:
    from cost_tracker import CostTracker
    _COST_TRACKER = CostTracker()
except Exception:
    _COST_TRACKER = None

# ─── 로깅 설정 ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("kis_stock_bot")

# ─── 상수 ──────────────────────────────────────────────────────
STOCK_LIST = [
    # 빅테크
    "AAPL", "MSFT", "NVDA", "TSLA", "GOOGL", "AMZN", "META", "AMD",
    # 저평가 가치주
    "LULU", "CALM", "HRMY",
]

STOCK_NAMES = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "NVIDIA",
    "TSLA": "Tesla", "GOOGL": "Alphabet/Google", "AMZN": "Amazon",
    "META": "Meta Platforms", "AMD": "AMD",
    "LULU": "Lululemon", "CALM": "Cal-Maine Foods", "HRMY": "Harmony Bio",
}

# 미국 장 시간 (KST 기준, 서머타임 적용)
# EST: 9:30~16:00 = KST: 23:30~06:00 (다음날)
# EDT (서머타임): 9:30~16:00 = KST: 22:30~05:00 (다음날)
# 여기서는 KIS API의 trading_hours를 활용

DECISION_CYCLE_SECONDS = 300  # 5분
PREMARKET_HOUR_KST = 23  # 프리마켓 분석 시작 (KST)

BASE_TRADE_AMOUNT_USD = 50.0  # 기본 매매 금액 (USD)
CONFIDENCE_THRESHOLD = 0.65   # 매매 실행 최소 확신도

TRADING_ROOT = os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading"))
USERDATA_DIR = os.path.join(TRADING_ROOT, "freqtrade_userdata")
DECISION_LOG = os.path.join(USERDATA_DIR, "logs/stock_decisions.jsonl")
CONFIG_FILE = os.environ.get("FREQTRADE_CONFIG", os.path.join(USERDATA_DIR, "config_upbit_dryrun.json"))

# Keychain 시크릿 우선 → env 폴백
try:
    from secrets_helper import get_secret as _get_secret
except ImportError:
    def _get_secret(key):
        return os.environ.get(key)

# Gemini 2.5 Flash 가격 (USD per 1M tokens)
GEMINI_PRICE_INPUT = 0.15
GEMINI_PRICE_THINKING = 3.50
GEMINI_PRICE_OUTPUT = 0.60

# ─── Graceful shutdown ─────────────────────────────────────────
_shutdown = False


def _signal_handler(signum, frame):
    global _shutdown
    logger.info("종료 시그널 수신, 안전하게 종료합니다...")
    _shutdown = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ─── 텔레그램 ──────────────────────────────────────────────────
class TelegramNotifier:
    """텔레그램 알림 전송 클래스"""

    def __init__(self, config_path: str = CONFIG_FILE):
        with open(config_path) as f:
            config = json.load(f)
        self.token = config["telegram"]["token"]
        self.chat_id = str(config["telegram"]["chat_id"])
        self.api = f"https://api.telegram.org/bot{self.token}"

    def send(self, msg: str):
        """HTML 파싱 모드로 텔레그램 메시지 전송"""
        try:
            requests.post(
                f"{self.api}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": msg,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"텔레그램 전송 실패: {e}")


# ─── 뉴스 수집 (Tavily API + RAG) ─────────────────────────────
def fetch_news(symbol: str, stock_name: str, max_results: int = 5) -> tuple[str, str]:
    """Tavily API로 뉴스 수집 + RAG 벡터DB 저장/검색.

    v7.0: RAG 통합 — 뉴스를 벡터DB에 저장하고 관련 컨텍스트 반환.
    Returns: (plain_news, rag_context)
    """
    if _NEWS_RAG and _NEWS_RAG.is_ready:
        return _NEWS_RAG.fetch_and_ingest(symbol, stock_name, max_results)

    # RAG 미사용 시 기존 방식
    tavily_key = _get_secret("TAVILY_API_KEY")
    if not tavily_key:
        return "No news available (TAVILY_API_KEY not set).", ""

    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": tavily_key,
                "query": f"{stock_name} {symbol} stock price analysis today",
                "max_results": max_results,
                "search_depth": "basic",
            },
            timeout=15,
        )
        articles = resp.json().get("results", [])
        if not articles:
            return "No recent news found.", ""

        return "\n".join(f"- {a.get('title', '')}" for a in articles[:max_results]), ""
    except Exception as e:
        logger.warning(f"뉴스 수집 실패 ({symbol}): {e}")
        return "News fetch failed.", ""


# ─── 판단 이력 관리 (JSONL) ────────────────────────────────────
def load_recent_decisions(symbol: str, limit: int = 10) -> str:
    """JSONL 로그에서 해당 종목의 최근 판단 이력 로드"""
    try:
        recent = []
        with open(DECISION_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("symbol") == symbol:
                    recent.append(entry)
        recent = recent[-limit:]
        if not recent:
            return "No previous decisions for this stock."

        lines = []
        for r in recent:
            ts = r.get("timestamp", "?")[:16]
            act = r.get("action", "?")
            conf = r.get("confidence", 0)
            reason = r.get("reason", "")
            price = r.get("price", 0)
            outcome = r.get("outcome", "")
            outcome_str = f" -> {outcome}" if outcome else ""
            lines.append(
                f"  [{ts}] {act}(conf={conf:.2f}) @${price:.2f}{outcome_str} - {reason}"
            )
        return "\n".join(lines)
    except FileNotFoundError:
        return "No history available (first run)."
    except Exception:
        return "History load failed."


def log_decision(
    symbol: str,
    decision: dict,
    price: float,
    input_tokens: int = 0,
    thinking_tokens: int = 0,
    output_tokens: int = 0,
):
    """판단 결과를 JSONL 파일에 기록"""
    try:
        os.makedirs(os.path.dirname(DECISION_LOG), exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "action": decision.get("action", "hold"),
            "confidence": decision.get("confidence", 0),
            "reason": decision.get("reason", ""),
            "price": price,
            "stake_multiplier": decision.get("stake_multiplier", 1.0),
            "risk_level": decision.get("risk_level", "medium"),
            "price_target": decision.get("price_target", ""),
            "stop_loss": decision.get("stop_loss", ""),
            "input_tokens": input_tokens,
            "thinking_tokens": thinking_tokens,
            "output_tokens": output_tokens,
        }
        with open(DECISION_LOG, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"판단 기록 실패: {e}")


# ─── KIS REST API 직접 호출 래퍼 ─────────────────────────────
# 거래소 코드 맵
EXCHANGE_MAP = {
    "AAPL": "NAS", "MSFT": "NAS", "NVDA": "NAS", "TSLA": "NAS",
    "GOOGL": "NAS", "AMZN": "NAS", "META": "NAS", "AMD": "NAS",
    "LULU": "NAS", "CALM": "NAS", "HRMY": "NAS",
}


class KISClient:
    """한국투자증권 REST API 클라이언트 (모의투자 직접 호출)"""

    BASE_URL = "https://openapivts.koreainvestment.com:29443"

    def __init__(self):
        self.app_key = _get_secret("KIS_APP_KEY")
        self.app_secret = _get_secret("KIS_APP_SECRET")
        self.account_no = os.environ.get("KIS_ACCOUNT_NO", "50189546")
        self.account_suffix = "01"

        if not self.app_key or not self.app_secret:
            raise ValueError("KIS_APP_KEY, KIS_APP_SECRET 환경변수가 필요합니다.")

        self._token = ""
        self._token_expires = 0
        self._refresh_token()
        logger.info(f"KIS 모의투자 연결 완료 (계좌: {self.account_no})")

    def _refresh_token(self):
        """접근 토큰 발급/갱신"""
        try:
            resp = requests.post(
                f"{self.BASE_URL}/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.app_key,
                    "appsecret": self.app_secret,
                },
                timeout=10,
            )
            data = resp.json()
            self._token = data.get("access_token", "")
            # 토큰 유효시간 (보통 24시간, 여유 두고 23시간)
            self._token_expires = time.time() + 82800
            if self._token:
                logger.info("KIS 접근 토큰 발급 완료")
            else:
                logger.error(f"토큰 발급 실패: {data}")
        except Exception as e:
            logger.error(f"토큰 발급 에러: {e}")

    def _headers(self, tr_id: str) -> dict:
        """API 요청 헤더 생성"""
        if time.time() > self._token_expires:
            self._refresh_token()
        return {
            "authorization": f"Bearer {self._token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "content-type": "application/json; charset=utf-8",
        }

    def _get(self, path: str, tr_id: str, params: dict) -> dict:
        """GET 요청 헬퍼"""
        try:
            resp = requests.get(
                f"{self.BASE_URL}{path}",
                headers=self._headers(tr_id),
                params=params,
                timeout=15,
            )
            return resp.json()
        except Exception as e:
            logger.warning(f"KIS GET 실패 ({path}): {e}")
            return {}

    def _post(self, path: str, tr_id: str, body: dict) -> dict:
        """POST 요청 헬퍼"""
        try:
            resp = requests.post(
                f"{self.BASE_URL}{path}",
                headers=self._headers(tr_id),
                json=body,
                timeout=15,
            )
            return resp.json()
        except Exception as e:
            logger.warning(f"KIS POST 실패 ({path}): {e}")
            return {}

    def get_quote(self, symbol: str) -> Optional[dict]:
        """해외주식 현재가 조회"""
        excd = EXCHANGE_MAP.get(symbol, "NAS")
        data = self._get(
            "/uapi/overseas-price/v1/quotations/price",
            "HHDFS00000300",
            {"AUTH": "", "EXCD": excd, "SYMB": symbol},
        )
        if data.get("rt_cd") != "0" or not data.get("output"):
            logger.warning(f"시세 조회 실패 ({symbol}): {data.get('msg1', '')}")
            return None

        o = data["output"]
        price = float(o.get("last", 0) or 0)
        return {
            "price": price,
            "open": float(o.get("open", 0) or 0),
            "high": float(o.get("high", 0) or 0),
            "low": float(o.get("low", 0) or 0),
            "close": price,
            "volume": int(o.get("tvol", 0) or 0),
            "change": float(o.get("rate", 0) or 0),
            "prev_price": float(o.get("base", 0) or 0),
            "name": STOCK_NAMES.get(symbol, symbol),
        }

    def get_daily_chart(self, symbol: str, days: int = 60) -> list:
        """해외주식 일봉 차트 데이터 조회"""
        excd = EXCHANGE_MAP.get(symbol, "NAS")
        end_date = date.today().strftime("%Y%m%d")
        start_date = (date.today() - timedelta(days=days)).strftime("%Y%m%d")

        data = self._get(
            "/uapi/overseas-price/v1/quotations/dailyprice",
            "HHDFS76240000",
            {
                "AUTH": "",
                "EXCD": excd,
                "SYMB": symbol,
                "GUBN": "0",  # 0=일, 1=주, 2=월
                "BYMD": end_date,
                "MODP": "1",  # 수정주가 반영
            },
        )
        if data.get("rt_cd") != "0":
            logger.warning(f"차트 조회 실패 ({symbol}): {data.get('msg1', '')}")
            return []

        bars = []
        for item in data.get("output2", []):
            if not item.get("clos"):
                continue
            bars.append({
                "date": item.get("xymd", ""),
                "open": float(item.get("open", 0) or 0),
                "high": float(item.get("high", 0) or 0),
                "low": float(item.get("low", 0) or 0),
                "close": float(item.get("clos", 0) or 0),
                "volume": int(item.get("tvol", 0) or 0),
            })
        # 오래된 순서로 정렬
        bars.sort(key=lambda x: x["date"])
        return bars

    def get_balance(self) -> Optional[dict]:
        """해외주식 계좌 잔고 조회"""
        data = self._get(
            "/uapi/overseas-stock/v1/trading/inquire-balance",
            "VTTS3012R",
            {
                "CANO": self.account_no,
                "ACNT_PRDT_CD": self.account_suffix,
                "OVRS_EXCG_CD": "NASD",
                "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )
        if data.get("rt_cd") != "0":
            logger.warning(f"잔고 조회 실패: {data.get('msg1', '')}")
            return None

        stocks = []
        for item in data.get("output1", []):
            qty = int(item.get("ovrs_cblc_qty", 0) or 0)
            if qty <= 0:
                continue
            stocks.append({
                "symbol": item.get("ovrs_pdno", ""),
                "name": item.get("ovrs_item_name", ""),
                "qty": qty,
                "avg_price": float(item.get("pchs_avg_pric", 0) or 0),
                "current_price": float(item.get("now_pric2", 0) or 0),
                "profit_rate": float(item.get("evlu_pfls_rt", 0) or 0),
                "profit": float(item.get("frcr_evlu_pfls_amt", 0) or 0),
            })

        # output2에서 총 자산 정보
        summary = data.get("output2", {})
        if isinstance(summary, list) and summary:
            summary = summary[0]

        return {
            "total": float(summary.get("tot_evlu_pfls_amt", 0) or 0),
            "deposit": float(summary.get("frcr_dncl_amt_2", 0) or 0),
            "stocks": stocks,
        }

    def get_positions(self) -> list:
        """현재 보유 포지션 목록"""
        balance = self.get_balance()
        if not balance:
            return []
        return [s for s in balance.get("stocks", []) if s["qty"] > 0]

    def buy_stock(self, symbol: str, qty: int, price: float = None) -> Optional[dict]:
        """해외주식 매수 주문"""
        excd = EXCHANGE_MAP.get(symbol, "NAS")
        # 거래소 코드 → 주문용 코드
        excg_map = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}
        excg_cd = excg_map.get(excd, "NASD")

        body = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_suffix,
            "OVRS_EXCG_CD": excg_cd,
            "PDNO": symbol,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": str(price) if price else "0",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00" if price else "00",  # 지정가
        }

        data = self._post(
            "/uapi/overseas-stock/v1/trading/order",
            "VTTT1002U",  # 모의투자 해외주식 매수
            body,
        )
        if data.get("rt_cd") != "0":
            logger.error(f"매수 실패 ({symbol}): {data.get('msg1', '')}")
            return None

        output = data.get("output", {})
        result = {
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "order_number": output.get("ORNO", "N/A"),
            "time": datetime.now().isoformat(),
        }
        logger.info(f"매수 주문 완료: {symbol} {qty}주 @ ${price or 'MKT'}")
        return result

    def sell_stock(self, symbol: str, qty: int, price: float = None) -> Optional[dict]:
        """해외주식 매도 주문"""
        excd = EXCHANGE_MAP.get(symbol, "NAS")
        excg_map = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}
        excg_cd = excg_map.get(excd, "NASD")

        body = {
            "CANO": self.account_no,
            "ACNT_PRDT_CD": self.account_suffix,
            "OVRS_EXCG_CD": excg_cd,
            "PDNO": symbol,
            "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": str(price) if price else "0",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN": "00" if price else "00",
        }

        data = self._post(
            "/uapi/overseas-stock/v1/trading/order",
            "VTTT1001U",  # 모의투자 해외주식 매도
            body,
        )
        if data.get("rt_cd") != "0":
            logger.error(f"매도 실패 ({symbol}): {data.get('msg1', '')}")
            return None

        output = data.get("output", {})
        result = {
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "order_number": output.get("ORNO", "N/A"),
            "time": datetime.now().isoformat(),
        }
        logger.info(f"매도 주문 완료: {symbol} {qty}주 @ ${price or 'MKT'}")
        return result

    def is_market_open(self) -> bool:
        """미국 시장 개장 여부 (KST 기준 수동 판단)"""
        now = datetime.now()
        hour = now.hour
        minute = now.minute
        weekday = now.weekday()  # 0=월 ~ 6=일

        # 주말 제외 (토=5, 일=6)
        # 금요일 밤~토요일 아침은 장 열림 (금 23:30 ~ 토 06:00)
        if weekday == 5 and hour >= 6:
            return False  # 토요일 오전 6시 이후 닫힘
        if weekday == 6:
            return False  # 일요일 종일 닫힘

        # KST 22:30 ~ 06:00 (서머타임 기간)
        # KST 23:30 ~ 07:00 (비서머타임 기간)
        # 넉넉하게 22:30 ~ 06:00으로 설정
        if hour >= 23 or hour < 6:
            return True
        if hour == 22 and minute >= 30:
            return True
        return False


# ─── Gemini AI 판단 엔진 ───────────────────────────────────────
class GeminiDecisionEngine:
    """Gemini 2.5 Flash를 활용한 AI 매매 판단 엔진"""

    def __init__(self):
        self.api_key = _get_secret("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY 환경변수가 필요합니다.")
        self._cache: dict = {}  # symbol -> {"decision": ..., "ts": ...}
        self._cache_ttl = 280  # 약 5분 (사이클보다 약간 짧게)

    def _compute_technicals(self, chart_data: list) -> dict:
        """차트 데이터에서 기술지표를 계산 (numpy/talib 없이 순수 파이썬)"""
        if len(chart_data) < 20:
            return {}

        closes = [bar["close"] for bar in chart_data]
        volumes = [bar["volume"] for bar in chart_data]

        # EMA 계산 함수
        def ema(data, period):
            if len(data) < period:
                return data[-1] if data else 0
            k = 2 / (period + 1)
            result = sum(data[:period]) / period  # SMA 시드
            for val in data[period:]:
                result = val * k + result * (1 - k)
            return result

        # SMA 계산 함수
        def sma(data, period):
            if len(data) < period:
                return sum(data) / len(data) if data else 0
            return sum(data[-period:]) / period

        # RSI 계산
        def calc_rsi(data, period=14):
            if len(data) < period + 1:
                return 50.0
            gains, losses = [], []
            for i in range(1, len(data)):
                delta = data[i] - data[i - 1]
                gains.append(max(delta, 0))
                losses.append(max(-delta, 0))
            if len(gains) < period:
                return 50.0
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
            if avg_loss == 0:
                return 100.0
            rs = avg_gain / avg_loss
            return 100 - (100 / (1 + rs))

        # MACD
        ema12 = ema(closes, 12)
        ema26 = ema(closes, 26)
        macd_val = ema12 - ema26

        # 시그널 라인 (MACD의 EMA9) — 단순 근사
        # 최근 9개 MACD 값을 구하기 위해 반복 계산
        macd_series = []
        for i in range(max(26, len(closes) - 30), len(closes)):
            subset = closes[:i + 1]
            e12 = ema(subset, 12)
            e26 = ema(subset, 26)
            macd_series.append(e12 - e26)

        macd_signal = ema(macd_series, 9) if len(macd_series) >= 9 else macd_val
        macd_hist = macd_val - macd_signal

        # Bollinger Bands
        sma20 = sma(closes, 20)
        if len(closes) >= 20:
            std20 = (sum((c - sma20) ** 2 for c in closes[-20:]) / 20) ** 0.5
        else:
            std20 = 0
        bb_upper = sma20 + 2 * std20
        bb_lower = sma20 - 2 * std20

        # 이동평균
        ema9 = ema(closes, 9)
        ema21 = ema(closes, 21)
        ema50 = ema(closes, 50) if len(closes) >= 50 else ema(closes, len(closes))

        # ATR (Average True Range)
        atr_vals = []
        for i in range(1, len(chart_data)):
            h = chart_data[i]["high"]
            l = chart_data[i]["low"]
            pc = chart_data[i - 1]["close"]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            atr_vals.append(tr)
        atr14 = sma(atr_vals, 14) if len(atr_vals) >= 14 else (sma(atr_vals, len(atr_vals)) if atr_vals else 0)

        # 볼륨 SMA
        vol_sma20 = sma(volumes, 20) if len(volumes) >= 20 else sma(volumes, len(volumes))

        price = closes[-1]

        # ─── 추가 지표 (YouTube 검증 전략) ─────────────────────
        # 200 EMA — 트렌드 필터 (TradingLab, Trade Pro 공통)
        ema200 = ema(closes, 200) if len(closes) >= 200 else ema(closes, len(closes))
        trend_200 = "Above 200EMA (Bullish)" if price > ema200 else "Below 200EMA (Bearish)"

        # 20/50 EMA — 풀백 감지 (Trade Pro)
        ema20 = ema(closes, 20)
        pullback_to_ema = "Near EMA20" if abs(price - ema20) / price < 0.005 else \
                          "Near EMA50" if abs(price - ema50) / price < 0.01 else \
                          "Extended from EMAs"

        # MACD 제로라인 위치 (TradingLab)
        macd_zone = "Below Zero" if macd_val < 0 else "Above Zero"
        macd_cross_quality = ""
        if len(macd_series) >= 2:
            prev_macd = macd_series[-2]
            prev_signal = ema(macd_series[:-1], 9) if len(macd_series) > 9 else prev_macd
            if prev_macd <= prev_signal and macd_val > macd_signal:
                macd_cross_quality = "BULLISH CROSS"
                if macd_val < 0:
                    macd_cross_quality += " below zero (STRONG BUY signal - TradingLab)"
            elif prev_macd >= prev_signal and macd_val < macd_signal:
                macd_cross_quality = "BEARISH CROSS"
                if macd_val > 0:
                    macd_cross_quality += " above zero (STRONG SELL signal - TradingLab)"

        # 이격도 (Disparity Index) — BNF 전략
        disparity_20 = ((price - sma20) / sma20) * 100 if sma20 > 0 else 0
        bnf_signal = ""
        rsi_val = calc_rsi(closes)
        if disparity_20 < -5 and rsi_val < 30 and macd_hist > 0:
            bnf_signal = "🔥 BNF BUY SIGNAL (급락 후 반전: 이격도 < -5%, RSI 과매도, MACD 반전)"
        elif disparity_20 > 5 and rsi_val > 70:
            bnf_signal = "⚠️ BNF SELL SIGNAL (과열: 이격도 > +5%, RSI 과매수)"

        # Stochastic RSI (Data Trader 전략)
        def calc_stoch_rsi(data, rsi_period=14, stoch_period=14):
            if len(data) < rsi_period + stoch_period:
                return 50.0
            rsi_values = []
            for i in range(stoch_period + rsi_period, len(data) + 1):
                rsi_values.append(calc_rsi(data[:i], rsi_period))
            if len(rsi_values) < stoch_period:
                return 50.0
            recent = rsi_values[-stoch_period:]
            rsi_min, rsi_max = min(recent), max(recent)
            if rsi_max == rsi_min:
                return 50.0
            return ((rsi_values[-1] - rsi_min) / (rsi_max - rsi_min)) * 100

        stoch_rsi = calc_stoch_rsi(closes)
        stoch_status = "Oversold" if stoch_rsi < 20 else "Overbought" if stoch_rsi > 80 else "Neutral"

        # 삼중 확인 시그널 (Stochastic + RSI + MACD — Data Trader)
        triple_confirm = ""
        if stoch_rsi < 20 and rsi_val > 50 and macd_cross_quality.startswith("BULLISH"):
            triple_confirm = "✅ TRIPLE CONFIRM BUY (Stoch 과매도 + RSI 상승추세 + MACD 크로스)"
        elif stoch_rsi > 80 and rsi_val < 50 and macd_cross_quality.startswith("BEARISH"):
            triple_confirm = "✅ TRIPLE CONFIRM SELL (Stoch 과매수 + RSI 하락추세 + MACD 크로스)"

        # ─── 래리 윌리엄스 변동성 돌파 (K=0.7) ────────────────
        highs = [bar["high"] for bar in chart_data]
        lows = [bar["low"] for bar in chart_data]
        larry_signal = ""
        if len(chart_data) >= 2:
            prev_range = highs[-2] - lows[-2]  # 전일 변동폭
            k_val = 0.7
            breakout_level = chart_data[-1]["open"] + prev_range * k_val
            breakdown_level = chart_data[-1]["open"] - prev_range * k_val
            if price >= breakout_level:
                larry_signal = f"🚀 LARRY WILLIAMS BUY (변동성 돌파: 가격 ${price:.2f} > 돌파선 ${breakout_level:.2f})"
            elif price <= breakdown_level:
                larry_signal = f"📉 LARRY WILLIAMS SELL (하방 돌파: 가격 ${price:.2f} < 돌파선 ${breakdown_level:.2f})"
            else:
                larry_signal = f"대기중 (돌파선: 상 ${breakout_level:.2f} / 하 ${breakdown_level:.2f})"

        # ─── 터틀 트레이딩 (20일/55일 돈치안 채널) ─────────────
        turtle_signal = ""
        if len(chart_data) >= 55:
            high_20 = max(highs[-20:])
            low_10 = min(lows[-10:])
            high_55 = max(highs[-55:])
            low_20 = min(lows[-20:])
            if price >= high_20:
                turtle_signal = f"🐢 TURTLE S1 BUY (20일 최고가 ${high_20:.2f} 돌파)"
            elif price >= high_55:
                turtle_signal = f"🐢 TURTLE S2 BUY (55일 최고가 ${high_55:.2f} 돌파)"
            elif price <= low_10:
                turtle_signal = f"🐢 TURTLE S1 EXIT (10일 최저가 ${low_10:.2f} 이탈)"
            elif price <= low_20:
                turtle_signal = f"🐢 TURTLE S2 EXIT (20일 최저가 ${low_20:.2f} 이탈)"
        elif len(chart_data) >= 20:
            high_20 = max(highs[-20:])
            low_10 = min(lows[-10:])
            if price >= high_20:
                turtle_signal = f"🐢 TURTLE BUY (20일 최고가 ${high_20:.2f} 돌파)"
            elif price <= low_10:
                turtle_signal = f"🐢 TURTLE EXIT (10일 최저가 ${low_10:.2f} 이탈)"

        # ─── BB+RSI+ADX 평균회귀 (Quant Tactics, 179% 수익) ───
        # ADX 계산 (순수 파이썬)
        def calc_adx(candles, period=14):
            if len(candles) < period + 1:
                return 25.0
            plus_dm, minus_dm, tr_vals = [], [], []
            for i in range(1, len(candles)):
                h, l = candles[i]["high"], candles[i]["low"]
                ph, pl = candles[i-1]["high"], candles[i-1]["low"]
                pc = candles[i-1]["close"]
                p_dm = max(h - ph, 0) if (h - ph) > (pl - l) else 0
                m_dm = max(pl - l, 0) if (pl - l) > (h - ph) else 0
                tr = max(h - l, abs(h - pc), abs(l - pc))
                plus_dm.append(p_dm)
                minus_dm.append(m_dm)
                tr_vals.append(tr)
            if len(tr_vals) < period:
                return 25.0
            atr_s = sum(tr_vals[:period])
            pdm_s = sum(plus_dm[:period])
            mdm_s = sum(minus_dm[:period])
            dx_vals = []
            for i in range(period, len(tr_vals)):
                atr_s = atr_s - atr_s / period + tr_vals[i]
                pdm_s = pdm_s - pdm_s / period + plus_dm[i]
                mdm_s = mdm_s - mdm_s / period + minus_dm[i]
                if atr_s > 0:
                    pdi = (pdm_s / atr_s) * 100
                    mdi = (mdm_s / atr_s) * 100
                    if pdi + mdi > 0:
                        dx_vals.append(abs(pdi - mdi) / (pdi + mdi) * 100)
            if not dx_vals:
                return 25.0
            return sum(dx_vals[-period:]) / min(period, len(dx_vals))

        adx_val = calc_adx(chart_data)
        mean_rev_signal = ""
        bb_width = bb_upper - bb_lower
        if bb_width > 0:
            bb_pct_b = (price - bb_lower) / bb_width
        else:
            bb_pct_b = 0.5
        if price < bb_lower and rsi_val > 50 and adx_val > 20:
            mean_rev_signal = f"📊 MEAN REVERSION BUY (BB하단 이탈 + RSI>50 상승추세 + ADX={adx_val:.0f} 추세존재)"
        elif price > bb_upper and rsi_val < 50 and adx_val > 20:
            mean_rev_signal = f"📊 MEAN REVERSION SELL (BB상단 돌파 + RSI<50 하락추세 + ADX={adx_val:.0f})"

        # ─── 시장 상태 자동 분류 (Market Regime) ──────────────
        regime = "UNKNOWN"
        if len(closes) >= 50:
            sma50 = sma(closes, 50)
            slope_20 = (sma20 - sma(closes[:-5], 20)) / sma20 * 100 if len(closes) >= 25 else 0
            if ema9 > ema21 > ema50 and price > ema200 and adx_val > 25:
                regime = "STRONG_UPTREND"
            elif ema9 < ema21 < ema50 and price < ema200 and adx_val > 25:
                regime = "STRONG_DOWNTREND"
            elif adx_val < 20 and abs(slope_20) < 0.3:
                regime = "SIDEWAYS"
            elif price > ema200 and ema9 > ema21:
                regime = "MILD_UPTREND"
            elif price < ema200 and ema9 < ema21:
                regime = "MILD_DOWNTREND"
            else:
                regime = "TRANSITION"

        regime_advice = {
            "STRONG_UPTREND": "강한 상승장 → 트렌드 추종 (래리윌리엄스/터틀), 풀백 매수 적극",
            "MILD_UPTREND": "완만한 상승 → 풀백 매수, 욕심 줄이기",
            "SIDEWAYS": "횡보장 → 평균회귀(BB반등) 전략, 트렌드 전략 중지",
            "MILD_DOWNTREND": "완만한 하락 → 매수 자제, 기존 포지션만 관리",
            "STRONG_DOWNTREND": "강한 하락장 → BNF 역발상만 (극과매도시), 신규매수 금지",
            "TRANSITION": "전환기 → 관망, 방향 확인 후 진입",
            "UNKNOWN": "데이터 부족",
        }.get(regime, "")

        return {
            "ema9": ema9,
            "ema21": ema21,
            "ema50": ema50,
            "ema200": ema200,
            "ema_trend": "Bullish (9>21>50)" if ema9 > ema21 > ema50 else "Bearish (9<21<50)" if ema9 < ema21 < ema50 else "Mixed",
            "trend_200": trend_200,
            "pullback_status": pullback_to_ema,
            "rsi": rsi_val,
            "stoch_rsi": stoch_rsi,
            "stoch_status": stoch_status,
            "macd": macd_val,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "macd_zone": macd_zone,
            "macd_cross": macd_cross_quality,
            "bb_upper": bb_upper,
            "bb_middle": sma20,
            "bb_lower": bb_lower,
            "bb_position": "Near Upper" if price > bb_upper * 0.99 else "Near Lower" if price < bb_lower * 1.01 else "Middle",
            "disparity_20": disparity_20,
            "bnf_signal": bnf_signal,
            "triple_confirm": triple_confirm,
            "larry_signal": larry_signal,
            "turtle_signal": turtle_signal,
            "mean_rev_signal": mean_rev_signal,
            "adx": adx_val,
            "bb_pct_b": bb_pct_b,
            "market_regime": regime,
            "regime_advice": regime_advice,
            "atr14": atr14,
            "volume": volumes[-1] if volumes else 0,
            "volume_sma20": vol_sma20,
            "volume_status": "Above Avg" if (volumes and volumes[-1] > vol_sma20) else "Below Avg",
            "price_change_5d": ((closes[-1] / closes[-5]) - 1) * 100 if len(closes) >= 5 else 0,
            "price_change_20d": ((closes[-1] / closes[-20]) - 1) * 100 if len(closes) >= 20 else 0,
        }

    def _build_prompt(
        self,
        symbol: str,
        quote: dict,
        technicals: dict,
        news: str,
        past_decisions: str,
        balance_info: str,
        positions_info: str,
        ml_section: str = "",
        rag_context: str = "",
    ) -> str:
        """Gemini에 전송할 프롬프트 빌드"""
        stock_name = STOCK_NAMES.get(symbol, symbol)

        tech_section = ""
        if technicals:
            # 특수 시그널 라인
            # 특수 시그널 수집
            special_signals = ""
            for sig_key in ["bnf_signal", "triple_confirm", "larry_signal", "turtle_signal", "mean_rev_signal"]:
                sig = technicals.get(sig_key, "")
                if sig and not sig.startswith("대기"):
                    special_signals += f"\n{sig}"
            if technicals.get("macd_cross"):
                special_signals += f"\n- MACD Cross: {technicals['macd_cross']}"
            if special_signals:
                special_signals = f"\n\n### ⚡ ACTIVE SIGNALS{special_signals}"

            tech_section = f"""## Market Regime: {technicals['market_regime']}
→ {technicals['regime_advice']}

## Technical Indicators
- EMA9: ${technicals['ema9']:.2f} | EMA21: ${technicals['ema21']:.2f} | EMA50: ${technicals['ema50']:.2f} | EMA200: ${technicals['ema200']:.2f}
- EMA Trend: {technicals['ema_trend']}
- 200 EMA Filter: {technicals['trend_200']}
- Pullback Status: {technicals['pullback_status']}
- RSI(14): {technicals['rsi']:.1f} {'(Oversold)' if technicals['rsi'] < 30 else '(Overbought)' if technicals['rsi'] > 70 else '(Neutral)'}
- Stochastic RSI: {technicals['stoch_rsi']:.1f} ({technicals['stoch_status']})
- ADX: {technicals['adx']:.1f} ({'Strong Trend' if technicals['adx'] > 25 else 'Weak/Sideways'})
- MACD: {technicals['macd']:.4f} | Signal: {technicals['macd_signal']:.4f} | Histogram: {technicals['macd_hist']:.4f}
- MACD Zone: {technicals['macd_zone']} | Trend: {'Bullish' if technicals['macd_hist'] > 0 else 'Bearish'}
- Disparity Index (20): {technicals['disparity_20']:+.2f}%
- Bollinger Bands: Lower=${technicals['bb_lower']:.2f} | Middle=${technicals['bb_middle']:.2f} | Upper=${technicals['bb_upper']:.2f}
- BB %B: {technicals['bb_pct_b']:.3f} | Position: {technicals['bb_position']}
- ATR(14): ${technicals['atr14']:.2f}
- Volume: {technicals['volume']:,} ({technicals['volume_status']}, avg={technicals['volume_sma20']:,.0f})
- 5-day Change: {technicals['price_change_5d']:+.2f}%
- 20-day Change: {technicals['price_change_20d']:+.2f}%{special_signals}"""

        return f"""You are an active US stock trader managing a paper trading portfolio via Korea Investment & Securities (KIS) virtual account.
This is PAPER TRADING (모의투자) — there is ZERO real money risk. Be BOLD and trade actively.

## {symbol} ({stock_name}) Market Data
Current Price: ${quote['price']:.2f}
Open: ${quote['open']:.2f} | High: ${quote['high']:.2f} | Low: ${quote['low']:.2f}
Previous Close: ${quote['prev_price']:.2f}
Change: {quote['change']:+.2f}%
Volume: {quote['volume']:,}

{tech_section}
{ml_section}
## Recent News Headlines
{news}

{rag_context}

## Your Previous Decisions for {symbol}
{past_decisions}

## Portfolio Context
{balance_info}

## Current Positions
{positions_info}

## Trading Rules
- Paper trading with ~${BASE_TRADE_AMOUNT_USD:.0f} per trade — NO REAL RISK
- Max 5 concurrent positions across {len(STOCK_LIST)} stocks
- Trading US stocks via KIS (한국투자증권) mock account
- Goal: ACTIVELY TRADE but LET WINNERS RUN. Don't sell too early.
- Hold time target: 1-5 trading days (swing trading, NOT scalping)
- Consider position sizing: suggest stake_multiplier (0.5x to 2.0x of base ${BASE_TRADE_AMOUNT_USD:.0f})

## CRITICAL LESSONS FROM CRYPTO BOT (APPLY HERE TOO)
Our crypto bot had 20.8% win rate because:
1. Stops were too tight — small dips triggered exits in uptrends
2. AI sold too fast on minor pullbacks instead of letting winners run
3. Hold time was too short (minutes instead of hours/days)

THEREFORE:
- BUY only on STRONG momentum (RSI + EMA alignment + volume confirmation)
- Do NOT sell for dips < 2%. Minor pullbacks are normal.
- If stock is in uptrend (EMA9 > EMA21 > EMA50), do NOT sell on small pullbacks
- Set stop_loss at least 3-5% below entry (not 0.5%)
- Set price_target at least 3-5% above entry (minimum 2:1 reward-to-risk)
- PATIENCE = PROFIT. Hold through minor volatility.

## PROVEN STRATEGY RULES (from backtested YouTube strategies)
### Rule 1 — 200 EMA Trend Filter (TradingLab, Trade Pro)
- ONLY BUY if price is ABOVE 200 EMA. ONLY SELL/SHORT if price is BELOW 200 EMA.
- This single rule eliminates most losing trades. NEVER trade against the 200 EMA trend.

### Rule 2 — MACD Cross Below Zero = Strong Buy (TradingLab, 86% win rate)
- MACD bullish cross BELOW the zero line = strongest buy signal.
- MACD bearish cross ABOVE the zero line = strongest sell signal.
- MACD cross near zero line = weak signal, lower confidence.

### Rule 3 — BNF Mean Reversion (BNF: ¥2M → ¥40B)
- If disparity index < -5% AND RSI oversold AND MACD histogram turning green → STRONG BUY
- Take profit when disparity returns to 0. Stop loss at previous low.
- This is a COUNTER-TREND strategy — only use when indicators clearly show reversal.

### Rule 4 — Triple Confirmation (Stochastic + RSI + MACD)
- BUY: Stochastic RSI oversold + RSI > 50 (uptrend) + MACD bullish cross
- SELL: Stochastic RSI overbought + RSI < 50 (downtrend) + MACD bearish cross
- All 3 must agree. If only 1-2 agree, reduce confidence.

### Rule 5 — Pullback Entry (Trade Pro, 8yr experience)
- Wait for price to pull back to 20 EMA before entering (discount entry)
- Don't buy when price is extended far above EMAs (premium = risky)
- Stochastic RSI should be fully reset (oversold in uptrend) before entry

### Rule 6 — Larry Williams Volatility Breakout ($10K → $1.13M, K=0.7)
- If price breaks above (today's open + yesterday's range × 0.7) → BUY
- If price breaks below (today's open - yesterday's range × 0.7) → SELL
- Best in trending markets (ADX > 25). Weak in sideways.

### Rule 7 — Turtle Trading ($100M proven, Donchian Channel)
- Price breaks 20-day high → BUY (System 1)
- Price breaks 10-day low → EXIT
- Swing trading (hold days to weeks). High drawdown tolerance needed.

### Rule 8 — Mean Reversion (BB + RSI + ADX, Freqtrade 179% profit)
- Price below lower BB + RSI > 50 (uptrend) + ADX > 20 → BUY (oversold bounce)
- Price above upper BB + RSI < 50 (downtrend) + ADX > 20 → SELL
- Best in SIDEWAYS markets. Don't use in strong trends.

### Rule 9 — Risk:Reward minimum 1.5:1
- Set stop_loss and price_target so that reward >= 1.5x risk
- If R:R < 1.5, skip the trade regardless of signal strength

## MARKET REGIME STRATEGY SELECTION
- STRONG_UPTREND → Use Larry Williams, Turtle, Pullback Entry. Ignore Mean Reversion.
- MILD_UPTREND → Use Pullback Entry, Triple Confirm. Moderate sizing.
- SIDEWAYS → Use Mean Reversion (BB bounce). Avoid trend-following strategies.
- MILD_DOWNTREND → Reduce positions. Only BNF at extremes.
- STRONG_DOWNTREND → Cash is king. Only BNF reversal at extreme oversold.

## Decision Framework
1. REGIME: Check Market Regime FIRST. Choose appropriate strategy for current regime.
2. TREND: Check 200 EMA. Don't trade against it (except BNF reversal).
3. ACTIVE SIGNALS: Check ⚡ signals above — Larry Williams, Turtle, BNF, Triple Confirm, Mean Reversion.
4. ML SIGNALS: XGBoost/LSTM/RL consensus. ML agrees with signals → boost confidence.
5. ENTRY TIMING: Is price at discount (near 20/50 EMA)? Or extended (skip)?
6. MACD QUALITY: Where is MACD cross? Below zero = strongest for buy.
7. NEWS: Recent headlines impact?
8. SELF-REVIEW: Learn from past decisions.
9. R:R CHECK: price_target / stop_loss >= 1.5:1.
10. POSITION SIZING: stake_multiplier (0.5x to 2.0x)

## Confidence Guide (AGGRESSIVE for paper trading)
- 0.8-1.0: Very strong signal -> MUST act
- 0.65-0.8: Good signal -> ACT
- 0.5-0.65: Moderate -> lean toward acting
- 0.3-0.5: Weak -> hold
- 0.0-0.3: No signal

## SELL RULES (STRICT)
- Only sell if: trend reversed (EMA cross bearish) OR RSI > 80 overbought OR stop_loss hit
- Do NOT sell just because price dipped 0.5-1%. That's noise.
- If in profit and trend still bullish: HOLD, do not sell.

Respond JSON only: {{"action": "buy" or "sell" or "hold", "confidence": 0.0-1.0, "reason": "<detailed 30 word analysis>", "risk_level": "low/medium/high", "price_target": "<target price or N/A>", "stop_loss": "<stop loss price or N/A>", "stake_multiplier": 0.5-2.0}}"""

    def get_decision(
        self,
        symbol: str,
        quote: dict,
        chart_data: list,
        news: str,
        past_decisions: str,
        balance_info: str,
        positions_info: str,
        rag_context: str = "",
    ) -> dict:
        """Gemini에게 매매 판단 요청 + 멀티에이전트 보조 판단.

        v7.0: RAG 컨텍스트 + QuickCrew 멀티에이전트 + 비용 추적.
        """
        # 캐시 확인
        now = time.time()
        cached = self._cache.get(symbol)
        if cached and (now - cached["ts"]) < self._cache_ttl:
            return cached["decision"]

        # 기술지표 계산
        technicals = self._compute_technicals(chart_data)

        # ML 시그널 생성
        ml_section = ""
        if _ML_ENGINE is not None and len(chart_data) >= 30:
            try:
                ml_signals = _ML_ENGINE.get_signals(chart_data)
                ml_section = "\n" + _ML_ENGINE.format_for_llm_prompt(ml_signals) + "\n"
                logger.info(
                    f"ML signal {symbol}: {ml_signals['consensus'].get('action','?')} "
                    f"(conf={ml_signals['consensus'].get('confidence',0):.2f})"
                )
            except Exception as ml_err:
                logger.warning(f"ML signal failed for {symbol}: {ml_err}")

        # v7.0: 멀티에이전트 보조 판단
        crew_section = ""
        if _QUICK_CREW and _QUICK_CREW.is_ready:
            try:
                market_data = (
                    f"Price: ${quote['price']:.2f}, Change: {quote.get('change', 0):+.2f}%, "
                    f"Volume: {quote.get('volume', 0):,}"
                )
                tech_summary = ""
                if technicals:
                    tech_summary = (
                        f"RSI={technicals.get('rsi', 0):.1f}, "
                        f"MACD={technicals.get('macd', 0):.4f}, "
                        f"Regime={technicals.get('market_regime', '?')}, "
                        f"EMA Trend={technicals.get('ema_trend', '?')}"
                    )
                crew_result = _QUICK_CREW.analyze(
                    symbol, market_data, news, tech_summary,
                    past_decisions, balance_info, rag_context,
                )
                if crew_result:
                    crew_action = crew_result.get("action", "hold")
                    crew_conf = crew_result.get("confidence", 0)
                    panel = crew_result.get("panel", "")
                    crew_section = (
                        f"\n## Multi-Agent Panel (v7.0)\n"
                        f"Consensus: {crew_action} (confidence={crew_conf:.2f})\n"
                        f"Panel: {panel}\n"
                        f"Reasoning: {crew_result.get('reason', '')}\n"
                    )
            except Exception as crew_err:
                logger.warning(f"QuickCrew failed for {symbol}: {crew_err}")

        # 프롬프트 빌드
        prompt = self._build_prompt(
            symbol, quote, technicals, news, past_decisions,
            balance_info, positions_info,
            ml_section=ml_section + crew_section,
            rag_context=rag_context,
        )

        try:
            t0 = time.time()
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={self.api_key}",
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "maxOutputTokens": 8192,
                        "temperature": 0.2,
                        "responseMimeType": "application/json",
                    },
                },
                timeout=60,
            )
            latency_ms = int((time.time() - t0) * 1000)

            data = resp.json()

            # 에러 체크
            if "error" in data:
                logger.error(f"Gemini API 에러: {data['error']}")
                return {"action": "hold", "confidence": 0, "reason": f"API error: {data['error'].get('message', '')}"}

            content = data["candidates"][0]["content"]["parts"][0]["text"].strip()

            # JSON 파싱
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            result = json.loads(content)
            decision = {
                "action": result.get("action", "hold").lower(),
                "confidence": max(0.0, min(1.0, float(result.get("confidence", 0)))),
                "reason": result.get("reason", ""),
                "risk_level": result.get("risk_level", "medium"),
                "price_target": str(result.get("price_target", "N/A")),
                "stop_loss": str(result.get("stop_loss", "N/A")),
                "stake_multiplier": max(0.5, min(2.0, float(result.get("stake_multiplier", 1.0)))),
            }

            # 토큰 사용량 + 비용 추적
            usage = data.get("usageMetadata", {})
            input_tokens = usage.get("promptTokenCount", 0)
            thinking_tokens = usage.get("thoughtsTokenCount", 0)
            output_tokens = usage.get("candidatesTokenCount", 0)

            if _COST_TRACKER:
                _COST_TRACKER.record(
                    provider="gemini-direct", model="gemini-2.5-flash",
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    thinking_tokens=thinking_tokens, symbol=symbol,
                    latency_ms=latency_ms,
                )

            logger.info(
                f"AI {symbol}: {decision['action']} "
                f"(conf={decision['confidence']:.2f}, risk={decision['risk_level']}, "
                f"think={thinking_tokens}tok, {latency_ms}ms) "
                f"{decision['reason'][:80]}"
            )

            # 판단 기록 (JSONL + RAG)
            log_decision(
                symbol, decision, quote.get("price", 0),
                input_tokens, thinking_tokens, output_tokens,
            )
            if _NEWS_RAG and _NEWS_RAG.is_ready:
                try:
                    tech_summary = f"RSI={technicals.get('rsi',0):.0f} Regime={technicals.get('market_regime','?')}"
                    _NEWS_RAG.ingest_decision(symbol, decision, quote.get("price", 0), tech_summary)
                except Exception:
                    pass

            # 캐시 저장
            self._cache[symbol] = {"decision": decision, "ts": now}
            return decision

        except Exception as e:
            logger.error(f"Gemini 판단 실패 ({symbol}): {e}")
            traceback.print_exc()
            return {"action": "hold", "confidence": 0, "reason": f"API error: {e}", "stake_multiplier": 1.0}


# ─── 메인 트레이딩 봇 ──────────────────────────────────────────
class StockTradingBot:
    """미국 주식 AI 트레이딩 봇 메인 클래스"""

    def __init__(self):
        logger.info("=" * 60)
        logger.info("미국 주식 AI 트레이딩 봇 초기화")
        logger.info("=" * 60)

        self.kis = KISClient()
        self.ai = GeminiDecisionEngine()
        self.tg = TelegramNotifier()

        self._last_premarket = None  # 마지막 프리마켓 분석 날짜
        self._total_api_cost_usd = 0.0

    def _format_balance_info(self, balance: dict) -> str:
        """잔고 정보를 텍스트로 변환"""
        if not balance:
            return "Balance unavailable."
        return (
            f"Total Assets: ${balance['total']:,.2f}\n"
            f"Cash (Deposit): ${balance['deposit']:,.2f}\n"
            f"Invested: ${balance['total'] - balance['deposit']:,.2f}"
        )

    def _format_positions_info(self, positions: list) -> str:
        """보유 포지션 정보를 텍스트로 변환"""
        if not positions:
            return "No current positions."

        lines = []
        for p in positions:
            lines.append(
                f"  {p['symbol']} ({p['name']}): {p['qty']}주 "
                f"avg=${p['avg_price']:.2f} cur=${p['current_price']:.2f} "
                f"P/L={p['profit_rate']:.2f}%"
            )
        return "\n".join(lines)

    def _send_trade_notification(self, action: str, symbol: str, decision: dict, quote: dict, order_result: dict = None):
        """텔레그램으로 매매 알림 전송 (한국어)"""
        stock_name = STOCK_NAMES.get(symbol, symbol)
        conf = decision.get("confidence", 0)
        reason = decision.get("reason", "")
        price = quote.get("price", 0)
        multiplier = decision.get("stake_multiplier", 1.0)

        if action == "buy":
            emoji = "🟢"
            action_kr = "매수"
        elif action == "sell":
            emoji = "🔴"
            action_kr = "매도"
        else:
            return

        order_info = ""
        if order_result:
            order_info = (
                f"\n주문번호: {order_result.get('order_number', 'N/A')}"
                f"\n수량: {order_result.get('qty', 0)}주"
            )

        msg = (
            f"{emoji} <b>[미국주식] {action_kr} 실행</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"종목: <code>{symbol}</code> ({stock_name})\n"
            f"가격: ${price:.2f}\n"
            f"확신도: {conf:.0%} | 배율: {multiplier:.1f}x\n"
            f"리스크: {decision.get('risk_level', 'N/A')}\n"
            f"목표가: {decision.get('price_target', 'N/A')}\n"
            f"손절가: {decision.get('stop_loss', 'N/A')}{order_info}\n"
            f"\n💡 {reason}"
        )
        self.tg.send(msg)

    def _send_hold_summary(self, decisions: dict):
        """관망 판단 요약 알림 (너무 자주 보내지 않도록)"""
        hold_items = []
        for symbol, dec in decisions.items():
            if dec["action"] == "hold" and dec.get("confidence", 0) >= 0.3:
                hold_items.append(f"  {symbol}: conf={dec['confidence']:.0%} - {dec['reason'][:50]}")

        if hold_items:
            msg = (
                f"⏸ <b>[미국주식] AI 분석 완료</b>\n"
                f"━━━━━━━━━━━━━━\n"
                f"관망 종목 ({len(hold_items)}건):\n"
                + "\n".join(hold_items[:5])
            )
            self.tg.send(msg)

    def run_analysis_cycle(self):
        """전체 종목에 대한 AI 분석 + 매매 실행 사이클"""
        logger.info("─" * 40)
        logger.info("분석 사이클 시작")

        # 잔고 및 포지션 조회
        balance = self.kis.get_balance()
        positions = self.kis.get_positions()
        balance_info = self._format_balance_info(balance)
        positions_info = self._format_positions_info(positions)

        current_position_symbols = {p["symbol"] for p in positions}
        decisions = {}

        for symbol in STOCK_LIST:
            if _shutdown:
                break

            logger.info(f"분석 중: {symbol}")

            # 1. 시세 조회
            quote = self.kis.get_quote(symbol)
            if not quote:
                logger.warning(f"{symbol}: 시세 조회 실패, 건너뜀")
                continue

            # 2. 차트 데이터 조회
            chart_data = self.kis.get_daily_chart(symbol, days=60)

            # 3. 뉴스 수집 + RAG
            news, rag_context = fetch_news(symbol, STOCK_NAMES.get(symbol, symbol))

            # 4. 과거 판단 이력
            past_decisions = load_recent_decisions(symbol)

            # 5. AI 판단 요청 (RAG 컨텍스트 포함)
            decision = self.ai.get_decision(
                symbol, quote, chart_data, news,
                past_decisions, balance_info, positions_info,
                rag_context=rag_context,
            )
            decisions[symbol] = decision

            action = decision.get("action", "hold")
            confidence = decision.get("confidence", 0)
            multiplier = decision.get("stake_multiplier", 1.0)

            # 6. 매매 실행
            if action == "buy" and confidence >= CONFIDENCE_THRESHOLD:
                # 이미 보유 중이면 추가 매수 제한
                if symbol in current_position_symbols:
                    logger.info(f"{symbol}: 이미 보유 중, 추가 매수 건너뜀")
                    continue

                # 최대 포지션 수 체크
                if len(current_position_symbols) >= 5:
                    logger.info(f"최대 포지션(5) 도달, {symbol} 매수 건너뜀")
                    continue

                # 포지션 크기 계산
                trade_amount = BASE_TRADE_AMOUNT_USD * multiplier
                qty = max(1, int(trade_amount / quote["price"]))

                order_result = self.kis.buy_stock(symbol, qty)
                if order_result:
                    current_position_symbols.add(symbol)
                    self._send_trade_notification("buy", symbol, decision, quote, order_result)

            elif action == "sell" and confidence >= CONFIDENCE_THRESHOLD:
                # 보유 중인 경우에만 매도
                if symbol not in current_position_symbols:
                    logger.info(f"{symbol}: 미보유, 매도 건너뜀")
                    continue

                # 보유 수량 조회
                pos = next((p for p in positions if p["symbol"] == symbol), None)
                if not pos or pos["qty"] <= 0:
                    continue

                # 손실 중이면 매도 확신도 더 높아야 함 (작은 손실에 패닉 매도 방지)
                if pos["avg_price"] > 0:
                    pnl_pct = ((quote["price"] - pos["avg_price"]) / pos["avg_price"]) * 100
                    if -3.0 < pnl_pct < 0:
                        # 0~3% 손실 구간: 확신도 0.8 이상만 매도
                        if confidence < 0.80:
                            logger.info(f"{symbol}: 손실 {pnl_pct:.1f}%이나 확신도 {confidence:.0%} 부족, 매도 보류")
                            continue
                    elif pnl_pct >= 0 and pnl_pct < 2.0:
                        # 0~2% 수익 구간: 차트 상승 중이면 매도 보류
                        tech = self.ai._compute_technicals(chart_data) if chart_data else {}
                        if tech and "Bullish" in tech.get("ema_trend", ""):
                            logger.info(f"{symbol}: 수익 {pnl_pct:.1f}% + 상승트렌드, 매도 보류 (홀딩)")
                            continue

                order_result = self.kis.sell_stock(symbol, pos["qty"])
                if order_result:
                    current_position_symbols.discard(symbol)
                    self._send_trade_notification("sell", symbol, decision, quote, order_result)

            # API 부하 방지
            time.sleep(1)

        # 관망 요약 (매수/매도가 없었을 때만)
        traded = any(
            d["action"] in ("buy", "sell") and d.get("confidence", 0) >= CONFIDENCE_THRESHOLD
            for d in decisions.values()
        )
        if not traded and decisions:
            self._send_hold_summary(decisions)

        logger.info(f"분석 사이클 완료: {len(decisions)}개 종목 분석")

    def run_premarket_analysis(self):
        """프리마켓 종합 분석 리포트"""
        today = date.today()
        if self._last_premarket == today:
            return  # 오늘 이미 실행됨

        logger.info("프리마켓 분석 시작")
        self._last_premarket = today

        lines = [
            f"📋 <b>[미국주식] 프리마켓 분석</b>",
            f"━━━━━━━━━━━━━━",
            f"일시: {datetime.now().strftime('%Y-%m-%d %H:%M')} KST",
            "",
        ]

        for symbol in STOCK_LIST:
            if _shutdown:
                break

            quote = self.kis.get_quote(symbol)
            if not quote:
                lines.append(f"❌ {symbol}: 시세 조회 실패")
                continue

            chart_data = self.kis.get_daily_chart(symbol, days=30)
            technicals = self.ai._compute_technicals(chart_data) if chart_data else {}

            name = STOCK_NAMES.get(symbol, symbol)
            price = quote["price"]
            change = quote.get("change", 0)
            rsi = technicals.get("rsi", 0)
            trend = technicals.get("ema_trend", "N/A")

            emoji = "🟢" if change >= 0 else "🔴"
            rsi_label = "과매도" if rsi < 30 else "과매수" if rsi > 70 else "중립"

            lines.append(
                f"{emoji} <code>{symbol:5s}</code> ({name})\n"
                f"   ${price:.2f} ({change:+.2f}%) | RSI={rsi:.0f}({rsi_label}) | {trend}"
            )
            time.sleep(0.5)

        # 포지션 현황
        positions = self.kis.get_positions()
        if positions:
            lines.append("")
            lines.append("<b>📈 보유 포지션</b>")
            for p in positions:
                profit_emoji = "🟢" if p["profit_rate"] >= 0 else "🔴"
                lines.append(
                    f"  {profit_emoji} {p['symbol']}: {p['qty']}주 "
                    f"P/L={p['profit_rate']:+.2f}%"
                )

        self.tg.send("\n".join(lines))
        logger.info("프리마켓 분석 전송 완료")

    def run(self):
        """메인 루프"""
        self.tg.send(
            "🤖 <b>[미국주식] AI 트레이딩 봇 시작</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"대상 종목: {', '.join(STOCK_LIST)}\n"
            f"분석 주기: {DECISION_CYCLE_SECONDS // 60}분\n"
            f"매매 기준: 확신도 {CONFIDENCE_THRESHOLD:.0%} 이상\n"
            f"모드: 모의투자 (Paper Trading)"
        )

        logger.info(f"봇 시작 — 대상: {STOCK_LIST}")
        logger.info(f"분석 주기: {DECISION_CYCLE_SECONDS}초, 확신도 기준: {CONFIDENCE_THRESHOLD}")

        while not _shutdown:
            try:
                now = datetime.now()
                hour = now.hour
                minute = now.minute

                # 프리마켓 분석 (KST 23:00 ~ 23:29)
                if hour == PREMARKET_HOUR_KST and minute < 30:
                    self.run_premarket_analysis()

                # 미국 장 시간 체크
                if self.kis.is_market_open():
                    self.run_analysis_cycle()
                    logger.info(f"다음 분석까지 {DECISION_CYCLE_SECONDS}초 대기")
                    self._sleep_interruptible(DECISION_CYCLE_SECONDS)
                else:
                    # 장 외 시간 — 10분마다 체크
                    next_check = 600
                    logger.info(
                        f"미국 장 닫힘 (현재 KST {hour:02d}:{minute:02d}). "
                        f"{next_check // 60}분 후 재확인"
                    )
                    self._sleep_interruptible(next_check)

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"메인 루프 에러: {e}")
                traceback.print_exc()
                self._sleep_interruptible(60)

        # 종료 처리 + 비용 리포트
        cost_msg = ""
        if _COST_TRACKER:
            cost_msg = f"\n{_COST_TRACKER.daily_report()}"
        self.tg.send(f"🔴 <b>[미국주식] AI 트레이딩 봇 종료</b>{cost_msg}")
        logger.info("봇 종료")

    def _sleep_interruptible(self, seconds: int):
        """인터럽트 가능한 sleep"""
        end = time.time() + seconds
        while time.time() < end and not _shutdown:
            time.sleep(min(1, end - time.time()))


# ─── 엔트리 포인트 ─────────────────────────────────────────────
if __name__ == "__main__":
    try:
        bot = StockTradingBot()
        bot.run()
    except ValueError as e:
        logger.error(f"초기화 실패: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"치명적 에러: {e}")
        traceback.print_exc()
        sys.exit(1)
