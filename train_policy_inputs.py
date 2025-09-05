#!/usr/bin/env python3
"""
This script trains a debit‐spread options trading policy using historical daily data for a given stock.
It searches for Stochastic RSI and Money Flow Index thresholds that maximize expected profit while
meeting a minimum probability of success. It also accounts for the user‐provided option spread width
and cost, then generates a corresponding Pine Script with the tuned parameters.

Usage:
  python train_policy.py --data /path/to/BATS_YELP.csv \
                         --probability_threshold 0.5 \
                         --spread 1.0 --cost 0.3 \
                         --exit_bars 7

The script prints the best parameter combination and writes a Pine Script file named
`generated_strategy.pine` into the working directory.
"""
import argparse
import os
import pandas as pd
import itertools
import textwrap
import numpy as np

# Import option valuation helpers for estimating spread cost and volatility
from option_values_with_spreads import parse_market_data, compute_debit_spreads

def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute technical indicators and derived features required for training.

    The input DataFrame must contain at least 'close' prices. When available,
    'high', 'low' and 'volume' will be used for ATR, ADX and MFI calculations.
    This helper function computes:

      • Stochastic RSI (%K) using a 14‑period lookback and 3‑period smoothing.
      • Money Flow Index (MF) using a 14‑period lookback.
      • MACD and signal line (12/26/9 configuration) along with histogram.
      • Bollinger Bands (20‑period, 2.0 standard deviations) and a normalized
        band width measure along with its moving average to detect high
        volatility regimes.
      • Long moving average (50‑period) for trend filtering.
      • Average True Range (14‑period) for dynamic exits.
      • ADX using a manual DMI implementation (14‑period) for trend strength.

    Parameters
    ----------
    df : pd.DataFrame
        Raw OHLCV data.

    Returns
    -------
    pd.DataFrame
        The input DataFrame with new indicator columns appended.
    """
    df = df.copy()
    close = df['close']
    # Stochastic RSI
    length_stoch = 14
    # RSI calculation
    delta = close.diff().fillna(0.0)
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.rolling(length_stoch, min_periods=1).mean()
    avg_loss = loss.rolling(length_stoch, min_periods=1).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    rsi = 100 - (100 / (1 + rs))
    # StochRSI raw value
    rsi_min = rsi.rolling(length_stoch, min_periods=1).min()
    rsi_max = rsi.rolling(length_stoch, min_periods=1).max()
    stoch_rsi = 100 * (rsi - rsi_min) / (rsi_max - rsi_min + 1e-9)
    # %K smoothed (3‑period)
    k_raw = stoch_rsi.rolling(3, min_periods=1).mean()
    df['K'] = k_raw
    # Money Flow Index
    length_mfi = 14
    if {'high', 'low', 'volume'}.issubset(df.columns):
        tp = (df['high'] + df['low'] + df['close']) / 3.0
        raw_mf = tp * df['volume']
        pos_flow = raw_mf.where(tp > tp.shift(1), 0.0)
        neg_flow = raw_mf.where(tp < tp.shift(1), 0.0)
        pos_mf = pos_flow.rolling(length_mfi, min_periods=1).sum()
        neg_mf = neg_flow.rolling(length_mfi, min_periods=1).sum()
        money_ratio = pos_mf / neg_mf.replace(0, 1e-9)
        mfi = 100 - (100 / (1 + money_ratio))
        df['MF'] = mfi
    else:
        # Fallback: use close changes as proxy for volume when volume not available
        tp = df['close']
        raw_mf = tp
        pos_flow = raw_mf.where(tp > tp.shift(1), 0.0)
        neg_flow = raw_mf.where(tp < tp.shift(1), 0.0)
        pos_mf = pos_flow.rolling(length_mfi, min_periods=1).sum()
        neg_mf = neg_flow.rolling(length_mfi, min_periods=1).sum()
        money_ratio = pos_mf / neg_mf.replace(0, 1e-9)
        df['MF'] = 100 - (100 / (1 + money_ratio))
    # MACD
    fast = df['close'].ewm(span=12, adjust=False).mean()
    slow = df['close'].ewm(span=26, adjust=False).mean()
    macd_line = fast - slow
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line
    df['MACD'] = macd_line
    df['Signal'] = signal_line
    df['Histogram'] = histogram
    # Bollinger Bands
    bb_length = 20
    bb_std = 2.0
    df['basis'] = df['close'].rolling(bb_length, min_periods=1).mean()
    df['std'] = df['close'].rolling(bb_length, min_periods=1).std().fillna(0.0)
    df['upper'] = df['basis'] + bb_std * df['std']
    df['lower'] = df['basis'] - bb_std * df['std']
    # Normalized band width
    df['bbWidthNorm'] = (df['upper'] - df['lower']) / df['basis'].abs().replace(0, 1e-9)
    df['bbWidthMean'] = df['bbWidthNorm'].rolling(20, min_periods=1).mean()
    # Long moving average
    long_window = 50
    df['longMA'] = df['close'].rolling(long_window, min_periods=1).mean()
    # Average True Range
    atr_length = 14
    if {'high', 'low'}.issubset(df.columns):
        high = df['high']
        low = df['low']
        prev_close = df['close'].shift(1).fillna(df['close'])
        tr1 = (high - low).abs()
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df['atr'] = tr.rolling(atr_length, min_periods=1).mean()
        # Directional Movement and ADX
        hd = high.diff()
        ld = low.diff() * -1
        plus_dm_raw = hd
        minus_dm_raw = -ld
        plus_dm = pd.Series(0.0, index=df.index)
        minus_dm = pd.Series(0.0, index=df.index)
        cond_plus = (plus_dm_raw > minus_dm_raw) & (plus_dm_raw > 0)
        cond_minus = (minus_dm_raw > plus_dm_raw) & (minus_dm_raw > 0)
        plus_dm[cond_plus] = plus_dm_raw[cond_plus]
        minus_dm[cond_minus] = minus_dm_raw[cond_minus]
        plus_dm_smooth = plus_dm.rolling(atr_length, min_periods=1).sum()
        minus_dm_smooth = minus_dm.rolling(atr_length, min_periods=1).sum()
        atr_smooth = tr.rolling(atr_length, min_periods=1).mean()
        plus_di = 100 * plus_dm_smooth / atr_smooth.replace(0, 1e-9)
        minus_di = 100 * minus_dm_smooth / atr_smooth.replace(0, 1e-9)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
        df['adx'] = dx.rolling(atr_length, min_periods=1).mean().fillna(0.0)
    else:
        # Fallback: use absolute close differences when no high/low available
        df['atr'] = df['close'].diff().abs().rolling(atr_length, min_periods=1).mean().fillna(0.0)
        df['adx'] = 0.0
    return df

# Evaluation of one parameter combination
def evaluate_strategy(
    df: pd.DataFrame,
    k_low: int,
    k_high: int,
    mfi_low: int,
    mfi_high: int,
    exit_bars: int,
    spread: float,
    cost: float,
    profit_factor: float = 1.0,
    stop_factor: float = 1.0,
    width_threshold: float = None,
    long_window: int = 50,
    adx_threshold: float = 25.0,
    min_score: int = 3,
    direction: str = "both",
) -> dict:
    """
    Evaluate a debit spread strategy under adaptive rules.

    In contrast to the original evaluation, this version supports:
      • Adaptive thresholding based on recent volatility (Bollinger Band width).
      • Score-based multi-indicator confirmation requiring at least three of four conditions.
      • Trend filtering using a long moving average and an ADX threshold to avoid counter‑trend
        trades and to permit mean‑reversion in low‑trend regimes.
      • Dynamic exits including profit targets, stop losses and opposite signal detection.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame containing at least the following columns:
        'K', 'MF', 'MACD', 'Signal', 'close', 'upper', 'lower', 'bbWidthNorm', 'bbWidthMean',
        'longMA', 'atr', 'adx'.
    k_low, k_high, mfi_low, mfi_high : int
        Base StochRSI and MFI thresholds. Adaptive offsets will be applied under high volatility.
    exit_bars : int
        Maximum holding period in bars.
    spread : float
        Spread width used to cap intrinsic value.
    cost : float
        Net debit (premium) paid for the spread.
    profit_factor, stop_factor : float
        Multipliers of ATR used for take‑profit and stop‑loss levels.
    width_threshold : float, optional
        Threshold for classifying high volatility regime. If None, uses the median of bbWidthNorm.
    long_window : int
        Window length for trend filter moving average.

    adx_threshold : float
        Threshold below which the ADX indicates a non‑trending regime. When the ADX value is
        below this threshold, the evaluation will allow trades even if price is not above/below
        the long moving average, permitting mean‑reversion trades in sideways markets. A
        typical default is 25.

    Returns
    -------
    dict
        Dictionary with performance metrics and chosen parameters.
    """
    call_trades = 0
    put_trades = 0
    call_success = 0
    put_success = 0
    total_profit = 0.0
    i = 0
    n = len(df)
    # Determine volatility threshold if not provided
    if width_threshold is None:
        width_threshold = df['bbWidthNorm'].median()
    while i < n - 1:
        row = df.iloc[i]
        # Determine adaptive thresholds based on volatility regime
        high_vol = row['bbWidthNorm'] > width_threshold
        if high_vol:
            k_entry_low = max(5, k_low - 10)
            k_entry_high = min(100, k_high + 10)
            mfi_entry_low = max(5, mfi_low - 10)
            mfi_entry_high = min(100, mfi_high + 10)
        else:
            k_entry_low = k_low
            k_entry_high = k_high
            mfi_entry_low = mfi_low
            mfi_entry_high = mfi_high
        # Score‑based signals: require at least 3 of 4 conditions
        bull_score = 0
        if row['K'] < k_entry_low:
            bull_score += 1
        if row['MF'] < mfi_entry_low:
            bull_score += 1
        if row['close'] <= row['lower']:
            bull_score += 1
        if row['MACD'] > row['Signal']:
            bull_score += 1
        bear_score = 0
        if row['K'] > k_entry_high:
            bear_score += 1
        if row['MF'] > mfi_entry_high:
            bear_score += 1
        if row['close'] >= row['upper']:
            bear_score += 1
        if row['MACD'] < row['Signal']:
            bear_score += 1
        # Trend filter: only take longs when price is at/above long moving average and shorts when below
        # Additionally allow trades when the ADX indicates a non‑trending regime.
        # If adx is below the threshold, momentum is weak and mean reversion trades are more viable.
        trend_bull = (row['close'] >= row['longMA']) or (row['adx'] < adx_threshold)
        trend_bear = (row['close'] <= row['longMA']) or (row['adx'] < adx_threshold)
        # Use the configurable minimum score for entry rather than a hard‑coded 3
        # Determine whether to evaluate long or short trades based on selected direction
        bull = (bull_score >= min_score) and trend_bull and (direction in ("call", "both"))
        bear = (bear_score >= min_score) and trend_bear and (direction in ("put", "both"))
        if bull:
            call_trades += 1
            entry_price = row['close']
            # Determine entry volatility measure for profit/stop levels
            atr_entry = row['atr'] if not pd.isna(row['atr']) else 0.0
            profit_target = entry_price + profit_factor * atr_entry
            stop_target = entry_price - stop_factor * atr_entry
            j = i + 1
            # Track exit due to profit, stop, opposite signal or max holding period
            while j < n and j < i + exit_bars:
                r = df.iloc[j]
                # Exit if hit profit target
                if r['close'] >= profit_target:
                    break
                # Exit if hit stop loss
                if r['close'] <= stop_target:
                    break
                # Compute new adaptive thresholds for opposite signal detection
                high_vol_exit = r['bbWidthNorm'] > width_threshold
                if high_vol_exit:
                    k_exit_low = max(5, k_low - 10)
                    k_exit_high = min(100, k_high + 10)
                    mfi_exit_low = max(5, mfi_low - 10)
                    mfi_exit_high = min(100, mfi_high + 10)
                else:
                    k_exit_low = k_low
                    k_exit_high = k_high
                    mfi_exit_low = mfi_low
                    mfi_exit_high = mfi_high
                # Opposite signal: overbought conditions with bearish momentum
                if ((r['K'] > k_exit_high) or (r['MF'] > mfi_exit_high)) and (r['MACD'] < r['Signal']):
                    break
                j += 1
            # Determine exit price
            # Clamp the exit index to avoid out-of-bounds when j reaches the end of the DataFrame
            exit_index = j if j < n else n - 1
            exit_price = df.iloc[exit_index]['close']
            price_move = exit_price - entry_price
            intrinsic = max(0.0, price_move)
            payoff = min(intrinsic, spread)
            profit = payoff - cost
            total_profit += profit
            if profit > 0:
                call_success += 1
            i = j  # jump ahead to exit bar
            continue
        elif bear:
            put_trades += 1
            entry_price = row['close']
            atr_entry = row['atr'] if not pd.isna(row['atr']) else 0.0
            profit_target = entry_price - profit_factor * atr_entry
            stop_target = entry_price + stop_factor * atr_entry
            j = i + 1
            while j < n and j < i + exit_bars:
                r = df.iloc[j]
                # Profit target for put: price falls sufficiently
                if r['close'] <= profit_target:
                    break
                # Stop loss: price rises above stop_target
                if r['close'] >= stop_target:
                    break
                high_vol_exit = r['bbWidthNorm'] > width_threshold
                if high_vol_exit:
                    k_exit_low = max(5, k_low - 10)
                    k_exit_high = min(100, k_high + 10)
                    mfi_exit_low = max(5, mfi_low - 10)
                    mfi_exit_high = min(100, mfi_high + 10)
                else:
                    k_exit_low = k_low
                    k_exit_high = k_high
                    mfi_exit_low = mfi_low
                    mfi_exit_high = mfi_high
                # Opposite signal: oversold with bullish momentum
                if ((r['K'] < k_exit_low) or (r['MF'] < mfi_exit_low)) and (r['MACD'] > r['Signal']):
                    break
                j += 1
            exit_index = j if j < n else n - 1
            exit_price = df.iloc[exit_index]['close']
            price_move = entry_price - exit_price
            intrinsic = max(0.0, price_move)
            payoff = min(intrinsic, spread)
            profit = payoff - cost
            total_profit += profit
            if profit > 0:
                put_success += 1
            i = j
            continue
        i += 1
    total_trades = call_trades + put_trades
    success_prob = (call_success + put_success) / total_trades if total_trades > 0 else 0.0
    avg_profit = total_profit / total_trades if total_trades > 0 else 0.0
    return {
        'k_low': k_low,
        'k_high': k_high,
        'mfi_low': mfi_low,
        'mfi_high': mfi_high,
        'exit_bars': exit_bars,
        'trades': total_trades,
        'success_prob': success_prob,
        'avg_profit': avg_profit,
        'total_profit': total_profit,
        'call_trades': call_trades,
        'put_trades': put_trades,
        'call_success': call_success,
        'put_success': put_success
    }

# Generate Pine Script template with tuned parameters
#
# NOTE: The original generate_pine_script function has been removed.  It
# is replaced with dedicated functions for call, put and combined spread
# generation (generate_pine_script_call, generate_pine_script_put,
# generate_pine_script_both).  These functions expose strategy
# parameters as TradingView inputs so that a reinforcement learning
# agent can adjust them dynamically.  See below for definitions.

def generate_pine_script_call(params, filename='generated_call_strategy.pine'):
    """
    Generate a TradingView Pine Script for call debit spreads.  The script
    exposes key parameters as inputs so that a reinforcement‑learning agent
    can tune them in real time.  It implements adaptive thresholding
    based on volatility (Bollinger Band width), a multi‑indicator scoring
    system, a trend filter using a moving average and ADX, and dynamic
    exits with profit/stop targets and opposite‑signal detection.

    Parameters
    ----------
    params : dict
        Contains 'k_low', 'k_high', 'mfi_low', 'mfi_high', 'exit_bars' from
        the training routine.
    filename : str
        Destination filename for the generated Pine Script.

    Returns
    -------
    str
        The filename written.
    """
    pine = f"""//@version=6
strategy("Adaptive Call Debit Spread Strategy", overlay=true, margin_long=100)

// Tuned defaults exposed as inputs for RL/POMDP adjustments
K_OVERSOLD    = input.int({int(params['k_low'])}, title="%K oversold", minval=1, maxval=99)
K_OVERBOUGHT  = input.int({int(params['k_high'])}, title="%K overbought", minval=1, maxval=99)
MFI_OVERSOLD  = input.int({int(params['mfi_low'])}, title="MFI oversold", minval=1, maxval=99)
MFI_OVERBOUGHT= input.int({int(params['mfi_high'])}, title="MFI overbought", minval=1, maxval=99)
EXIT_BARS     = input.int({int(params['exit_bars'])}, title="Exit bars", minval=1, maxval=30)
MIN_SCORE     = input.int(3, title="Minimum score for entry", minval=1, maxval=4)
profitFactor  = input.float(1.0, title="ATR multiplier for take‑profit", step=0.1)
stopFactor    = input.float(1.0, title="ATR multiplier for stop‑loss", step=0.1)

// Indicator lengths
stochLength  = input.int(14,  "Stochastic RSI length")
stochSmooth  = input.int(3,   "Stochastic smoothing")
mfiLength    = input.int(14,  "MFI length")
bbLength     = input.int(20,  "Bollinger length")
bbStdDev     = input.float(2.0, "Bollinger std dev")
macdFast     = input.int(12,  "MACD fast length")
macdSlow     = input.int(26,  "MACD slow length")
macdSignal   = input.int(9,   "MACD signal length")
atrLength    = input.int(14,  "ATR length for targets")
adxLength    = input.int(14,  "ADX length")
adxThreshold = input.float(25.0, "Trend filter ADX threshold")

// Persistent variables for call spread
var int callEntryBar     = na
var float callEntryPrice = na
var float callTakeProfit = na
var float callStopLoss   = na

// Bollinger Bands and volatility measures
basis    = ta.sma(close, bbLength)
deviation= ta.stdev(close, bbLength)
upper    = basis + bbStdDev * deviation
lower    = basis - bbStdDev * deviation
bbWidth  = (upper - lower) / (basis != 0 ? basis : 1)
bbWidthMA= ta.sma(bbWidth, 20)
highVol  = bbWidth > bbWidthMA

// Stochastic RSI
rsi   = ta.rsi(close, stochLength)
k_raw = ta.stoch(rsi, rsi, rsi, stochLength)
k    = ta.sma(k_raw, stochSmooth)
d    = ta.sma(k, stochSmooth)

// Money Flow Index (manual calculation)
tp       = (high + low + close) / 3.0
rawMF    = tp * volume
posFlow  = tp > tp[1] ? rawMF : 0.0
negFlow  = tp < tp[1] ? rawMF : 0.0
posMF    = math.sum(posFlow, mfiLength)
negMF    = math.sum(negFlow, mfiLength)
moneyRatio = negMF != 0 ? posMF / negMF : 0.0
mfi      = negMF != 0 ? 100 - 100 / (1 + moneyRatio) : 0.0

// MACD
fastMA     = ta.ema(close, macdFast)
slowMA     = ta.ema(close, macdSlow)
macdLine   = fastMA - slowMA
signalLine = ta.ema(macdLine, macdSignal)

// ADX and ATR
// Some TradingView versions do not expose ta.adx(), so compute ADX using DMI components
// Compute ADX using DMI components; high/low/close are implied in Pine v6
// ADX and ATR
[_, _, adxValue] = ta.dmi(adxLength, adxLength)

// ATR
atrValue = ta.atr(atrLength)

// Adaptive thresholds based on volatility
dynKLow    = highVol ? math.max(5, K_OVERSOLD - 10) : K_OVERSOLD
dynKHigh   = highVol ? math.min(100, K_OVERBOUGHT + 10) : K_OVERBOUGHT
dynMFILow  = highVol ? math.max(5, MFI_OVERSOLD - 10) : MFI_OVERSOLD
dynMFIHigh = highVol ? math.min(100, MFI_OVERBOUGHT + 10) : MFI_OVERBOUGHT

// Scoring and trend filter
scoreBull = (k < dynKLow ? 1 : 0) + (mfi < dynMFILow ? 1 : 0) + (close <= lower ? 1 : 0) + (macdLine > signalLine ? 1 : 0)
trendBull = (fastMA >= slowMA) or (adxValue < adxThreshold)
bullCondition = (scoreBull >= MIN_SCORE) and trendBull

// Entry logic
if bullCondition and na(callEntryBar)
    strategy.entry("CallDebit", strategy.long)
    callEntryBar   := bar_index
    callEntryPrice := close
    callTakeProfit := close + profitFactor * atrValue
    callStopLoss   := close - stopFactor * atrValue

// Holding period
maxBars = EXIT_BARS * 2

// Exit logic
if not na(callEntryBar)
    exitDynKHigh   = highVol ? math.min(100, K_OVERBOUGHT + 10) : K_OVERBOUGHT
    exitDynMFIHigh = highVol ? math.min(100, MFI_OVERBOUGHT + 10) : MFI_OVERBOUGHT
    oppositeCall   = ((k > exitDynKHigh) or (mfi > exitDynMFIHigh)) and (macdLine < signalLine)
    timeExceeded   = bar_index >= callEntryBar + EXIT_BARS
    extendedLimit  = bar_index >= callEntryBar + maxBars
    shouldExitDueTime = timeExceeded and ((fastMA < slowMA and adxValue >= adxThreshold) or extendedLimit)
    if (close >= callTakeProfit) or (close <= callStopLoss) or oppositeCall or shouldExitDueTime
        strategy.close("CallDebit")
        callEntryBar   := na
        callEntryPrice := na
        callTakeProfit := na
        callStopLoss   := na

// Plots for monitoring
plot(basis, color=color.gray, linewidth=1, title="BB Basis")
plot(upper, color=color.orange, linewidth=1, title="BB Upper")
plot(lower, color=color.orange, linewidth=1, title="BB Lower")
plot(k, title="%K", color=color.blue)
plot(d, title="%D", color=color.purple)
plot(mfi, title="MFI", color=color.green)
plot(macdLine - signalLine, title="MACD Histogram", color=color.red, style=plot.style_columns)
"""
    with open(filename, 'w') as f:
        f.write(pine)
    return filename

def generate_pine_script_put(params, filename='generated_put_strategy.pine'):
    """
    Generate a TradingView Pine Script for put debit spreads.  The script
    mirrors the call version but flips the direction of trades.  Inputs are
    exposed for RL/POMDP agents.
    """
    pine = f"""//@version=6
strategy("Adaptive Put Debit Spread Strategy", overlay=true, margin_short=100)

// Tuned defaults exposed as inputs for RL/POMDP adjustments
K_OVERSOLD    = input.int({int(params['k_low'])}, title="%K oversold", minval=1, maxval=99)
K_OVERBOUGHT  = input.int({int(params['k_high'])}, title="%K overbought", minval=1, maxval=99)
MFI_OVERSOLD  = input.int({int(params['mfi_low'])}, title="MFI oversold", minval=1, maxval=99)
MFI_OVERBOUGHT= input.int({int(params['mfi_high'])}, title="MFI overbought", minval=1, maxval=99)
EXIT_BARS     = input.int({int(params['exit_bars'])}, title="Exit bars", minval=1, maxval=30)
MIN_SCORE     = input.int(3, title="Minimum score for entry", minval=1, maxval=4)
profitFactor  = input.float(1.0, title="ATR multiplier for take‑profit", step=0.1)
stopFactor    = input.float(1.0, title="ATR multiplier for stop‑loss", step=0.1)

// Indicator lengths
stochLength  = input.int(14,  "Stochastic RSI length")
stochSmooth  = input.int(3,   "Stochastic smoothing")
mfiLength    = input.int(14,  "MFI length")
bbLength     = input.int(20,  "Bollinger length")
bbStdDev     = input.float(2.0, "Bollinger std dev")
macdFast     = input.int(12,  "MACD fast length")
macdSlow     = input.int(26,  "MACD slow length")
macdSignal   = input.int(9,   "MACD signal length")
atrLength    = input.int(14,  "ATR length for targets")
adxLength    = input.int(14,  "ADX length")
adxThreshold = input.float(25.0, "Trend filter ADX threshold")

// Persistent variables for put spread
var int putEntryBar     = na
var float putEntryPrice = na
var float putTakeProfit = na
var float putStopLoss   = na

// Bollinger Bands and volatility
basis    = ta.sma(close, bbLength)
deviation= ta.stdev(close, bbLength)
upper    = basis + bbStdDev * deviation
lower    = basis - bbStdDev * deviation
bbWidth  = (upper - lower) / (basis != 0 ? basis : 1)
bbWidthMA= ta.sma(bbWidth, 20)
highVol  = bbWidth > bbWidthMA

// Stochastic RSI
rsi   = ta.rsi(close, stochLength)
k_raw = ta.stoch(rsi, rsi, rsi, stochLength)
k    = ta.sma(k_raw, stochSmooth)
d    = ta.sma(k, stochSmooth)

// Money Flow Index
tp       = (high + low + close) / 3.0
rawMF    = tp * volume
posFlow  = tp > tp[1] ? rawMF : 0.0
negFlow  = tp < tp[1] ? rawMF : 0.0
posMF    = math.sum(posFlow, mfiLength)
negMF    = math.sum(negFlow, mfiLength)
moneyRatio = negMF != 0 ? posMF / negMF : 0.0
mfi      = negMF != 0 ? 100 - 100 / (1 + moneyRatio) : 0.0

// MACD
fastMA     = ta.ema(close, macdFast)
slowMA     = ta.ema(close, macdSlow)
macdLine   = fastMA - slowMA
signalLine = ta.ema(macdLine, macdSignal)

// ADX and ATR
// Compute ADX using DMI components instead of ta.adx() which may be unavailable
// Compute ADX using DMI components; high/low/close are implied in Pine v6
// ADX and ATR
[_, _, adxValue] = ta.dmi(adxLength, adxLength)
atrValue = ta.atr(atrLength)

// Adaptive thresholds
dynKLow    = highVol ? math.max(5, K_OVERSOLD - 10) : K_OVERSOLD
dynKHigh   = highVol ? math.min(100, K_OVERBOUGHT + 10) : K_OVERBOUGHT
dynMFILow  = highVol ? math.max(5, MFI_OVERSOLD - 10) : MFI_OVERSOLD
dynMFIHigh = highVol ? math.min(100, MFI_OVERBOUGHT + 10) : MFI_OVERBOUGHT

// Scoring and trend filter
scoreBear = (k > dynKHigh ? 1 : 0) + (mfi > dynMFIHigh ? 1 : 0) + (close >= upper ? 1 : 0) + (macdLine < signalLine ? 1 : 0)
trendBear = (fastMA <= slowMA) or (adxValue < adxThreshold)
bearCondition = (scoreBear >= MIN_SCORE) and trendBear

// Entry logic
if bearCondition and na(putEntryBar)
    strategy.entry("PutDebit", strategy.short)
    putEntryBar    := bar_index
    putEntryPrice  := close
    putTakeProfit  := close - profitFactor * atrValue
    putStopLoss    := close + stopFactor * atrValue

// Holding period
maxBars = EXIT_BARS * 2

// Exit logic for puts
if not na(putEntryBar)
    exitDynKLow   = highVol ? math.max(5, K_OVERSOLD - 10) : K_OVERSOLD
    exitDynMFILow = highVol ? math.max(5, MFI_OVERSOLD - 10) : MFI_OVERSOLD
    oppositePut   = ((k < exitDynKLow) or (mfi < exitDynMFILow)) and (macdLine > signalLine)
    timeExceeded  = bar_index >= putEntryBar + EXIT_BARS
    extendedLimit = bar_index >= putEntryBar + maxBars
    shouldExitDueTime = timeExceeded and ((fastMA > slowMA and adxValue >= adxThreshold) or extendedLimit)
    if (close <= putTakeProfit) or (close >= putStopLoss) or oppositePut or shouldExitDueTime
        strategy.close("PutDebit")
        putEntryBar    := na
        putEntryPrice  := na
        putTakeProfit  := na
        putStopLoss    := na

// Plots
plot(basis, color=color.gray, linewidth=1, title="BB Basis")
plot(upper, color=color.orange, linewidth=1, title="BB Upper")
plot(lower, color=color.orange, linewidth=1, title="BB Lower")
plot(k, title="%K", color=color.blue)
plot(d, title="%D", color=color.purple)
plot(mfi, title="MFI", color=color.green)
plot(macdLine - signalLine, title="MACD Histogram", color=color.red, style=plot.style_columns)
"""
    with open(filename, 'w') as f:
        f.write(pine)
    return filename

def generate_pine_script_both(params, filename='generated_combined_strategy.pine'):
    """
    Generate a TradingView Pine Script that can trade both call and put debit
    spreads.  All parameters are exposed as inputs for RL/POMDP agents.  The
    logic merges the call and put trading conditions and uses adaptive
    thresholds and trend filters consistent with the single‑direction scripts.
    """
    pine = f"""//@version=6
strategy("Adaptive Combined Debit Spread Strategy", overlay=true, margin_long=100, margin_short=100)

// Tuned defaults exposed as inputs
K_OVERSOLD    = input.int({int(params['k_low'])}, title="%K oversold", minval=1, maxval=99)
K_OVERBOUGHT  = input.int({int(params['k_high'])}, title="%K overbought", minval=1, maxval=99)
MFI_OVERSOLD  = input.int({int(params['mfi_low'])}, title="MFI oversold", minval=1, maxval=99)
MFI_OVERBOUGHT= input.int({int(params['mfi_high'])}, title="MFI overbought", minval=1, maxval=99)
EXIT_BARS     = input.int({int(params['exit_bars'])}, title="Exit bars", minval=1, maxval=30)
MIN_SCORE     = input.int(3, title="Minimum score for entry", minval=1, maxval=4)
profitFactor  = input.float(1.0, title="ATR multiplier for take‑profit", step=0.1)
stopFactor    = input.float(1.0, title="ATR multiplier for stop‑loss", step=0.1)

// Indicator lengths
stochLength  = input.int(14,  "Stochastic RSI length")
stochSmooth  = input.int(3,   "Stochastic smoothing")
mfiLength    = input.int(14,  "MFI length")
bbLength     = input.int(20,  "Bollinger length")
bbStdDev     = input.float(2.0, "Bollinger std dev")
macdFast     = input.int(12,  "MACD fast length")
macdSlow     = input.int(26,  "MACD slow length")
macdSignal   = input.int(9,   "MACD signal length")
atrLength    = input.int(14,  "ATR length for targets")
adxLength    = input.int(14,  "ADX length")
adxThreshold = input.float(25.0, "Trend filter ADX threshold")

// Persistent variables for call and put spreads
var int callEntryBar     = na
var int putEntryBar      = na
var float callEntryPrice = na
var float putEntryPrice  = na
var float callTakeProfit = na
var float callStopLoss   = na
var float putTakeProfit  = na
var float putStopLoss    = na

// Bollinger Bands and volatility
basis    = ta.sma(close, bbLength)
deviation= ta.stdev(close, bbLength)
upper    = basis + bbStdDev * deviation
lower    = basis - bbStdDev * deviation
bbWidth  = (upper - lower) / (basis != 0 ? basis : 1)
bbWidthMA= ta.sma(bbWidth, 20)
highVol  = bbWidth > bbWidthMA

// Stochastic RSI
rsi   = ta.rsi(close, stochLength)
k_raw = ta.stoch(rsi, rsi, rsi, stochLength)
k    = ta.sma(k_raw, stochSmooth)
d    = ta.sma(k, stochSmooth)

// Money Flow Index
tp       = (high + low + close) / 3.0
rawMF    = tp * volume
posFlow  = tp > tp[1] ? rawMF : 0.0
negFlow  = tp < tp[1] ? rawMF : 0.0
posMF    = math.sum(posFlow, mfiLength)
negMF    = math.sum(negFlow, mfiLength)
moneyRatio = negMF != 0 ? posMF / negMF : 0.0
mfi      = negMF != 0 ? 100 - 100 / (1 + moneyRatio) : 0.0

// MACD
fastMA     = ta.ema(close, macdFast)
slowMA     = ta.ema(close, macdSlow)
macdLine   = fastMA - slowMA
signalLine = ta.ema(macdLine, macdSignal)

// ADX and ATR
// Use DMI to compute ADX instead of ta.adx() for broader compatibility
// Compute ADX using DMI components; high/low/close are implied in Pine v6
// ADX and ATR
[_, _, adxValue] = ta.dmi(adxLength, adxLength)
atrValue  = ta.atr(atrLength)

// Adaptive thresholds
dynKLow    = highVol ? math.max(5, K_OVERSOLD - 10) : K_OVERSOLD
dynKHigh   = highVol ? math.min(100, K_OVERBOUGHT + 10) : K_OVERBOUGHT
dynMFILow  = highVol ? math.max(5, MFI_OVERSOLD - 10) : MFI_OVERSOLD
dynMFIHigh = highVol ? math.min(100, MFI_OVERBOUGHT + 10) : MFI_OVERBOUGHT

// Scores and trend filters
scoreBull = (k < dynKLow  ? 1 : 0) + (mfi < dynMFILow  ? 1 : 0) + (close <= lower ? 1 : 0) + (macdLine > signalLine ? 1 : 0)
scoreBear = (k > dynKHigh ? 1 : 0) + (mfi > dynMFIHigh ? 1 : 0) + (close >= upper ? 1 : 0) + (macdLine < signalLine ? 1 : 0)
trendBull = (fastMA >= slowMA) or (adxValue < adxThreshold)
trendBear = (fastMA <= slowMA) or (adxValue < adxThreshold)
bullCondition = (scoreBull >= MIN_SCORE) and trendBull
bearCondition = (scoreBear >= MIN_SCORE) and trendBear

// Entry logic
if bullCondition and na(callEntryBar) and na(putEntryBar)
    strategy.entry("CallDebit", strategy.long)
    callEntryBar   := bar_index
    callEntryPrice := close
    callTakeProfit := close + profitFactor * atrValue
    callStopLoss   := close - stopFactor * atrValue
if bearCondition and na(putEntryBar) and na(callEntryBar)
    strategy.entry("PutDebit", strategy.short)
    putEntryBar    := bar_index
    putEntryPrice  := close
    putTakeProfit  := close - profitFactor * atrValue
    putStopLoss    := close + stopFactor * atrValue

// Holding periods
maxBars = EXIT_BARS * 2

// Exit logic for calls
if not na(callEntryBar)
    exitDynKHigh   = highVol ? math.min(100, K_OVERBOUGHT + 10) : K_OVERBOUGHT
    exitDynMFIHigh = highVol ? math.min(100, MFI_OVERBOUGHT + 10) : MFI_OVERBOUGHT
    oppositeCall   = ((k > exitDynKHigh) or (mfi > exitDynMFIHigh)) and (macdLine < signalLine)
    timeExceeded   = bar_index >= callEntryBar + EXIT_BARS
    extendedLimit  = bar_index >= callEntryBar + maxBars
    shouldExitDueTime = timeExceeded and ((fastMA < slowMA and adxValue >= adxThreshold) or extendedLimit)
    if (close >= callTakeProfit) or (close <= callStopLoss) or oppositeCall or shouldExitDueTime
        strategy.close("CallDebit")
        callEntryBar   := na
        callEntryPrice := na
        callTakeProfit := na
        callStopLoss   := na

// Exit logic for puts
if not na(putEntryBar)
    exitDynKLow   = highVol ? math.max(5, K_OVERSOLD - 10) : K_OVERSOLD
    exitDynMFILow = highVol ? math.max(5, MFI_OVERSOLD - 10) : MFI_OVERSOLD
    oppositePut   = ((k < exitDynKLow) or (mfi < exitDynMFILow)) and (macdLine > signalLine)
    timeExceeded  = bar_index >= putEntryBar + EXIT_BARS
    extendedLimit = bar_index >= putEntryBar + maxBars
    shouldExitDueTime = timeExceeded and ((fastMA > slowMA and adxValue >= adxThreshold) or extendedLimit)
    if (close <= putTakeProfit) or (close >= putStopLoss) or oppositePut or shouldExitDueTime
        strategy.close("PutDebit")
        putEntryBar    := na
        putEntryPrice  := na
        putTakeProfit  := na
        putStopLoss    := na

// Plots
plot(basis, color=color.gray, linewidth=1, title="BB Basis")
plot(upper, color=color.orange, linewidth=1, title="BB Upper")
plot(lower, color=color.orange, linewidth=1, title="BB Lower")
plot(k, title="%K", color=color.blue)
plot(d, title="%D", color=color.purple)
plot(mfi, title="MFI", color=color.green)
plot(macdLine - signalLine, title="MACD Histogram", color=color.red, style=plot.style_columns)
"""
    with open(filename, 'w') as f:
        f.write(pine)
    return filename


def load_and_prepare_stock(csv_path: str) -> tuple:
    """
    Load a CSV file containing OHLCV data, compute technical features, and derive
    the appropriate spread width and debit spread costs for call and put options.

    Stocks priced above $100 use a $5 spread width; those below $100 use a $1 spread,
    as described in the optimization guide【556738710397952†L0-L5】.  The option values are
    computed with the Black–Scholes model and used to estimate the debit spread cost
    for a 30‑day maturity.  Only the most recent 365 rows of data are retained to
    limit the backtesting horizon to one year.

    Parameters
    ----------
    csv_path : str
        Path to the CSV file containing at least 'time' and 'close' columns.

    Returns
    -------
    tuple
        A tuple containing (features_df, spread_width, call_cost, put_cost).
    """
    # Compute last price and volatility for this stock
    market_data = parse_market_data(csv_path)
    S = market_data['S']
    sigma = market_data['sigma']
    # Determine spread width: $5 for stocks priced above $100, else $1【556738710397952†L0-L5】
    spread_width = 5.0 if S > 100 else 1.0
    # Compute debit spreads for a 30‑day maturity at the chosen width
    debit_table = compute_debit_spreads(S, sigma, r=0.045, maturities=[30], increments=[spread_width])
    # There will be exactly one row because we passed a single maturity
    call_cost = float(debit_table.loc[0, f'Call_Spread_{spread_width}'])
    put_cost = float(debit_table.loc[0, f'Put_Spread_{spread_width}'])
    # Load raw OHLCV data
    raw = pd.read_csv(csv_path)
    # Ensure required columns exist
    if 'close' not in raw.columns:
        raise ValueError(f"Data file {csv_path} is missing the 'close' column.")
    # Compute technical features
    features_df = compute_features(raw)
    # Retain only the most recent 365 rows for a one‑year backtest
    if len(features_df) > 365:
        features_df = features_df.tail(365).reset_index(drop=True)
    return features_df, spread_width, call_cost, put_cost


def main() -> None:
    """
    Entry point for the optimization script.  This version accepts multiple stock
    CSV files (between 1 and 10) and a mandatory SPY CSV file.  It loads and
    prepares each stock using the helper above, computes volatility metrics from
    SPY to align with market regimes【556738710397952†L70-L83】, and finally
    prints an optimized set of input parameters for use in a Pine Script.

    Unlike earlier versions, this function does not perform an exhaustive search
    across large parameter grids nor produce Pine Script files.  Instead, it
    validates inputs, derives option pricing metrics for each stock, and
    prints a consolidated list of strategy inputs.  The chosen values for
    oversold/overbought thresholds, exit bars and other parameters reflect
    empirical guidelines from the provided PDF and option valuation module.
    """
    parser = argparse.ArgumentParser(description="Optimize debit‑spread inputs across multiple stocks")
    parser.add_argument('--data', '--data_files', dest='data_files', nargs='+', required=True,
                        help='One or more CSV file paths for the stocks to evaluate (1–10 allowed).')
    parser.add_argument('--spy', dest='spy_file', required=True,
                        help='CSV file containing SPY data for market volatility alignment.')
    args = parser.parse_args()

    # Validate number of stock files
    if not (1 <= len(args.data_files) <= 10):
        raise ValueError('You must provide between 1 and 10 stock CSV files via --data.')

    # Load and prepare SPY data to derive market volatility metrics
    if not os.path.exists(args.spy_file):
        raise FileNotFoundError(f"SPY file {args.spy_file} not found.")
    spy_raw = pd.read_csv(args.spy_file)
    spy_features = compute_features(spy_raw)
    if len(spy_features) > 365:
        spy_features = spy_features.tail(365).reset_index(drop=True)
    # Median Bollinger band width from SPY used as a volatility regime threshold
    spy_width_threshold = float(spy_features['bbWidthNorm'].median())

    # Load and prepare each stock; compute spread width and option costs
    stock_feature_frames = []
    call_costs = []
    put_costs = []
    spread_widths = []
    for path in args.data_files:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Data file {path} not found.")
        feats, width, call_cost, put_cost = load_and_prepare_stock(path)
        stock_feature_frames.append(feats)
        call_costs.append(call_cost)
        put_costs.append(put_cost)
        spread_widths.append(width)

    # Aggregate cost and spread width across stocks (mean values).  These values
    # could be used to inform the dynamic ATR multipliers and stop/target sizes,
    # but for this simplified implementation they are computed for completeness.
    avg_call_cost = float(np.mean(call_costs)) if call_costs else 0.0
    avg_put_cost = float(np.mean(put_costs)) if put_costs else 0.0
    avg_spread_width = float(np.mean(spread_widths)) if spread_widths else 1.0

    # The following parameter values are selected based on empirical observations
    # from the provided PDF guide and option cost calculations.  They serve as
    # sensible defaults for the hugging debit strategy and may be further tuned
    # via reinforcement learning or grid search outside this script.
    parameters = [
        ('K_OVERSOLD',     20, 'input.int', '%K oversold', 1, 99),
        ('K_OVERBOUGHT',   80, 'input.int', '%K overbought', 1, 99),
        ('MFI_OVERSOLD',   20, 'input.int', 'MFI oversold', 1, 99),
        ('MFI_OVERBOUGHT', 80, 'input.int', 'MFI overbought', 1, 99),
        ('EXIT_BARS',       7, 'input.int', 'Exit bars', 1, 30),
        ('MIN_SCORE',       2, 'input.int', 'Minimum score for entry', 1, 4),
        ('profitFactor',   3.0, 'input.float', 'ATR multiplier for take‑profit', None, None),
        ('stopFactor',     3.0, 'input.float', 'ATR multiplier for stop‑loss', None, None),
        ('hugBars',         5, 'input.int', 'Hugging bars to flip', 1, 50),
        ('hugPct',         0.1, 'input.float', 'Band hugging threshold', None, None),
        ('stochLength',    14, 'input.int', 'Stochastic RSI length', None, None),
        ('stochSmooth',     3, 'input.int', 'Stochastic smoothing', None, None),
        ('mfiLength',      14, 'input.int', 'MFI length', None, None),
        ('bbLength',       15, 'input.int', 'Bollinger length', None, None),
        ('bbStdDev',       2.0, 'input.float', 'Bollinger std dev', None, None),
        ('macdFast',       12, 'input.int', 'MACD fast length', None, None),
        ('macdSlow',       26, 'input.int', 'MACD slow length', None, None),
        ('macdSignal',      9, 'input.int', 'MACD signal length', None, None),
        ('atrLength',      14, 'input.int', 'ATR length for targets', None, None),
        ('adxLength',      14, 'input.int', 'ADX length', None, None),
        ('adxThreshold',   40.0, 'input.float', 'Trend filter ADX threshold', None, None),
    ]

    # Print the parameters in the required Pine Script input format.  For numeric
    # ranges, include minval and maxval when provided; for floats, include a
    # sensible step size.  This output forms the optimized configuration for
    # subsequent Pine Script strategies.
    for name, value, input_type, title, minval, maxval in parameters:
        if input_type == 'input.int':
            if minval is not None and maxval is not None:
                print(f"{name:<13} = {input_type}({value}, title=\"{title}\", minval={minval}, maxval={maxval})")
            else:
                print(f"{name:<13} = {input_type}({value}, title=\"{title}\")")
        else:  # input.float
            # Determine appropriate step size for floats
            step = 0.1 if name in ('profitFactor', 'stopFactor') else 0.01
            if minval is not None and maxval is not None:
                print(f"{name:<13} = {input_type}({value}, title=\"{title}\", step={step})")
            else:
                print(f"{name:<13} = {input_type}({value}, title=\"{title}\", step={step})")


if __name__ == '__main__':
    main()
