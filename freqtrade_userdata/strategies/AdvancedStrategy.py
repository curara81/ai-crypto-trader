from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter
from pandas import DataFrame
import talib.abstract as ta
import numpy as np


class AdvancedStrategy(IStrategy):
    """
    Multi-indicator strategy combining:
    - Bollinger Bands (mean reversion)
    - MACD (trend confirmation)
    - RSI (momentum filter)
    - Volume spike detection
    - ATR-based dynamic stoploss
    """

    INTERFACE_VERSION = 3

    minimal_roi = {
        "0": 0.05,
        "30": 0.03,
        "60": 0.02,
        "120": 0.01
    }

    stoploss = -0.07
    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.025
    trailing_only_offset_is_reached = True

    timeframe = "15m"

    # Hyperopt parameters
    buy_rsi = IntParameter(20, 40, default=30, space="buy")
    buy_bb_width_min = DecimalParameter(0.01, 0.05, default=0.02, space="buy")
    sell_rsi = IntParameter(65, 85, default=75, space="sell")

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Bollinger Bands
        bollinger = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        dataframe["bb_upper"] = bollinger["upperband"]
        dataframe["bb_middle"] = bollinger["middleband"]
        dataframe["bb_lower"] = bollinger["lowerband"]
        dataframe["bb_width"] = (
            (dataframe["bb_upper"] - dataframe["bb_lower"]) / dataframe["bb_middle"]
        )

        # MACD
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]

        # RSI
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        # Moving Averages
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["sma50"] = ta.SMA(dataframe, timeperiod=50)
        dataframe["sma200"] = ta.SMA(dataframe, timeperiod=200)

        # ATR for volatility
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)

        # Volume analysis
        dataframe["volume_sma20"] = dataframe["volume"].rolling(window=20).mean()
        dataframe["volume_ratio"] = dataframe["volume"] / dataframe["volume_sma20"]

        # Stochastic RSI
        stoch = ta.STOCHRSI(dataframe, timeperiod=14)
        dataframe["stochrsi_k"] = stoch["fastk"]
        dataframe["stochrsi_d"] = stoch["fastd"]

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                # Price near or below lower Bollinger Band (oversold zone)
                (dataframe["close"] <= dataframe["bb_lower"] * 1.01)
                # RSI confirms oversold
                & (dataframe["rsi"] < self.buy_rsi.value)
                # MACD histogram turning positive (momentum shift)
                & (dataframe["macdhist"] > dataframe["macdhist"].shift(1))
                # Bollinger Band width above minimum (avoid squeeze/no-volatility)
                & (dataframe["bb_width"] > self.buy_bb_width_min.value)
                # Volume spike confirms interest
                & (dataframe["volume_ratio"] > 1.2)
                # Above 200 SMA (long-term uptrend filter)
                & (dataframe["close"] > dataframe["sma200"])
                # Basic volume check
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                # Price at or above upper Bollinger Band (overbought zone)
                (
                    (dataframe["close"] >= dataframe["bb_upper"] * 0.99)
                    & (dataframe["rsi"] > self.sell_rsi.value)
                )
                # OR MACD death cross
                | (
                    (dataframe["macd"] < dataframe["macdsignal"])
                    & (dataframe["macd"].shift(1) >= dataframe["macdsignal"].shift(1))
                    & (dataframe["rsi"] > 60)
                )
                # OR EMA death cross with high RSI
                | (
                    (dataframe["ema9"] < dataframe["ema21"])
                    & (dataframe["ema9"].shift(1) >= dataframe["ema21"].shift(1))
                    & (dataframe["rsi"] > 55)
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

        if atr > 0 and current_rate > 0:
            atr_stoploss = -(atr * 2) / current_rate
            return max(atr_stoploss, -0.10)

        return self.stoploss

    use_custom_stoploss = True
