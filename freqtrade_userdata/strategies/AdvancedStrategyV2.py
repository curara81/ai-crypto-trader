from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter, RealParameter
from pandas import DataFrame
import talib.abstract as ta
import numpy as np


class AdvancedStrategyV2(IStrategy):
    """
    V2: Relaxed entry conditions + Hyperopt-tunable trailing stop.
    """

    INTERFACE_VERSION = 3

    minimal_roi = {
        "0": 0.04,
        "30": 0.025,
        "60": 0.015,
        "120": 0.008
    }

    stoploss = -0.05
    trailing_stop = True
    trailing_stop_positive = 0.008
    trailing_stop_positive_offset = 0.02
    trailing_only_offset_is_reached = True

    timeframe = "15m"

    buy_rsi = IntParameter(25, 50, default=40, space="buy")
    sell_rsi = IntParameter(60, 85, default=72, space="sell")

    trailing_stop_positive_p = DecimalParameter(0.005, 0.03, default=0.008, decimals=3, space="sell", optimize=True)
    trailing_stop_positive_offset_p = DecimalParameter(0.01, 0.06, default=0.02, decimals=3, space="sell", optimize=True)
    stoploss_p = DecimalParameter(-0.08, -0.03, default=-0.05, decimals=2, space="sell", optimize=True)
    atr_multiplier = DecimalParameter(1.5, 3.5, default=2.5, decimals=1, space="sell", optimize=True)

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False
    }

    startup_candle_count = 200

    def bot_start(self, **kwargs) -> None:
        self.stoploss = self.stoploss_p.value
        self.trailing_stop_positive = self.trailing_stop_positive_p.value
        self.trailing_stop_positive_offset = self.trailing_stop_positive_offset_p.value

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Bollinger Bands
        bollinger = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
        dataframe["bb_upper"] = bollinger["upperband"]
        dataframe["bb_middle"] = bollinger["middleband"]
        dataframe["bb_lower"] = bollinger["lowerband"]
        dataframe["bb_width"] = (
            (dataframe["bb_upper"] - dataframe["bb_lower"]) / dataframe["bb_middle"]
        )
        dataframe["bb_pct"] = (
            (dataframe["close"] - dataframe["bb_lower"])
            / (dataframe["bb_upper"] - dataframe["bb_lower"])
        )

        # MACD
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        dataframe["macdhist"] = macd["macdhist"]

        # RSI
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        # EMAs
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)

        # ATR
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)

        # Volume
        dataframe["volume_sma20"] = dataframe["volume"].rolling(window=20).mean()
        dataframe["volume_ratio"] = dataframe["volume"] / dataframe["volume_sma20"]

        # MFI (Money Flow Index)
        dataframe["mfi"] = ta.MFI(dataframe, timeperiod=14)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                # Signal A: Bollinger Band bounce
                (
                    (dataframe["bb_pct"] < 0.15)
                    & (dataframe["rsi"] < self.buy_rsi.value)
                    & (dataframe["macdhist"] > dataframe["macdhist"].shift(1))
                )
                # Signal B: EMA golden cross with momentum
                | (
                    (dataframe["ema9"] > dataframe["ema21"])
                    & (dataframe["ema9"].shift(1) <= dataframe["ema21"].shift(1))
                    & (dataframe["rsi"] < 55)
                    & (dataframe["macd"] > dataframe["macdsignal"])
                )
                # Signal C: RSI divergence recovery
                | (
                    (dataframe["rsi"] < 30)
                    & (dataframe["rsi"] > dataframe["rsi"].shift(1))
                    & (dataframe["close"] > dataframe["close"].shift(1))
                    & (dataframe["mfi"] < 30)
                )
            )
            & (dataframe["volume_ratio"] > 0.8)
            & (dataframe["close"] > dataframe["ema50"])
            & (dataframe["volume"] > 0),
            "enter_long",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                # Overbought at upper BB
                (
                    (dataframe["bb_pct"] > 0.95)
                    & (dataframe["rsi"] > self.sell_rsi.value)
                )
                # OR MACD death cross
                | (
                    (dataframe["macd"] < dataframe["macdsignal"])
                    & (dataframe["macd"].shift(1) >= dataframe["macdsignal"].shift(1))
                    & (dataframe["rsi"] > 55)
                )
                # OR EMA death cross
                | (
                    (dataframe["ema9"] < dataframe["ema21"])
                    & (dataframe["ema9"].shift(1) >= dataframe["ema21"].shift(1))
                    & (dataframe["rsi"] > 50)
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
            atr_stoploss = -(atr * self.atr_multiplier.value) / current_rate
            return max(atr_stoploss, -0.08)

        return self.stoploss

    use_custom_stoploss = True
