from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class SimpleStrategy(IStrategy):
    """
    Simple SMA crossover + RSI strategy for dry-run testing.
    - Buy: SMA 20 crosses above SMA 50 AND RSI < 40
    - Sell: SMA 20 crosses below SMA 50 OR RSI > 70
    """

    INTERFACE_VERSION = 3

    minimal_roi = {
        "60": 0.01,   # 1% after 60 min
        "120": 0.005, # 0.5% after 120 min
        "0": 0.03     # 3% immediate target
    }

    stoploss = -0.05  # 5% stop loss

    timeframe = "5m"

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "stoploss": "market",
        "stoploss_on_exchange": False
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["sma20"] = ta.SMA(dataframe, timeperiod=20)
        dataframe["sma50"] = ta.SMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["sma20"] > dataframe["sma50"])
                & (dataframe["sma20"].shift(1) <= dataframe["sma50"].shift(1))
                & (dataframe["rsi"] < 40)
                & (dataframe["volume"] > 0)
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["sma20"] < dataframe["sma50"])
                | (dataframe["rsi"] > 70)
            )
            & (dataframe["volume"] > 0),
            "exit_long",
        ] = 1
        return dataframe
