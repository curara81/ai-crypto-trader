import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from freqtrade.strategy import IStrategy, IntParameter
from pandas import DataFrame
import talib.abstract as ta

logger = logging.getLogger(__name__)


class LLMSentimentStrategy(IStrategy):
    """
    LLM-enhanced strategy that combines:
    - Technical analysis (EMA, RSI, MACD)
    - News sentiment via web search API
    - Claude/OpenAI LLM for sentiment scoring

    Requires env vars:
    - ANTHROPIC_API_KEY or OPENAI_API_KEY
    - TAVILY_API_KEY (for news search, free tier: 1000 req/month)

    Sentiment is cached per pair for 30 minutes to avoid API spam.
    """

    INTERFACE_VERSION = 3

    minimal_roi = {
        "0": 0.10,       # 즉시: 10% 이상이면 익절
        "120": 0.05,     # 2시간: 5%
        "360": 0.025,    # 6시간: 2.5%
        "720": 0.01      # 12시간: 1%
    }

    stoploss = -0.08
    trailing_stop = True
    trailing_stop_positive = 0.03    # 3% 트레일링 (기존 1.5%)
    trailing_stop_positive_offset = 0.04  # 4% 수익 후 활성 (기존 3%)
    trailing_only_offset_is_reached = True

    timeframe = "5m"
    process_only_new_candles = True

    buy_rsi = IntParameter(30, 60, default=55, space="buy")
    sell_rsi = IntParameter(60, 80, default=72, space="sell")

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False
    }

    _sentiment_cache: dict = {}
    _sentiment_ttl = 14400  # 4 hour cache (fits Tavily free tier: ~900 calls/month)

    def _get_sentiment(self, pair: str) -> Optional[float]:
        """
        Get sentiment score for a pair.
        Returns float between -1.0 (very bearish) and 1.0 (very bullish).
        Returns None if APIs unavailable.
        """
        now = datetime.now(timezone.utc).timestamp()
        cached = self._sentiment_cache.get(pair)
        if cached and (now - cached["ts"]) < self._sentiment_ttl:
            return cached["score"]

        try:
            from secrets_helper import get_secret as _gs
        except ImportError:
            _gs = os.environ.get
        tavily_key = _gs("TAVILY_API_KEY")
        anthropic_key = _gs("ANTHROPIC_API_KEY")

        if not tavily_key or not anthropic_key:
            return None

        coin = pair.split("/")[0]
        coin_names = {
            "BTC": "Bitcoin", "ETH": "Ethereum", "XRP": "Ripple",
            "SOL": "Solana", "ADA": "Cardano", "DOGE": "Dogecoin",
        }
        coin_name = coin_names.get(coin, coin)

        try:
            search_resp = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": tavily_key,
                    "query": f"{coin_name} {coin} crypto price analysis today",
                    "max_results": 5,
                    "search_depth": "basic",
                    "include_answer": False,
                },
                timeout=10,
            )
            search_data = search_resp.json()
            articles = search_data.get("results", [])

            if not articles:
                return None

            news_text = "\n".join(
                f"- {a.get('title', '')}: {a.get('content', '')[:200]}"
                for a in articles[:5]
            )

            llm_resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 50,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                f"Analyze these news headlines about {coin_name} ({coin}) "
                                f"and respond with ONLY a JSON object: "
                                f'{{"score": <float between -1.0 and 1.0>, "reason": "<10 words max>"}}\n\n'
                                f"Score guide: -1.0=very bearish, 0=neutral, 1.0=very bullish\n\n"
                                f"Headlines:\n{news_text}"
                            ),
                        }
                    ],
                },
                timeout=15,
            )

            llm_data = llm_resp.json()
            content = llm_data["content"][0]["text"].strip()

            if content.startswith("{"):
                result = json.loads(content)
                score = float(result["score"])
                score = max(-1.0, min(1.0, score))
                reason = result.get("reason", "")
                logger.info(f"Sentiment {pair}: {score} ({reason})")

                self._sentiment_cache[pair] = {"score": score, "ts": now}
                return score

        except Exception as e:
            logger.warning(f"Sentiment fetch failed for {pair}: {e}")

        return None

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # EMAs
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["sma200"] = ta.SMA(dataframe, timeperiod=200)

        # RSI
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

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

        # Volume
        dataframe["volume_sma20"] = dataframe["volume"].rolling(window=20).mean()
        dataframe["volume_ratio"] = dataframe["volume"] / dataframe["volume_sma20"]

        # Sentiment (fetched once per pair per 30 min)
        sentiment = self._get_sentiment(metadata["pair"])
        dataframe["sentiment"] = sentiment if sentiment is not None else 0.0

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        sentiment = dataframe["sentiment"].iloc[-1] if len(dataframe) > 0 else 0.0

        # Sentiment-adjusted RSI threshold
        rsi_threshold = self.buy_rsi.value
        if sentiment > 0.3:
            rsi_threshold += 5  # more lenient when bullish
        elif sentiment < -0.3:
            rsi_threshold -= 5  # stricter when bearish

        dataframe.loc[
            (
                # EMA short-term uptrend
                (dataframe["ema9"] > dataframe["ema21"])
                # RSI not overbought (sentiment-adjusted)
                & (dataframe["rsi"] < rsi_threshold)
                # Minimal volume filter
                & (dataframe["volume_ratio"] > 0.4)
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        sentiment = dataframe["sentiment"].iloc[-1] if len(dataframe) > 0 else 0.0

        sell_rsi_threshold = self.sell_rsi.value
        if sentiment < -0.3:
            sell_rsi_threshold -= 5  # exit earlier when bearish

        dataframe.loc[
            (
                # RSI overbought
                (dataframe["rsi"] > sell_rsi_threshold)
                # OR strong EMA death cross with momentum confirm
                | (
                    (dataframe["ema9"] < dataframe["ema21"])
                    & (dataframe["macdhist"] < 0)
                    & (dataframe["macdhist"] < dataframe["macdhist"].shift(1))
                )
            )
            & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1

        return dataframe

    def custom_stoploss(self, pair: str, trade, current_time, current_rate,
                        current_profit, after_fill, **kwargs) -> float:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if len(dataframe) < 1:
            return self.stoploss

        last_candle = dataframe.iloc[-1]
        atr = last_candle["atr"]
        sentiment = last_candle.get("sentiment", 0.0)

        if atr > 0 and current_rate > 0:
            multiplier = 2.5 if sentiment > 0.3 else 2.0 if sentiment > -0.3 else 1.5
            atr_stoploss = -(atr * multiplier) / current_rate
            return max(atr_stoploss, -0.10)

        return self.stoploss

    use_custom_stoploss = True
