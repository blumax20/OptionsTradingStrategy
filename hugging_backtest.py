"""
Hugging strategy backtest script using Backtrader.

This script demonstrates how to evaluate the adaptive hugging debit spread
strategy on one or more stocks loaded from CSV files.  The strategy
approximates the Pine Script logic found in the generated hugging debit
strategy by translating it into Backtrader constructs.  Key features include:

  • Adaptive thresholding based on volatility using Bollinger Bands.
  • Stochastic RSI and Money Flow Index scoring to identify oversold/overbought
    conditions.
  • Trend filtering via fast/slow exponential moving averages and the ADX.
  • Hugging logic that flips from long to short (or vice versa) when price
    hugs a Bollinger band for a user‑defined number of bars and the trend
    reverses.
  • Profit targets, stop losses and time‑based exits controlled by ATR
    multipliers and exit bar limits.

Usage example::

    python hugging_backtest.py --data "BATS_FL, 1D.csv" "BATS_AMD, 1D.csv" --cash 100000

This will backtest the hugging strategy on the specified CSV files and
report the final portfolio value for each run.  The CSV files must contain
at least the columns 'time', 'open', 'high', 'low', 'close' and 'volume'.

Note: Backtrader is not included in this environment by default.  Install
it locally via `pip install backtrader` before running the script.

"""

import argparse
from datetime import datetime
from typing import List

import pandas as pd

try:
    import backtrader as bt  # type: ignore
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "Backtrader is required to run this script. Install it with `pip install backtrader`."
    )


class HuggingStrategy(bt.Strategy):
    """Adaptive hugging debit spread strategy implemented for Backtrader.

    The parameters default to the values used in the supplied Pine Script.  They
    may be overridden via the strategy params mechanism if desired.
    """

    params = (
        ('K_OVERSOLD', 20),
        ('K_OVERBOUGHT', 80),
        ('MFI_OVERSOLD', 20),
        ('MFI_OVERBOUGHT', 80),
        ('EXIT_BARS', 7),
        ('MIN_SCORE', 2),
        ('profitFactor', 2.0),
        ('stopFactor', 2.0),
        ('hugBars', 5),
        ('hugPct', 0.1),
        ('stochLength', 14),
        ('stochSmooth', 3),
        ('mfiLength', 14),
        ('bbLength', 15),
        ('bbStdDev', 2.0),
        ('macdFast', 12),
        ('macdSlow', 26),
        ('macdSignal', 9),
        ('atrLength', 14),
        ('adxLength', 14),
        ('adxThreshold', 40.0),
    )

    def __init__(self) -> None:
        # Bollinger Bands and width measures
        self.bb = bt.indicators.BollingerBands(
            self.data.close, period=self.p.bbLength, devfactor=self.p.bbStdDev
        )
        self.basis = self.bb.mid
        self.upper = self.bb.top
        self.lower = self.bb.bot
        # Normalised band width: (upper - lower) / basis
        self.bbWidth = (self.upper - self.lower) / self.basis
        # Moving average of width to classify volatility regimes
        self.bbWidthMA = bt.indicators.MovingAverageSimple(self.bbWidth, period=20)

        # RSI and Stochastic RSI
        rsi = bt.indicators.RSI(self.data.close, period=self.p.stochLength)
        rsi_high = bt.indicators.Highest(rsi, period=self.p.stochLength)
        rsi_low = bt.indicators.Lowest(rsi, period=self.p.stochLength)
        stoch_rsi = 100.0 * (rsi - rsi_low) / (rsi_high - rsi_low + 1e-9)
        self.k = bt.indicators.MovingAverageSimple(stoch_rsi, period=self.p.stochSmooth)

        # Money Flow Index
        # Use built‑in indicator if available; otherwise compute manually
        try:
            self.mfi = bt.indicators.MoneyFlowIndex(self.data, period=self.p.mfiLength)
        except Exception:
            tp = (self.data.high + self.data.low + self.data.close) / 3.0
            raw_mf = tp * self.data.volume
            pos_flow = bt.indicators.SumN(
                bt.If(tp > tp(-1), raw_mf, 0.0), period=self.p.mfiLength
            )
            neg_flow = bt.indicators.SumN(
                bt.If(tp < tp(-1), raw_mf, 0.0), period=self.p.mfiLength
            )
            money_ratio = pos_flow / (neg_flow + 1e-9)
            self.mfi = 100.0 - (100.0 / (1.0 + money_ratio))

        # MACD components
        self.fastMA = bt.indicators.EMA(self.data.close, period=self.p.macdFast)
        self.slowMA = bt.indicators.EMA(self.data.close, period=self.p.macdSlow)
        self.macdLine = self.fastMA - self.slowMA
        self.signalLine = bt.indicators.EMA(self.macdLine, period=self.p.macdSignal)

        # ATR and ADX
        self.atr = bt.indicators.ATR(self.data, period=self.p.atrLength)
        self.adx = bt.indicators.ADX(self.data, period=self.p.adxLength)

        # State variables for trade management
        self.call_entry_bar = None
        self.put_entry_bar = None
        self.call_entry_price = None
        self.put_entry_price = None
        self.call_take_profit = None
        self.call_stop_loss = None
        self.put_take_profit = None
        self.put_stop_loss = None
        self.hug_lower_count = 0
        self.hug_upper_count = 0

        # Tracking trade outcomes for performance statistics
        # trade_profits will collect per-trade profit or loss (in currency units)
        # total_trades counts all closed trades; profitable_trades counts trades with positive PnL
        self.trade_profits = []  # type: List[float]
        self.total_trades = 0
        self.profitable_trades = 0

    def next(self) -> None:
        # Determine volatility regime using current width vs moving average
        high_vol = self.bbWidth[0] > self.bbWidthMA[0]
        # Adaptive thresholds
        dynKLow = max(5, self.p.K_OVERSOLD - 10) if high_vol else self.p.K_OVERSOLD
        dynKHigh = min(100, self.p.K_OVERBOUGHT + 10) if high_vol else self.p.K_OVERBOUGHT
        dynMFILow = max(5, self.p.MFI_OVERSOLD - 10) if high_vol else self.p.MFI_OVERSOLD
        dynMFIHigh = min(100, self.p.MFI_OVERBOUGHT + 10) if high_vol else self.p.MFI_OVERBOUGHT
        # Hugging zones
        width = self.upper[0] - self.lower[0]
        hug_lower = self.lower[0] + width * self.p.hugPct
        hug_upper = self.upper[0] - width * self.p.hugPct
        # Update hugging counters
        if self.data.close[0] <= hug_lower:
            self.hug_lower_count += 1
        else:
            self.hug_lower_count = 0
        if self.data.close[0] >= hug_upper:
            self.hug_upper_count += 1
        else:
            self.hug_upper_count = 0
        # Compute multi‑indicator scores
        bull_score = 0
        if self.k[0] < dynKLow:
            bull_score += 1
        if self.mfi[0] < dynMFILow:
            bull_score += 1
        if self.data.close[0] <= self.lower[0]:
            bull_score += 1
        if self.macdLine[0] > self.signalLine[0]:
            bull_score += 1
        bear_score = 0
        if self.k[0] > dynKHigh:
            bear_score += 1
        if self.mfi[0] > dynMFIHigh:
            bear_score += 1
        if self.data.close[0] >= self.upper[0]:
            bear_score += 1
        if self.macdLine[0] < self.signalLine[0]:
            bear_score += 1
        # Trend filters
        trend_bull = (self.fastMA[0] >= self.slowMA[0]) or (self.adx[0] < self.p.adxThreshold)
        trend_bear = (self.fastMA[0] <= self.slowMA[0]) or (self.adx[0] < self.p.adxThreshold)
        bull_condition = (bull_score >= self.p.MIN_SCORE) and trend_bull
        bear_condition = (bear_score >= self.p.MIN_SCORE) and trend_bear
        # Current bar index
        current_bar = len(self)
        # Manage open long (call) position
        if self.position.size > 0:
            # Hugging flip from long to short
            if (self.hug_lower_count >= self.p.hugBars) and (self.fastMA[0] < self.slowMA[0]):
                # Close existing long
                self.close()
                self.call_entry_bar = None
                # Enter short
                self.sell()
                self.put_entry_bar = current_bar
                self.put_entry_price = self.data.close[0]
                self.put_take_profit = self.data.close[0] - self.p.profitFactor * self.atr[0]
                self.put_stop_loss = self.data.close[0] + self.p.stopFactor * self.atr[0]
                # Reset hugging counters
                self.hug_lower_count = 0
                self.hug_upper_count = 0
                return
            # Exit conditions for long
            exitDynKHigh = (min(100, self.p.K_OVERBOUGHT + 10) if high_vol else self.p.K_OVERBOUGHT)
            exitDynMFIHigh = (min(100, self.p.MFI_OVERBOUGHT + 10) if high_vol else self.p.MFI_OVERBOUGHT)
            opposite_call = ((self.k[0] > exitDynKHigh) or (self.mfi[0] > exitDynMFIHigh)) and (self.macdLine[0] < self.signalLine[0])
            time_exceeded = (
                self.call_entry_bar is not None and current_bar >= self.call_entry_bar + self.p.EXIT_BARS
            )
            extended_limit = (
                self.call_entry_bar is not None and current_bar >= self.call_entry_bar + self.p.EXIT_BARS * 2
            )
            should_exit_due_time = time_exceeded and (
                (self.fastMA[0] < self.slowMA[0] and self.adx[0] >= self.p.adxThreshold) or extended_limit
            )
            if (
                self.data.close[0] >= (self.call_take_profit or float('inf'))
                or self.data.close[0] <= (self.call_stop_loss or float('-inf'))
                or opposite_call
                or should_exit_due_time
            ):
                self.close()
                self.call_entry_bar = None
                self.hug_lower_count = 0
                self.hug_upper_count = 0
                return
        # Manage open short (put) position
        elif self.position.size < 0:
            # Hugging flip from short to long
            if (self.hug_upper_count >= self.p.hugBars) and (self.fastMA[0] > self.slowMA[0]):
                self.close()
                self.put_entry_bar = None
                # Enter long
                self.buy()
                self.call_entry_bar = current_bar
                self.call_entry_price = self.data.close[0]
                self.call_take_profit = self.data.close[0] + self.p.profitFactor * self.atr[0]
                self.call_stop_loss = self.data.close[0] - self.p.stopFactor * self.atr[0]
                self.hug_lower_count = 0
                self.hug_upper_count = 0
                return
            exitDynKLow = (max(5, self.p.K_OVERSOLD - 10) if high_vol else self.p.K_OVERSOLD)
            exitDynMFILow = (max(5, self.p.MFI_OVERSOLD - 10) if high_vol else self.p.MFI_OVERSOLD)
            opposite_put = ((self.k[0] < exitDynKLow) or (self.mfi[0] < exitDynMFILow)) and (
                self.macdLine[0] > self.signalLine[0]
            )
            time_exceeded = (
                self.put_entry_bar is not None and current_bar >= self.put_entry_bar + self.p.EXIT_BARS
            )
            extended_limit = (
                self.put_entry_bar is not None and current_bar >= self.put_entry_bar + self.p.EXIT_BARS * 2
            )
            should_exit_due_time = time_exceeded and (
                (self.fastMA[0] > self.slowMA[0] and self.adx[0] >= self.p.adxThreshold) or extended_limit
            )
            if (
                self.data.close[0] <= (self.put_take_profit or float('-inf'))
                or self.data.close[0] >= (self.put_stop_loss or float('inf'))
                or opposite_put
                or should_exit_due_time
            ):
                self.close()
                self.put_entry_bar = None
                self.hug_lower_count = 0
                self.hug_upper_count = 0
                return
        # No open position: evaluate entry conditions
        else:
            if bull_condition:
                self.buy()
                self.call_entry_bar = current_bar
                self.call_entry_price = self.data.close[0]
                self.call_take_profit = self.data.close[0] + self.p.profitFactor * self.atr[0]
                self.call_stop_loss = self.data.close[0] - self.p.stopFactor * self.atr[0]
                self.hug_lower_count = 0
                self.hug_upper_count = 0
            elif bear_condition:
                self.sell()
                self.put_entry_bar = current_bar
                self.put_entry_price = self.data.close[0]
                self.put_take_profit = self.data.close[0] - self.p.profitFactor * self.atr[0]
                self.put_stop_loss = self.data.close[0] + self.p.stopFactor * self.atr[0]
                self.hug_lower_count = 0
                self.hug_upper_count = 0

    def notify_trade(self, trade: bt.trade.Trade) -> None:
        """Capture trade results when a trade is closed.

        Backtrader calls this method whenever a trade transitions state.  We
        record the profit/loss on fully closed trades to compute statistics
        after the backtest completes.
        """
        if trade.isclosed:
            pnl = trade.pnl  # profit/loss in currency units
            self.trade_profits.append(pnl)
            self.total_trades += 1
            if pnl > 0:
                self.profitable_trades += 1


def load_csv(path: str) -> pd.DataFrame:
    """Load a CSV file and prepare it for Backtrader.

    The CSV must include columns 'time', 'open', 'high', 'low', 'close' and 'volume'.
    The 'time' column is interpreted as Unix epoch seconds.  Only the most
    recent 365 records are kept to limit backtesting to approximately one
    trading year.
    """
    df = pd.read_csv(path)
    # Rename columns to standard names if needed
    col_map = {c.lower(): c for c in df.columns}
    required = ['time', 'open', 'high', 'low', 'close', 'volume']
    missing = [r for r in required if r not in col_map]
    if missing:
        raise ValueError(f"CSV file {path} is missing required columns: {missing}")
    # Create a unified DataFrame with lowercase column names
    df_std = df[[col_map[c] for c in required]].copy()
    df_std.columns = required
    # Convert epoch seconds to datetime
    df_std['datetime'] = pd.to_datetime(df_std['time'], unit='s')
    df_std = df_std.set_index('datetime')
    # Keep only the last 365 entries
    if len(df_std) > 365:
        df_std = df_std.tail(365)
    # Drop the raw 'time' column
    df_std = df_std.drop(columns=['time'])
    return df_std


def run_backtests(data_paths: List[str], cash: float) -> None:
    """Run the hugging strategy on each CSV and print the final portfolio value."""
    for path in data_paths:
        cerebro = bt.Cerebro()
        df = load_csv(path)
        data_feed = bt.feeds.PandasData(dataname=df)
        cerebro.adddata(data_feed)
        cerebro.addstrategy(HuggingStrategy)
        cerebro.broker.setcash(cash)
        # Disable commission and slippage for simplicity
        cerebro.broker.setcommission(commission=0.0)
        cerebro.broker.set_slippage_perc(perc=0.0)
        # Run backtest
        # Run backtest and retrieve strategy instance
        results = cerebro.run()
        final_value = cerebro.broker.getvalue()
        # There will be one strategy instance in results for each run
        strat = results[0]
        total_trades = strat.total_trades
        profitable_trades = strat.profitable_trades
        pop = (profitable_trades / total_trades) if total_trades else 0.0
        avg_profit = (sum(strat.trade_profits) / total_trades) if total_trades else 0.0
        if strat.trade_profits:
            profit_range = (min(strat.trade_profits), max(strat.trade_profits))
        else:
            profit_range = (0.0, 0.0)
        print(f"{path}: starting cash {cash:.2f} → final value {final_value:.2f}")
        print(f"    Trades: {total_trades}, Probability of profit: {pop:.2%}, "
              f"Average profit per trade: {avg_profit:.2f}, Profit range: {profit_range}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the hugging strategy on CSV files using Backtrader")
    parser.add_argument(
        '--data', nargs='+', required=True,
        help='One or more CSV file paths containing OHLCV data (columns: time, open, high, low, close, volume)'
    )
    parser.add_argument(
        '--cash', type=float, default=100000.0,
        help='Initial capital for each backtest (default: 100000)'
    )
    args = parser.parse_args()
    run_backtests(args.data, args.cash)


if __name__ == '__main__':
    main()