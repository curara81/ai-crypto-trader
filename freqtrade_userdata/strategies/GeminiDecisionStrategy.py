import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import requests
from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta
import numpy as np

# ML Signal Engine 임포트
TRADING_ROOT = os.environ.get("TRADING_ROOT", os.path.expanduser("~/trading"))
sys.path.insert(0, os.path.join(TRADING_ROOT, "scripts"))
try:
    from ml_signal_engine import MLSignalEngine
    _ML_ENGINE = MLSignalEngine()
    logging.getLogger(__name__).info("MLSignalEngine 로드 성공")
except Exception as _ml_err:
    _ML_ENGINE = None
    logging.getLogger(__name__).warning(f"MLSignalEngine 로드 실패 (ML 시그널 없이 동작): {_ml_err}")

logger = logging.getLogger(__name__)


class GeminiDecisionStrategy(IStrategy):
    """
    L4: Gemini 2.5 Flash가 직접 매수/매도 판단하는 전략.

    매 캔들마다 기술지표를 계산하되, 1시간마다 Gemini에게
    차트 데이터 + 지표 + 뉴스를 전달하고 매수/매도/관망 판단을 받음.

    Requires env vars:
    - GEMINI_API_KEY
    - TAVILY_API_KEY (optional, for news)
    """

    INTERFACE_VERSION = 3

    # ROI/stoploss는 Gemini 판단의 안전망
    minimal_roi = {
        "0": 0.15,      # 15% 즉시 익절 (안전망)
        "360": 0.08,
        "720": 0.04,
    }

    stoploss = -0.15     # 15% 손절 (안전망, 여유 확대)
    trailing_stop = True
    trailing_stop_positive = 0.01   # 1% 수익 도달 후 트레일링 (기존 3%→1%로 낮춤)
    trailing_stop_positive_offset = 0.02  # 2% 수익 넘으면 트레일링 활성화 (기존 5%→2%)
    trailing_only_offset_is_reached = True

    timeframe = "5m"
    process_only_new_candles = True

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    # --- Gemini cache (instance state initialized in bot_start) ---
    _decision_ttl = 300  # 5분 캐시 = 캔들마다 새 판단 (GCP 크레딧 최대 활용)
    _log_file = os.path.join(TRADING_ROOT, "freqtrade_userdata/logs/gemini_decisions.jsonl")
    _log_max_mb = 100  # JSONL 로테이션 임계

    # --- Coin name map ---
    COIN_NAMES = {
        "BTC": "Bitcoin", "ETH": "Ethereum", "XRP": "Ripple",
        "SOL": "Solana", "ADA": "Cardano", "DOGE": "Dogecoin",
        "SHIB": "Shiba Inu", "AVAX": "Avalanche",
    }

    def bot_start(self, **kwargs) -> None:
        """Initialize per-instance state (avoid shared mutable class attrs)."""
        self._decision_cache: dict = {}
        self._decision_log: list = []
        os.makedirs(os.path.dirname(self._log_file), exist_ok=True)

    def _get_orderbook(self, pair: str) -> str:
        """Fetch order book from Upbit for bid-ask spread analysis."""
        try:
            # Convert pair format: BTC/KRW -> KRW-BTC
            coin = pair.split("/")[0]
            market = f"KRW-{coin}"

            resp = requests.get(
                f"https://api.upbit.com/v1/orderbook",
                params={"markets": market},
                timeout=5,
            )
            data = resp.json()
            if not data or not isinstance(data, list):
                return "Order book unavailable."

            ob = data[0]
            units = ob.get("orderbook_units", [])
            if not units:
                return "Order book empty."

            # Top 5 bids and asks
            asks = []  # 매도 호가 (낮은 가격부터)
            bids = []  # 매수 호가 (높은 가격부터)

            for u in units[:5]:
                asks.append({"price": u["ask_price"], "size": u["ask_size"]})
                bids.append({"price": u["bid_price"], "size": u["bid_size"]})

            best_ask = asks[0]["price"]
            best_bid = bids[0]["price"]
            spread = (best_ask - best_bid) / best_bid * 100
            total_ask_vol = sum(u["ask_size"] for u in units[:10])
            total_bid_vol = sum(u["bid_size"] for u in units[:10])
            pressure = "Buy pressure" if total_bid_vol > total_ask_vol * 1.2 else \
                       "Sell pressure" if total_ask_vol > total_bid_vol * 1.2 else "Balanced"

            lines = [
                f"## Order Book Analysis",
                f"Best Bid: {best_bid:,.0f} | Best Ask: {best_ask:,.0f} | Spread: {spread:.3f}%",
                f"Bid Volume (top10): {total_bid_vol:,.2f} | Ask Volume (top10): {total_ask_vol:,.2f}",
                f"Order Flow: **{pressure}** (bid/ask ratio: {total_bid_vol/max(total_ask_vol,0.001):.2f})",
                "Top 5 Asks: " + ", ".join(f"{a['price']:,.0f}({a['size']:.3f})" for a in asks),
                "Top 5 Bids: " + ", ".join(f"{b['price']:,.0f}({b['size']:.3f})" for b in bids),
            ]
            return "\n".join(lines)

        except Exception as e:
            logger.warning(f"Order book fetch failed for {pair}: {e}")
            return "Order book fetch failed."

    def _get_ticker(self, pair: str) -> str:
        """Fetch real-time ticker from Upbit for live price action."""
        try:
            coin = pair.split("/")[0]
            market = f"KRW-{coin}"

            resp = requests.get(
                f"https://api.upbit.com/v1/ticker",
                params={"markets": market},
                timeout=5,
            )
            data = resp.json()
            if not data or not isinstance(data, list):
                return "Ticker unavailable."

            t = data[0]
            return f"""## Real-time Ticker
Live Price: {t.get('trade_price', 0):,.0f} KRW
24h High: {t.get('high_price', 0):,.0f} | Low: {t.get('low_price', 0):,.0f}
24h Volume: {t.get('acc_trade_volume_24h', 0):,.2f} {coin}
24h Trade Value: {t.get('acc_trade_price_24h', 0)/1e8:,.1f}억 KRW
Change: {t.get('signed_change_rate', 0)*100:+.2f}% ({t.get('change', 'EVEN')})
52w High: {t.get('highest_52_week_price', 0):,.0f} | Low: {t.get('lowest_52_week_price', 0):,.0f}"""

        except Exception:
            return "Ticker fetch failed."

    def _get_news(self, coin: str, coin_name: str) -> str:
        """Fetch recent news headlines via Tavily."""
        tavily_key = os.environ.get("TAVILY_API_KEY")
        if not tavily_key:
            return "No news available."

        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": tavily_key,
                    "query": f"{coin_name} {coin} crypto price analysis today",
                    "max_results": 5,
                    "search_depth": "basic",
                },
                timeout=10,
            )
            articles = resp.json().get("results", [])
            if not articles:
                return "No recent news found."

            return "\n".join(
                f"- {a.get('title', '')}" for a in articles[:5]
            )
        except Exception:
            return "News fetch failed."

    def _build_market_summary(self, dataframe: DataFrame, pair: str) -> str:
        """Build a text summary of market data for Gemini."""
        if len(dataframe) < 50:
            return "Insufficient data."

        last = dataframe.iloc[-1]
        prev_1h = dataframe.iloc[-12] if len(dataframe) >= 12 else last   # 12 x 5min = 1h
        prev_4h = dataframe.iloc[-48] if len(dataframe) >= 48 else last   # 48 x 5min = 4h
        prev_24h = dataframe.iloc[-288] if len(dataframe) >= 288 else last  # 288 x 5min = 24h

        price = last["close"]
        change_1h = ((price - prev_1h["close"]) / prev_1h["close"] * 100) if prev_1h["close"] > 0 else 0
        change_4h = ((price - prev_4h["close"]) / prev_4h["close"] * 100) if prev_4h["close"] > 0 else 0
        change_24h = ((price - prev_24h["close"]) / prev_24h["close"] * 100) if prev_24h["close"] > 0 else 0

        # Recent price action (last 12 candles = 1 hour)
        recent = dataframe.tail(12)
        high_1h = recent["high"].max()
        low_1h = recent["low"].min()
        vol_avg = dataframe["volume"].tail(60).mean()
        vol_now = last["volume"]

        # 200 EMA 트렌드 필터
        ema200_val = last.get("ema200", 0)
        trend_200 = "Above 200EMA (Bullish)" if price > ema200_val else "Below 200EMA (Bearish)"

        # Stochastic RSI
        stoch_rsi = last.get("stoch_rsi", 50)
        stoch_status = "Oversold" if stoch_rsi < 20 else "Overbought" if stoch_rsi > 80 else "Neutral"

        # 이격도
        disparity = last.get("disparity_20", 0)

        # MACD 크로스 감지
        macd_cross = ""
        if len(dataframe) >= 2:
            prev = dataframe.iloc[-2]
            if prev["macd"] <= prev["macdsignal"] and last["macd"] > last["macdsignal"]:
                macd_cross = "BULLISH CROSS"
                if last["macd"] < 0:
                    macd_cross += " below zero (STRONG BUY - TradingLab)"
            elif prev["macd"] >= prev["macdsignal"] and last["macd"] < last["macdsignal"]:
                macd_cross = "BEARISH CROSS"
                if last["macd"] > 0:
                    macd_cross += " above zero (STRONG SELL - TradingLab)"

        # BNF 시그널
        bnf_signal = ""
        if disparity < -5 and last["rsi"] < 30 and last["macdhist"] > 0:
            bnf_signal = "\n🔥 BNF BUY SIGNAL (급락 후 반전: 이격도 < -5%, RSI 과매도, MACD 반전)"
        elif disparity > 5 and last["rsi"] > 70:
            bnf_signal = "\n⚠️ BNF SELL SIGNAL (과열: 이격도 > +5%, RSI 과매수)"

        # 삼중 확인
        triple = ""
        if stoch_rsi < 20 and last["rsi"] > 50 and "BULLISH" in macd_cross:
            triple = "\n✅ TRIPLE CONFIRM BUY (Stoch 과매도 + RSI 상승추세 + MACD 크로스)"
        elif stoch_rsi > 80 and last["rsi"] < 50 and "BEARISH" in macd_cross:
            triple = "\n✅ TRIPLE CONFIRM SELL (Stoch 과매수 + RSI 하락추세 + MACD 크로스)"

        return f"""## {pair} Market Data
Current Price: {price:,.0f} KRW
Price Change: 1h={change_1h:+.2f}%, 4h={change_4h:+.2f}%, 24h={change_24h:+.2f}%
1h Range: {low_1h:,.0f} ~ {high_1h:,.0f} KRW

## Technical Indicators
- EMA9: {last['ema9']:,.0f} | EMA21: {last['ema21']:,.0f} | EMA50: {last['ema50']:,.0f} | EMA200: {ema200_val:,.0f}
- EMA Trend: {'Bullish (9>21>50)' if last['ema9'] > last['ema21'] > last['ema50'] else 'Bearish' if last['ema9'] < last['ema21'] < last['ema50'] else 'Mixed'}
- 200 EMA Filter: {trend_200}
- RSI(14): {last['rsi']:.1f} {'(Oversold)' if last['rsi'] < 30 else '(Overbought)' if last['rsi'] > 70 else '(Neutral)'}
- Stochastic RSI: {stoch_rsi:.1f} ({stoch_status})
- MACD: {last['macd']:.2f} | Signal: {last['macdsignal']:.2f} | Histogram: {last['macdhist']:.2f}
- MACD Zone: {'Below Zero' if last['macd'] < 0 else 'Above Zero'} | Trend: {'Bullish' if last['macdhist'] > 0 else 'Bearish'}, {'Strengthening' if last['macdhist'] > dataframe.iloc[-2]['macdhist'] else 'Weakening'}
- MACD Cross: {macd_cross if macd_cross else 'None'}
- Disparity Index (20): {disparity:+.2f}% (BNF uses < -5% for buy)
- Bollinger Bands: Lower={last['bb_lower']:,.0f} | Middle={last['bb_middle']:,.0f} | Upper={last['bb_upper']:,.0f}
- Price vs BB: {'Near Upper' if price > last['bb_upper'] * 0.99 else 'Near Lower' if price < last['bb_lower'] * 1.01 else 'Middle'}
- ATR(14): {last['atr']:,.0f} (Volatility: {'High' if last['atr'] > dataframe['atr'].tail(50).mean() * 1.5 else 'Normal'})
- Volume: {vol_now:,.0f} ({'Above' if vol_now > vol_avg else 'Below'} average {vol_avg:,.0f}){bnf_signal}{triple}
{self._compute_extra_signals(dataframe, last, price)}"""

    def _compute_extra_signals(self, dataframe: DataFrame, last, price: float) -> str:
        """래리윌리엄스, 터틀, 평균회귀, 시장상태 분류 시그널 생성"""
        lines = []

        # ─── 래리 윌리엄스 변동성 돌파 (K=0.7) ──────────────
        if len(dataframe) >= 2:
            prev = dataframe.iloc[-2]
            prev_range = prev["high"] - prev["low"]
            k_val = 0.7
            open_price = last["open"]
            breakout = open_price + prev_range * k_val
            breakdown = open_price - prev_range * k_val
            if price >= breakout:
                lines.append(f"🚀 LARRY WILLIAMS BUY (변동성 돌파: {price:,.0f} > {breakout:,.0f})")
            elif price <= breakdown:
                lines.append(f"📉 LARRY WILLIAMS SELL (하방 돌파: {price:,.0f} < {breakdown:,.0f})")

        # ─── 터틀 트레이딩 (20일 돈치안, 5분봉 기준 20일=5760캔들) ──
        lookback_20d = min(5760, len(dataframe) - 1)
        lookback_10d = min(2880, len(dataframe) - 1)
        if lookback_20d >= 2880:
            high_20d = dataframe["high"].tail(lookback_20d).max()
            low_10d = dataframe["low"].tail(lookback_10d).min()
            if price >= high_20d:
                lines.append(f"🐢 TURTLE BUY ({lookback_20d//288}일 최고가 {high_20d:,.0f} 돌파)")
            elif price <= low_10d:
                lines.append(f"🐢 TURTLE EXIT ({lookback_10d//288}일 최저가 {low_10d:,.0f} 이탈)")

        # ─── BB+RSI+ADX 평균회귀 ──────────────────────────
        adx_val = float(last.get("adx", 25.0))
        if np.isnan(adx_val):
            adx_val = 25.0
        if price < last["bb_lower"] and last["rsi"] > 50 and adx_val > 20:
            lines.append(f"📊 MEAN REVERSION BUY (BB하단 이탈 + RSI>50 + ADX={adx_val:.0f})")
        elif price > last["bb_upper"] and last["rsi"] < 50 and adx_val > 20:
            lines.append(f"📊 MEAN REVERSION SELL (BB상단 돌파 + RSI<50 + ADX={adx_val:.0f})")

        # ─── 시장 상태 분류 ──────────────────────────────
        regime = "UNKNOWN"
        ema200_val = last.get("ema200", 0)
        if last.get("ema9", 0) > last.get("ema21", 0) > last.get("ema50", 0) and price > ema200_val and adx_val > 25:
            regime = "STRONG_UPTREND"
        elif last.get("ema9", 0) < last.get("ema21", 0) < last.get("ema50", 0) and price < ema200_val and adx_val > 25:
            regime = "STRONG_DOWNTREND"
        elif adx_val < 20:
            regime = "SIDEWAYS"
        elif price > ema200_val and last.get("ema9", 0) > last.get("ema21", 0):
            regime = "MILD_UPTREND"
        elif price < ema200_val and last.get("ema9", 0) < last.get("ema21", 0):
            regime = "MILD_DOWNTREND"
        else:
            regime = "TRANSITION"

        advice = {
            "STRONG_UPTREND": "강한 상승 → 래리윌리엄스/터틀 트렌드추종, 풀백 매수",
            "MILD_UPTREND": "완만한 상승 → 풀백 매수, 보수적 사이징",
            "SIDEWAYS": "횡보 → 평균회귀(BB반등) 전략, 트렌드추종 중지",
            "MILD_DOWNTREND": "완만한 하락 → 매수 자제, 포지션 축소",
            "STRONG_DOWNTREND": "강한 하락 → BNF 역발상만, 신규매수 금지",
            "TRANSITION": "전환기 → 관망, 방향 확인 후",
        }.get(regime, "")

        result = f"\n## Market Regime: {regime}\n→ {advice}"
        if lines:
            result += "\n\n### ⚡ ACTIVE SIGNALS\n" + "\n".join(lines)

        return result

    def _build_mtf_summary(self, dataframe: DataFrame, pair: str) -> str:
        """Build multi-timeframe analysis from 5m candles."""
        if len(dataframe) < 288:
            return "## Multi-Timeframe\nInsufficient data for MTF analysis."

        lines = ["## Multi-Timeframe Analysis"]

        # 15-minute view (3 candles)
        df_15m = dataframe.iloc[::3].tail(50).copy()
        if len(df_15m) > 14:
            rsi_15m = ta.RSI(df_15m, timeperiod=14).iloc[-1]
            ema9_15m = ta.EMA(df_15m, timeperiod=9).iloc[-1]
            ema21_15m = ta.EMA(df_15m, timeperiod=21).iloc[-1]
            trend_15m = "Bullish" if ema9_15m > ema21_15m else "Bearish"
            lines.append(f"- 15min: RSI={rsi_15m:.1f}, EMA Trend={trend_15m}")

        # 1-hour view (12 candles)
        df_1h = dataframe.iloc[::12].tail(50).copy()
        if len(df_1h) > 14:
            rsi_1h = ta.RSI(df_1h, timeperiod=14).iloc[-1]
            ema9_1h = ta.EMA(df_1h, timeperiod=9).iloc[-1]
            ema21_1h = ta.EMA(df_1h, timeperiod=21).iloc[-1]
            trend_1h = "Bullish" if ema9_1h > ema21_1h else "Bearish"
            lines.append(f"- 1hour: RSI={rsi_1h:.1f}, EMA Trend={trend_1h}")

        # 4-hour view (48 candles)
        df_4h = dataframe.iloc[::48].tail(50).copy()
        if len(df_4h) > 14:
            rsi_4h = ta.RSI(df_4h, timeperiod=14).iloc[-1]
            ema9_4h = ta.EMA(df_4h, timeperiod=9).iloc[-1]
            ema21_4h = ta.EMA(df_4h, timeperiod=21).iloc[-1]
            trend_4h = "Bullish" if ema9_4h > ema21_4h else "Bearish"
            lines.append(f"- 4hour: RSI={rsi_4h:.1f}, EMA Trend={trend_4h}")

        # Alignment check
        trends = [l.split("Trend=")[1] for l in lines[1:] if "Trend=" in l]
        if all(t == "Bullish" for t in trends):
            lines.append("- **ALL TIMEFRAMES BULLISH** — strong buy zone")
        elif all(t == "Bearish" for t in trends):
            lines.append("- **ALL TIMEFRAMES BEARISH** — strong sell zone")
        else:
            lines.append("- Mixed signals across timeframes — use caution")

        return "\n".join(lines)

    def _get_recent_decisions(self, pair: str, limit: int = 10) -> str:
        """Get recent decisions for this pair from log file, including trade outcomes."""
        try:
            recent = []
            with open(self._log_file) as f:
                for line in f:
                    entry = json.loads(line.strip())
                    if entry.get("pair") == pair:
                        recent.append(entry)
            recent = recent[-limit:]
            if not recent:
                return "No previous decisions."

            lines = []
            for r in recent:
                ts = r.get("timestamp", "?")[:16]
                act = r.get("action", "?")
                conf = r.get("confidence", 0)
                reason = r.get("reason", "")
                price = r.get("price", 0)
                outcome = r.get("outcome", "")
                outcome_str = f" → {outcome}" if outcome else ""
                lines.append(f"  [{ts}] {act}({conf:.1f}) @{price:,.0f}{outcome_str} - {reason}")
            return "\n".join(lines)
        except Exception:
            return "No history available."

    def _get_trade_outcomes(self, pair: str) -> str:
        """Get closed trade results to feed back for learning."""
        try:
            resp = requests.get(
                "http://127.0.0.1:8080/api/v1/trades",
                auth=("freqtrade", "freqtrade"),
                params={"limit": 50},
                timeout=5,
            )
            trades = resp.json().get("trades", [])
            closed = [t for t in trades if not t["is_open"] and t["pair"] == pair]
            if not closed:
                return "No closed trades for this pair."

            closed = closed[-10:]  # last 10
            lines = []
            wins = sum(1 for t in closed if t.get("profit_pct", 0) >= 0)
            losses = len(closed) - wins
            lines.append(f"Win/Loss: {wins}W {losses}L ({wins/(wins+losses)*100:.0f}% winrate)")
            for t in closed[-5:]:
                p = t.get("profit_pct", 0)
                reason = t.get("exit_reason", "?")
                dur = t.get("trade_duration", 0)
                emoji = "WIN" if p >= 0 else "LOSS"
                lines.append(f"  {emoji}: {p:+.2f}% | exit={reason} | held={dur}min")
            return "\n".join(lines)
        except Exception:
            return "Trade history unavailable."

    def _rotate_log_if_large(self):
        """JSONL이 _log_max_mb 초과 시 .1/.2/.3 으로 로테이션 (최대 3개 보관)."""
        try:
            path = self._log_file
            if not os.path.exists(path):
                return
            if os.path.getsize(path) < self._log_max_mb * 1024 * 1024:
                return
            for i in range(2, 0, -1):
                src = f"{path}.{i}"
                dst = f"{path}.{i + 1}"
                if os.path.exists(src):
                    os.rename(src, dst)
            os.rename(path, f"{path}.1")
            logger.info(f"Rotated log: {path} → {path}.1 (max {self._log_max_mb}MB)")
        except Exception as e:
            logger.warning(f"Log rotation failed: {e}")

    def _log_decision(self, pair: str, decision: dict, price: float, indicators: dict):
        """Log decision to JSONL file for learning.

        에러 fallback 결정(error 필드 있음)은 학습 로그를 오염시키지 않도록 스킵.
        """
        if decision.get("error"):
            return
        try:
            self._rotate_log_if_large()
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "pair": pair,
                "action": decision["action"],
                "confidence": decision["confidence"],
                "reason": decision["reason"],
                "price": price,
                "rsi": indicators.get("rsi", 0),
                "macd_hist": indicators.get("macdhist", 0),
                "ema_trend": indicators.get("ema_trend", ""),
                "volume_ratio": indicators.get("volume_ratio", 0),
                "input_tokens": indicators.get("input_tokens", 0),
                "thinking_tokens": indicators.get("thinking_tokens", 0),
                "output_tokens": indicators.get("output_tokens", 0),
            }
            with open(self._log_file, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _get_gemini_decision(self, pair: str, dataframe: DataFrame) -> dict:
        """
        Ask Gemini for a trading decision with deep analysis.
        Returns: {"action": "buy"|"sell"|"hold", "confidence": 0.0-1.0, "reason": "..."}
        """
        now = datetime.now(timezone.utc).timestamp()
        cached = self._decision_cache.get(pair)
        if cached and (now - cached["ts"]) < self._decision_ttl:
            return cached["decision"]

        gemini_key = os.environ.get("GEMINI_API_KEY")
        if not gemini_key:
            # 실제 hold 결정과 구분되도록 error 마킹 — populate_entry_trend에서 진입 차단됨
            return {"action": "hold", "confidence": 0, "reason": "ERROR: No GEMINI_API_KEY env var", "error": "no_api_key"}

        coin = pair.split("/")[0]
        coin_name = self.COIN_NAMES.get(coin, coin)

        # Build context
        market_summary = self._build_market_summary(dataframe, pair)
        news = self._get_news(coin, coin_name)
        past_decisions = self._get_recent_decisions(pair)
        trade_outcomes = self._get_trade_outcomes(pair)
        orderbook = self._get_orderbook(pair)
        ticker = self._get_ticker(pair)

        # Multi-timeframe summary
        mtf_summary = self._build_mtf_summary(dataframe, pair)

        # ML Signal Analysis
        ml_section = ""
        if _ML_ENGINE is not None:
            try:
                # DataFrame → candle dicts 변환 (최근 60개)
                tail = dataframe.tail(60)
                candles = [
                    {
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": float(row["volume"]),
                    }
                    for _, row in tail.iterrows()
                    if not (np.isnan(row["close"]) or row["close"] == 0)
                ]
                if len(candles) >= 30:
                    ml_signals = _ML_ENGINE.get_signals(candles)
                    ml_section = "\n" + _ML_ENGINE.format_for_llm_prompt(ml_signals) + "\n"
                    logger.info(f"ML signal {pair}: {ml_signals['consensus'].get('action','?')} "
                                f"(conf={ml_signals['consensus'].get('confidence',0):.2f})")
            except Exception as ml_err:
                logger.warning(f"ML signal failed for {pair}: {ml_err}")

        prompt = f"""You are an aggressive short-term crypto trader managing a KRW 1,000,000 paper trading portfolio.
This is PAPER TRADING — there is ZERO real money risk. Be BOLD and trade actively to gather data and learn.

{market_summary}

{ticker}

{orderbook}

{mtf_summary}
{ml_section}
## Recent News Headlines
{news}

## Your Previous Decisions for {pair}
{past_decisions}

## Actual Trade Results for {pair}
{trade_outcomes}
IMPORTANT: Analyze your wins and losses above. Learn from what worked and what didn't.
CRITICAL LEARNING (48 trades analyzed):
- Win rate only 20.8% — trailing_stop_loss killing 23 trades at avg -0.21%
- Problem: entering too early or selling too fast. Stops are too tight.
- When you BUY: Only buy when you see STRONG momentum, not just a mild signal.
- When you SELL: Do NOT issue sell signals for small dips (<1%). Let winners run.
- Hold time should be LONGER. In uptrends, patience = profit.
- If coin is in uptrend (EMA9 > EMA21 > EMA50), do NOT sell on minor pullbacks.

## Portfolio Context
- Paper trading with KRW 50,000 per trade — NO REAL RISK
- Max 5 concurrent positions across 8 coins
- Hold time target: 30min to 8 hours (was too short before)
- Goal: ACTIVELY TRADE but LET WINNERS RUN. Don't sell too early.
- Trailing stop at 1% after 2% profit — WIDER than before

## PROVEN STRATEGY RULES (backtested, YouTube verified)
### Rule 1 — 200 EMA Trend Filter
- ONLY BUY if price ABOVE 200 EMA. Never trade against the 200 EMA trend.
### Rule 2 — MACD Cross Below Zero = Strong Buy (TradingLab, 86% win rate)
- MACD bullish cross BELOW zero line = strongest buy. Above zero = weak.
### Rule 3 — BNF Mean Reversion (¥2M → ¥40B)
- Disparity < -5% + RSI oversold + MACD histogram green → STRONG BUY
### Rule 4 — Triple Confirmation (Stochastic + RSI + MACD)
- All 3 must agree for high confidence entry.
### Rule 5 — Pullback Entry (Trade Pro)
- Enter at 20 EMA pullback, not when price is extended.
### Rule 6 — Larry Williams Volatility Breakout (K=0.7, bear market 58% return)
- Price > Open + PrevRange×0.7 → BUY. Sell at next candle open.
### Rule 7 — Turtle Trading (Donchian Channel, 12636% historical)
- 20-day high breakout → BUY. 10-day low break → EXIT. Requires strong trend (ADX>25).
### Rule 8 — BB+RSI+ADX Mean Reversion (179% backtest)
- Price < lower BB + RSI > 50 + ADX > 20 → oversold bounce BUY.
- Price > upper BB + RSI < 50 + ADX > 20 → overbought SELL.
### Rule 9 — Market Regime Selection
- STRONG_UPTREND: Larry Williams + Turtle + pullback buy
- MILD_UPTREND: Conservative pullback, smaller position
- SIDEWAYS: Mean Reversion (BB bounce) ONLY, NO trend following
- MILD_DOWNTREND: Reduce position, sell rallies
- STRONG_DOWNTREND: BNF contrarian only, NO new longs
- TRANSITION: Wait for direction confirmation

## Decision Framework (10 STEPS)
1. MARKET REGIME: Check Market Regime classification first. Select strategy accordingly.
2. TREND: Check 200 EMA. Don't trade against it in trending regimes.
3. ACTIVE SIGNALS: Check ⚡ ACTIVE SIGNALS section. Multiple signals = high conviction.
4. SPECIAL SIGNALS: BNF, Larry Williams, Turtle, Mean Reversion = high priority.
5. ML SIGNALS: XGBoost/LSTM/RL consensus. ML agrees with rules → boost confidence.
6. MACD QUALITY: Cross below zero = strong. Cross near zero = weak.
7. MOMENTUM: Multi-timeframe alignment check.
8. NEWS: Headlines impact in next 1-4 hours?
9. SELF-REVIEW: Learn from actual trade results above.
10. RISK & SIZING: Paper trading. 0.5+ confidence if rules align. Stake 0.5x-2.0x.

## Confidence Guide (AGGRESSIVE)
- 0.8-1.0: Very strong signal → MUST act
- 0.6-0.7: Good signal → ACT (this is paper trading!)
- 0.4-0.5: Moderate signal → lean toward acting
- 0.2-0.3: Weak signal → hold
- 0.0-0.1: No signal

## IMPORTANT: Do NOT default to "hold" out of caution. This is paper trading.
If you see ANY reasonable setup (RSI oversold bounce, EMA cross, BB squeeze breakout, volume spike),
assign confidence 0.5+ and recommend action. We want MORE trades, not fewer.

Respond JSON: {{"action": "buy" or "sell" or "hold", "confidence": 0.0-1.0, "reason": "<detailed 30 word analysis>", "risk_level": "low/medium/high", "expected_move": "<predicted % move in next 1-4h>", "stake_multiplier": 0.5-2.0}}"""

        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_key}",
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

            data = resp.json()
            content = data["candidates"][0]["content"]["parts"][0]["text"].strip()

            # Parse JSON
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
                "expected_move": result.get("expected_move", "0%"),
                "stake_multiplier": max(0.5, min(2.0, float(result.get("stake_multiplier", 1.0)))),
            }

            # Get usage info
            usage = data.get("usageMetadata", {})
            thinking_tokens = usage.get("thoughtsTokenCount", 0)

            logger.info(
                f"Gemini {pair}: {decision['action']} "
                f"(conf={decision['confidence']:.2f}, risk={decision['risk_level']}, "
                f"move={decision['expected_move']}, think={thinking_tokens}tok) "
                f"{decision['reason']}"
            )

            # Log for learning (include token usage for cost tracking)
            input_tokens = usage.get("promptTokenCount", 0)
            output_tokens = usage.get("candidatesTokenCount", 0)
            last = dataframe.iloc[-1] if len(dataframe) > 0 else {}
            self._log_decision(pair, decision, float(last.get("close", 0)), {
                "rsi": float(last.get("rsi", 0)),
                "macdhist": float(last.get("macdhist", 0)),
                "ema_trend": "bull" if last.get("ema9", 0) > last.get("ema21", 0) else "bear",
                "volume_ratio": float(last.get("volume", 0)) / max(float(last.get("volume_sma20", 1)), 1),
                "input_tokens": input_tokens,
                "thinking_tokens": thinking_tokens,
                "output_tokens": output_tokens,
            })

            self._decision_cache[pair] = {"decision": decision, "ts": now}
            return decision

        except requests.Timeout:
            logger.warning(f"Gemini timeout for {pair}")
            return {"action": "hold", "confidence": 0, "reason": "ERROR: Gemini API timeout", "error": "timeout"}
        except requests.RequestException as e:
            logger.warning(f"Gemini network error for {pair}: {e}")
            return {"action": "hold", "confidence": 0, "reason": f"ERROR: Network ({type(e).__name__})", "error": "network"}
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            logger.warning(f"Gemini response parse error for {pair}: {e}")
            return {"action": "hold", "confidence": 0, "reason": f"ERROR: Parse ({type(e).__name__})", "error": "parse"}
        except Exception as e:
            logger.warning(f"Gemini decision failed for {pair}: {e}")
            return {"action": "hold", "confidence": 0, "reason": f"ERROR: {type(e).__name__}: {e}", "error": "unknown"}

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # EMAs
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)

        # RSI
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        # Stochastic RSI (Data Trader 전략)
        dataframe["stoch_rsi"] = (dataframe["rsi"] - dataframe["rsi"].rolling(14).min()) / \
            (dataframe["rsi"].rolling(14).max() - dataframe["rsi"].rolling(14).min()) * 100
        dataframe["stoch_rsi"] = dataframe["stoch_rsi"].fillna(50)

        # MACD
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]

        # Bollinger Bands
        bollinger = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        dataframe["bb_upper"] = bollinger["upperband"]
        dataframe["bb_lower"] = bollinger["lowerband"]
        dataframe["bb_middle"] = bollinger["middleband"]

        # ATR
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)

        # ADX (Average Directional Index — 추세 강도)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)

        # Volume
        dataframe["volume_sma20"] = dataframe["volume"].rolling(window=20).mean()

        # 이격도 Disparity Index (BNF 전략)
        sma20 = dataframe["close"].rolling(20).mean()
        dataframe["disparity_20"] = ((dataframe["close"] - sma20) / sma20) * 100

        # Get Gemini decision (cached per pair for 1 hour)
        decision = self._get_gemini_decision(metadata["pair"], dataframe)
        dataframe["ai_action"] = decision["action"]
        dataframe["ai_confidence"] = decision["confidence"]

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        action = dataframe["ai_action"].iloc[-1] if len(dataframe) > 0 else "hold"
        confidence = dataframe["ai_confidence"].iloc[-1] if len(dataframe) > 0 else 0

        if action == "buy" and confidence >= 0.6:
            # Good+ confidence: AI 판단만으로 진입 (paper trading = aggressive)
            dataframe.loc[
                (dataframe["volume"] > 0),
                "enter_long",
            ] = 1
        elif action == "buy" and confidence >= 0.4:
            # Moderate confidence: AI + 최소 기술지표 확인
            dataframe.loc[
                (
                    (dataframe["rsi"] < 65)
                    & (dataframe["volume"] > 0)
                ),
                "enter_long",
            ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        action = dataframe["ai_action"].iloc[-1] if len(dataframe) > 0 else "hold"
        confidence = dataframe["ai_confidence"].iloc[-1] if len(dataframe) > 0 else 0

        if action == "sell" and confidence >= 0.6:
            # Good+ confidence sell: AI만으로 청산
            dataframe.loc[
                (dataframe["volume"] > 0),
                "exit_long",
            ] = 1
        elif action == "sell" and confidence >= 0.4:
            # Moderate confidence: sell + 최소 확인
            dataframe.loc[
                (
                    (dataframe["rsi"] > 60)
                    | (dataframe["ema9"] < dataframe["ema21"])
                )
                & (dataframe["volume"] > 0),
                "exit_long",
            ] = 1

        return dataframe

    def custom_stoploss(self, pair: str, trade, current_time, current_rate,
                        current_profit, after_fill, **kwargs) -> float:
        """ATR-based dynamic stoploss."""
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if len(dataframe) < 1:
            return self.stoploss

        last = dataframe.iloc[-1]
        atr = last["atr"]
        confidence = last.get("ai_confidence", 0)

        if atr > 0 and current_rate > 0:
            # Higher confidence = wider stop (more room to breathe)
            # 기존: 2.0~3.0x → 변동에 너무 빨리 잘림 (승률 20%)
            # 수정: 4.0~6.0x → 충분한 호흡 공간 확보
            multiplier = 6.0 if confidence >= 0.7 else 5.0 if confidence >= 0.5 else 4.0
            atr_stoploss = -(atr * multiplier) / current_rate
            return max(atr_stoploss, -0.15)

        return self.stoploss

    use_custom_stoploss = True

    def custom_stake_amount(self, pair: str, current_time, current_rate,
                            proposed_stake, min_stake, max_stake,
                            leverage, entry_tag, side, **kwargs) -> float:
        """AI가 제안한 stake_multiplier로 포지션 크기 조절."""
        cached = self._decision_cache.get(pair, {})
        decision = cached.get("decision", {})
        multiplier = decision.get("stake_multiplier", 1.0)

        # Clamp multiplier between 0.5 and 2.0
        multiplier = max(0.5, min(2.0, float(multiplier)))

        adjusted = proposed_stake * multiplier
        adjusted = max(min_stake, min(adjusted, max_stake))

        if multiplier != 1.0:
            logger.info(f"Stake {pair}: {proposed_stake:,.0f} x {multiplier:.1f} = {adjusted:,.0f} KRW")

        return adjusted
