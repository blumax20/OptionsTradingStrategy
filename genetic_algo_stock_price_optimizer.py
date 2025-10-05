"""
Genetic algorithm optimiser for the hugging debit spread strategy.

This script implements a simple genetic algorithm that tunes a subset of the
parameters exposed by the ``HuggingStrategy`` found in ``hugging_backtest.py``.
Rather than relying on Backtrader (which is unavailable in this environment),
the hugging strategy logic has been re‑implemented using `pandas` to allow
lightweight simulation of trades.  Candidates are scored on the probability
of profit (POP) achieved over the past year of daily price data for a given
stock ticker.

Usage example::

    python genetic_hugging_optimizer.py --data your_stock_data.csv --generations 100 --population 20

Instead of taking a ticker symbol, this script accepts an explicit path to a
CSV file via the ``--data`` flag.  The CSV must contain at least the
columns ``Date``, ``Open``, ``High``, ``Low``, ``Close`` and ``Volume`` with
dates in a format recognised by `pandas.to_datetime`.  Only the most recent
365 rows are used during optimisation.

The optimiser randomises, breeds and mutates candidate parameter sets over
``generations`` iterations.  At the end of the run the best parameter set
along with its POP is printed.  See the ``param_ranges`` dictionary for
details on which parameters are tuned and their allowed ranges.
"""

import argparse
import os
import random
from typing import Dict, List, Tuple, Union

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Helper functions for technical indicator calculations
def bollinger_bands(series: pd.Series, length: int, std_dev: float) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Return basis (mid), upper and lower Bollinger Bands for a price series."""
    basis = series.rolling(window=length, min_periods=1).mean()
    deviation = series.rolling(window=length, min_periods=1).std(ddof=0)
    upper = basis + std_dev * deviation
    lower = basis - std_dev * deviation
    return basis, upper, lower


def exponential_moving_average(series: pd.Series, span: int) -> pd.Series:
    """Return an exponential moving average."""
    return series.ewm(span=span, adjust=False).mean()


def wilder_rsi(series: pd.Series, length: int) -> pd.Series:
    """Return the Wilder's Relative Strength Index."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def stochastic_rsi(rsi: pd.Series, length: int, smooth: int) -> pd.Series:
    """Return the smoothed Stochastic RSI."""
    rsi_min = rsi.rolling(window=length, min_periods=1).min()
    rsi_max = rsi.rolling(window=length, min_periods=1).max()
    stoch = 100 * (rsi - rsi_min) / (rsi_max - rsi_min + 1e-9)
    return stoch.rolling(window=smooth, min_periods=1).mean()


def money_flow_index(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, length: int) -> pd.Series:
    """Compute the Money Flow Index (MFI)."""
    typical_price = (high + low + close) / 3
    raw_mf = typical_price * volume
    positive_flow = np.where(typical_price > typical_price.shift(1), raw_mf, 0)
    negative_flow = np.where(typical_price < typical_price.shift(1), raw_mf, 0)
    pos_sum = pd.Series(positive_flow).rolling(window=length, min_periods=1).sum()
    neg_sum = pd.Series(negative_flow).rolling(window=length, min_periods=1).sum()
    money_ratio = pos_sum / (neg_sum + 1e-9)
    mfi = 100 - 100 / (1 + money_ratio)
    return mfi


def average_true_range(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    """Compute the Average True Range (ATR)."""
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=length, min_periods=1).mean()


def average_directional_index(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    """Compute the Average Directional Index (ADX)."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)
    up_move = high - prev_high
    down_move = prev_low - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=length, min_periods=1).mean()
    plus_di = 100 * pd.Series(plus_dm).rolling(window=length, min_periods=1).sum() / (atr + 1e-9)
    minus_di = 100 * pd.Series(minus_dm).rolling(window=length, min_periods=1).sum() / (atr + 1e-9)
    dx = 100 * (plus_di - minus_di).abs() / ((plus_di + minus_di).abs() + 1e-9)
    adx = dx.rolling(window=length, min_periods=1).mean()
    return adx


# ---------------------------------------------------------------------------
def compute_fitness(df: pd.DataFrame, params: Dict[str, Union[int, float]]) -> float:
    """Evaluate a candidate's fitness by simulating the hugging strategy.

    Parameters
    ----------
    df : pandas.DataFrame
        DataFrame containing columns ``open``, ``high``, ``low``, ``close``, ``volume``.
    params : dict
        Mapping of strategy parameter names to values.

    Returns
    -------
    float
        Probability of profit (profitable trades / total trades).  If no trades
        occur the POP is 0.
    """
    close = df['close']
    high = df['high']
    low = df['low']
    volume = df['volume']

    # Bollinger Bands
    basis, upper, lower = bollinger_bands(close, params['bbLength'], params['bbStdDev'])
    width = upper - lower
    bb_width = width / basis
    bb_width_ma = bb_width.rolling(window=20, min_periods=1).mean()

    # RSI and Stochastic RSI
    rsi = wilder_rsi(close, params['stochLength'])
    k = stochastic_rsi(rsi, params['stochLength'], params['stochSmooth'])

    # Money Flow Index
    mfi = money_flow_index(high, low, close, volume, params['mfiLength'])

    # MACD components
    ema_fast = exponential_moving_average(close, params['macdFast'])
    ema_slow = exponential_moving_average(close, params['macdSlow'])
    macd_line = ema_fast - ema_slow
    signal_line = exponential_moving_average(macd_line, params['macdSignal'])

    # ATR and ADX
    atr = average_true_range(high, low, close, params['atrLength'])
    adx = average_directional_index(high, low, close, params['adxLength'])

    # State variables for trade management
    position = 0  # 0: flat, 1: long, -1: short
    call_entry_bar = None
    put_entry_bar = None
    call_entry_price = None
    put_entry_price = None
    call_take_profit = None
    call_stop_loss = None
    put_take_profit = None
    put_stop_loss = None
    hug_lower_count = 0
    hug_upper_count = 0
    total_trades = 0
    profitable_trades = 0

    # Iterate over bars
    for idx in range(len(df)):
        # Determine volatility regime
        high_vol = bb_width.iloc[idx] > bb_width_ma.iloc[idx]
        # Adaptive thresholding for stochastic RSI and MFI
        K_OS = params['K_OVERSOLD']
        K_OB = params['K_OVERBOUGHT']
        MFI_OS = params['MFI_OVERSOLD']
        MFI_OB = params['MFI_OVERBOUGHT']
        dyn_k_low = max(5, K_OS - 10) if high_vol else K_OS
        dyn_k_high = min(100, K_OB + 10) if high_vol else K_OB
        dyn_mfi_low = max(5, MFI_OS - 10) if high_vol else MFI_OS
        dyn_mfi_high = min(100, MFI_OB + 10) if high_vol else MFI_OB

        # Hugging zones
        width_i = width.iloc[idx]
        lower_band = lower.iloc[idx]
        upper_band = upper.iloc[idx]
        hug_lower = lower_band + width_i * params['hugPct']
        hug_upper = upper_band - width_i * params['hugPct']

        # Update hugging counters
        price = close.iloc[idx]
        if price <= hug_lower:
            hug_lower_count += 1
        else:
            hug_lower_count = 0
        if price >= hug_upper:
            hug_upper_count += 1
        else:
            hug_upper_count = 0

        # Compute scores
        bull_score = 0
        bear_score = 0
        if k.iloc[idx] < dyn_k_low:
            bull_score += 1
        if mfi.iloc[idx] < dyn_mfi_low:
            bull_score += 1
        if price <= lower_band:
            bull_score += 1
        if macd_line.iloc[idx] > signal_line.iloc[idx]:
            bull_score += 1
        if k.iloc[idx] > dyn_k_high:
            bear_score += 1
        if mfi.iloc[idx] > dyn_mfi_high:
            bear_score += 1
        if price >= upper_band:
            bear_score += 1
        if macd_line.iloc[idx] < signal_line.iloc[idx]:
            bear_score += 1

        # Trend filters
        trend_bull = (ema_fast.iloc[idx] >= ema_slow.iloc[idx]) or (adx.iloc[idx] < params['adxThreshold'])
        trend_bear = (ema_fast.iloc[idx] <= ema_slow.iloc[idx]) or (adx.iloc[idx] < params['adxThreshold'])
        bull_condition = (bull_score >= params['MIN_SCORE']) and trend_bull
        bear_condition = (bear_score >= params['MIN_SCORE']) and trend_bear
        current_bar = idx

        # Manage open long position
        if position > 0:
            # Flip from long to short when hugging lower band and fast EMA < slow EMA
            if (hug_lower_count >= params['hugBars']) and (ema_fast.iloc[idx] < ema_slow.iloc[idx]):
                pnl = price - call_entry_price
                total_trades += 1
                if pnl > 0:
                    profitable_trades += 1
                # Open short
                position = -1
                put_entry_bar = current_bar
                put_entry_price = price
                put_take_profit = price - params['profitFactor'] * atr.iloc[idx]
                put_stop_loss = price + params['stopFactor'] * atr.iloc[idx]
                # Reset long state
                call_entry_bar = None
                call_entry_price = None
                call_take_profit = None
                call_stop_loss = None
                hug_lower_count = 0
                hug_upper_count = 0
                continue
            # Exit conditions for long
            exit_k_high = (min(100, K_OB + 10) if high_vol else K_OB)
            exit_mfi_high = (min(100, MFI_OB + 10) if high_vol else MFI_OB)
            opposite_call = ((k.iloc[idx] > exit_k_high) or (mfi.iloc[idx] > exit_mfi_high)) and (macd_line.iloc[idx] < signal_line.iloc[idx])
            time_exceeded = (call_entry_bar is not None and current_bar >= call_entry_bar + params['EXIT_BARS'])
            extended_limit = (call_entry_bar is not None and current_bar >= call_entry_bar + params['EXIT_BARS'] * 2)
            should_exit_due_time = time_exceeded and (
                (ema_fast.iloc[idx] < ema_slow.iloc[idx] and adx.iloc[idx] >= params['adxThreshold']) or extended_limit
            )
            tp_hit = (price >= (call_take_profit if call_take_profit is not None else float('inf')))
            sl_hit = (price <= (call_stop_loss if call_stop_loss is not None else float('-inf')))
            if tp_hit or sl_hit or opposite_call or should_exit_due_time:
                pnl = price - call_entry_price
                total_trades += 1
                if pnl > 0:
                    profitable_trades += 1
                # Close long
                position = 0
                call_entry_bar = None
                call_entry_price = None
                call_take_profit = None
                call_stop_loss = None
                hug_lower_count = 0
                hug_upper_count = 0
                continue

        # Manage open short position
        elif position < 0:
            # Flip from short to long when hugging upper band and fast EMA > slow EMA
            if (hug_upper_count >= params['hugBars']) and (ema_fast.iloc[idx] > ema_slow.iloc[idx]):
                pnl = put_entry_price - price
                total_trades += 1
                if pnl > 0:
                    profitable_trades += 1
                # Open long
                position = 1
                call_entry_bar = current_bar
                call_entry_price = price
                call_take_profit = price + params['profitFactor'] * atr.iloc[idx]
                call_stop_loss = price - params['stopFactor'] * atr.iloc[idx]
                # Reset short state
                put_entry_bar = None
                put_entry_price = None
                put_take_profit = None
                put_stop_loss = None
                hug_lower_count = 0
                hug_upper_count = 0
                continue
            # Exit conditions for short
            exit_k_low = (max(5, K_OS - 10) if high_vol else K_OS)
            exit_mfi_low = (max(5, MFI_OS - 10) if high_vol else MFI_OS)
            opposite_put = ((k.iloc[idx] < exit_k_low) or (mfi.iloc[idx] < exit_mfi_low)) and (macd_line.iloc[idx] > signal_line.iloc[idx])
            time_exceeded = (put_entry_bar is not None and current_bar >= put_entry_bar + params['EXIT_BARS'])
            extended_limit = (put_entry_bar is not None and current_bar >= put_entry_bar + params['EXIT_BARS'] * 2)
            should_exit_due_time = time_exceeded and (
                (ema_fast.iloc[idx] > ema_slow.iloc[idx] and adx.iloc[idx] >= params['adxThreshold']) or extended_limit
            )
            tp_hit = (price <= (put_take_profit if put_take_profit is not None else float('-inf')))
            sl_hit = (price >= (put_stop_loss if put_stop_loss is not None else float('inf')))
            if tp_hit or sl_hit or opposite_put or should_exit_due_time:
                pnl = put_entry_price - price
                total_trades += 1
                if pnl > 0:
                    profitable_trades += 1
                # Close short
                position = 0
                put_entry_bar = None
                put_entry_price = None
                put_take_profit = None
                put_stop_loss = None
                hug_lower_count = 0
                hug_upper_count = 0
                continue

        # Flat: evaluate entry conditions
        else:
            if bull_condition:
                position = 1
                call_entry_bar = current_bar
                call_entry_price = price
                call_take_profit = price + params['profitFactor'] * atr.iloc[idx]
                call_stop_loss = price - params['stopFactor'] * atr.iloc[idx]
                hug_lower_count = 0
                hug_upper_count = 0
                continue
            elif bear_condition:
                position = -1
                put_entry_bar = current_bar
                put_entry_price = price
                put_take_profit = price - params['profitFactor'] * atr.iloc[idx]
                put_stop_loss = price + params['stopFactor'] * atr.iloc[idx]
                hug_lower_count = 0
                hug_upper_count = 0
                continue

    # Require a minimum number of trades (one per month) for a meaningful score
    if total_trades < 12:
        return 0.0
    return (profitable_trades / total_trades)


# ---------------------------------------------------------------------------
# Genetic algorithm implementation
ParameterRange = Tuple[Union[int, float], Union[int, float], type]


param_ranges: Dict[str, ParameterRange] = {
    # Oscillator overbought/oversold levels (restricted to conventional ranges)
    'K_OVERSOLD': (10, 30, int),
    'K_OVERBOUGHT': (70, 90, int),
    'MFI_OVERSOLD': (10, 30, int),
    'MFI_OVERBOUGHT': (70, 90, int),
    # Trade management
    'EXIT_BARS': (3, 15, int),
    'MIN_SCORE': (1, 4, int),
    # Profit & stop factors (ATR multipliers)
    'profitFactor': (1.0, 3.0, float),
    'stopFactor': (1.0, 3.0, float),
    # Hugging logic: restrict to sensible bands
    'hugBars': (1, 14, int),
    'hugPct': (0.05, 0.33, float),
    # Indicator lengths
    'stochLength': (10, 30, int),
    'stochSmooth': (3, 10, int),
    'mfiLength': (10, 30, int),
    'bbLength': (15, 30, int),
    'bbStdDev': (1.0, 3.0, float),
    'macdFast': (8, 20, int),
    'macdSlow': (20, 40, int),
    'macdSignal': (5, 15, int),
    'atrLength': (10, 30, int),
    'adxLength': (10, 30, int),
    'adxThreshold': (20.0, 60.0, float),
}


def random_individual() -> Dict[str, Union[int, float]]:
    """Generate a random individual within the defined parameter ranges."""
    indiv: Dict[str, Union[int, float]] = {}
    for name, (low, high, typ) in param_ranges.items():
        if typ is int:
            indiv[name] = random.randint(int(low), int(high))
        else:
            indiv[name] = random.uniform(float(low), float(high))
    # Ensure oversold thresholds are less than overbought
    if indiv['K_OVERSOLD'] >= indiv['K_OVERBOUGHT']:
        indiv['K_OVERSOLD'], indiv['K_OVERBOUGHT'] = sorted([indiv['K_OVERSOLD'], indiv['K_OVERBOUGHT']])
    if indiv['MFI_OVERSOLD'] >= indiv['MFI_OVERBOUGHT']:
        indiv['MFI_OVERSOLD'], indiv['MFI_OVERBOUGHT'] = sorted([indiv['MFI_OVERSOLD'], indiv['MFI_OVERBOUGHT']])
    # Ensure MACD slow is greater than fast
    if indiv['macdSlow'] <= indiv['macdFast']:
        indiv['macdFast'], indiv['macdSlow'] = sorted([indiv['macdFast'], indiv['macdSlow']])
    return indiv


def crossover(parent1: Dict[str, Union[int, float]], parent2: Dict[str, Union[int, float]]) -> Dict[str, Union[int, float]]:
    """Produce a child from two parents using uniform crossover."""
    child: Dict[str, Union[int, float]] = {}
    for key in param_ranges.keys():
        child[key] = parent1[key] if random.random() < 0.5 else parent2[key]
    # Fix ordering constraints after crossover
    if child['K_OVERSOLD'] >= child['K_OVERBOUGHT']:
        child['K_OVERSOLD'], child['K_OVERBOUGHT'] = sorted([child['K_OVERSOLD'], child['K_OVERBOUGHT']])
    if child['MFI_OVERSOLD'] >= child['MFI_OVERBOUGHT']:
        child['MFI_OVERSOLD'], child['MFI_OVERBOUGHT'] = sorted([child['MFI_OVERSOLD'], child['MFI_OVERBOUGHT']])
    if child['macdSlow'] <= child['macdFast']:
        child['macdFast'], child['macdSlow'] = sorted([child['macdFast'], child['macdSlow']])
    return child


def mutate(indiv: Dict[str, Union[int, float]], mutation_rate: float) -> None:
    """Mutate an individual in place with a given mutation rate."""
    for key, (low, high, typ) in param_ranges.items():
        if random.random() < mutation_rate:
            if typ is int:
                indiv[key] = random.randint(int(low), int(high))
            else:
                indiv[key] = random.uniform(float(low), float(high))
    # Enforce parameter ordering constraints
    if indiv['K_OVERSOLD'] >= indiv['K_OVERBOUGHT']:
        indiv['K_OVERSOLD'], indiv['K_OVERBOUGHT'] = sorted([indiv['K_OVERSOLD'], indiv['K_OVERBOUGHT']])
    if indiv['MFI_OVERSOLD'] >= indiv['MFI_OVERBOUGHT']:
        indiv['MFI_OVERSOLD'], indiv['MFI_OVERBOUGHT'] = sorted([indiv['MFI_OVERSOLD'], indiv['MFI_OVERBOUGHT']])
    if indiv['macdSlow'] <= indiv['macdFast']:
        indiv['macdFast'], indiv['macdSlow'] = sorted([indiv['macdFast'], indiv['macdSlow']])


def genetic_optimize(df: pd.DataFrame, population_size: int, generations: int, mutation_rate: float) -> Tuple[Dict[str, Union[int, float]], float]:
    """Run the genetic algorithm and return the best individual and its score."""
    # Initialize population
    population: List[Dict[str, Union[int, float]]] = [random_individual() for _ in range(population_size)]

    # Evaluate initial fitnesses
    fitness_cache: Dict[int, float] = {}
    def evaluate(indiv: Dict[str, Union[int, float]]) -> float:
        # Use id of object as key for caching
        key = id(indiv)
        if key not in fitness_cache:
            fitness_cache[key] = compute_fitness(df, indiv)
        return fitness_cache[key]

    for gen in range(generations):
        # Compute scores for current population
        scores = [evaluate(ind) for ind in population]
        # Select two best individuals (highest POP)
        ranked_pairs = sorted(zip(scores, population), key=lambda x: x[0], reverse=True)
        parents = [ranked_pairs[0][1], ranked_pairs[1][1]]
        # Verbose logging: print progress every 10 generations
        if (gen + 1) % max(1, generations // 10) == 0:
            best_score = ranked_pairs[0][0]
            print(f"Generation {gen+1}/{generations}: best POP = {best_score:.2%}")
        # Create new population via crossover and mutation
        new_population = []
        for _ in range(population_size):
            child = crossover(parents[0], parents[1])
            mutate(child, mutation_rate)
            new_population.append(child)
        population = new_population
        # Clear fitness cache to avoid leaking memory; fitness values will be recomputed
        fitness_cache.clear()

    # Final evaluation to pick best individual
    final_scores = [compute_fitness(df, ind) for ind in population]
    best_idx = int(np.argmax(final_scores))
    best_individual = population[best_idx]
    best_score = final_scores[best_idx]
    return best_individual, best_score


def genetic_optimize_multi(dfs: List[pd.DataFrame], population_size: int, generations: int, mutation_rate: float) -> Tuple[Dict[str, Union[int, float]], float]:
    """Genetic optimisation across multiple datasets.

    The fitness of a candidate is the arithmetic mean of the POP values across all
    datasets.  Candidates producing fewer than 12 trades in any dataset will
    receive a POP of zero for that dataset (as enforced by ``compute_fitness``).

    Parameters
    ----------
    dfs : list of pandas.DataFrame
        List of preprocessed price dataframes.
    population_size : int
        Number of individuals in the population.
    generations : int
        Number of generations to iterate.
    mutation_rate : float
        Probability of mutating each gene in a child.

    Returns
    -------
    best_individual : dict
        Parameter set achieving the highest average POP.
    best_score : float
        The highest average POP obtained.
    """
    # Initialise population
    population: List[Dict[str, Union[int, float]]] = [random_individual() for _ in range(population_size)]

    def evaluate(indiv: Dict[str, Union[int, float]]) -> float:
        pops = [compute_fitness(df, indiv) for df in dfs]
        # Avoid division by zero if no datasets
        return sum(pops) / len(pops) if pops else 0.0

    for gen in range(generations):
        scores = [evaluate(ind) for ind in population]
        ranked_pairs = sorted(zip(scores, population), key=lambda x: x[0], reverse=True)
        parents = [ranked_pairs[0][1], ranked_pairs[1][1]]
        if (gen + 1) % max(1, generations // 10) == 0:
            print(f"Generation {gen+1}/{generations}: best average POP = {ranked_pairs[0][0]:.2%}")
        new_population = []
        for _ in range(population_size):
            child = crossover(parents[0], parents[1])
            mutate(child, mutation_rate)
            new_population.append(child)
        population = new_population

    # Final evaluation
    final_scores = [evaluate(ind) for ind in population]
    best_idx = int(np.argmax(final_scores))
    best_individual = population[best_idx]
    best_score = final_scores[best_idx]
    return best_individual, best_score


# ---------------------------------------------------------------------------
def load_price_data_from_csv(path: str) -> pd.DataFrame:
    """Load price data from a CSV file.

    The CSV must include a date column (named ``Date`` or ``date``) and
    OHLCV columns (``Open``, ``High``, ``Low``, ``Close``, ``Volume``).  Only
    the most recent 365 rows are retained to limit backtesting to
    approximately one trading year.  The returned DataFrame will contain
    columns ``time``, ``open``, ``high``, ``low``, ``close`` and ``volume``.

    Parameters
    ----------
    path : str
        File system path to a CSV file.

    Returns
    -------
    pandas.DataFrame
        Preprocessed price data ready for the hugging strategy simulation.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"CSV file '{path}' does not exist")
    raw = pd.read_csv(path)
    # Build a case‑insensitive mapping of column names
    col_map = {c.lower(): c for c in raw.columns}
    # Determine the time column: prefer 'time', fallback to 'date'
    if 'time' in col_map:
        time_col = col_map['time']
        # Convert to integer seconds if not already
        time_series = raw[time_col]
        # If dtype is not integer, attempt conversion
        if not np.issubdtype(time_series.dtype, np.integer):
            # Attempt to parse as datetime and convert to epoch seconds
            time_series = pd.to_datetime(time_series).astype('int64') // 10 ** 9
        df_time = time_series.astype('int64')
    elif 'date' in col_map:
        date_col = col_map['date']
        df_time = pd.to_datetime(raw[date_col]).astype('int64') // 10 ** 9
    else:
        raise ValueError(
            f"CSV file '{path}' must contain either a 'Date'/'date' column or a 'time' column"
        )
    # Required OHLCV columns (case insensitive)
    required_cols = ['open', 'high', 'low', 'close', 'volume']
    missing = [col for col in required_cols if col not in col_map]
    if missing:
        raise ValueError(f"CSV file '{path}' is missing required columns: {missing}")
    # Assemble DataFrame with uniform column names
    df = pd.DataFrame({
        'time': df_time,
        'open': raw[col_map['open']],
        'high': raw[col_map['high']],
        'low': raw[col_map['low']],
        'close': raw[col_map['close']],
        'volume': raw[col_map['volume']],
    })
    # Retain only the most recent 365 rows
    if len(df) > 365:
        df = df.tail(365)
    return df.reset_index(drop=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Genetic optimiser for the hugging debit spread strategy. "
            "Either optimise on a single CSV using --data, or optimise across "
            "a folder of sector subdirectories using --folder."
        )
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        '--data',
        help=(
            "Path to a CSV file containing price data. The CSV must include "
            "columns: Date/time, Open, High, Low, Close and Volume."
        ),
    )
    group.add_argument(
        '--folder',
        help=(
            "Path to a date folder containing subfolders named by sector. "
            "Each sector subfolder should contain one or more CSV files for that sector."
        ),
    )
    parser.add_argument('--population', type=int, default=10, help="Population size (default: 10)")
    parser.add_argument('--generations', type=int, default=100, help="Number of generations (default: 100)")
    parser.add_argument('--mutation', type=float, default=0.1, help="Mutation rate (default: 0.1)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.data:
        # Single file optimisation
        try:
            df = load_price_data_from_csv(args.data)
        except Exception as e:
            print(f"Error loading data: {e}")
            return
        print(f"Loaded price data from '{args.data}' with {len(df)} rows")
        best_params, best_pop = genetic_optimize(df, args.population, args.generations, args.mutation)
        print("\nBest parameter set found:")
        output_order = [
            'K_OVERSOLD', 'K_OVERBOUGHT', 'MFI_OVERSOLD', 'MFI_OVERBOUGHT',
            'EXIT_BARS', 'MIN_SCORE', 'profitFactor', 'stopFactor',
            'hugBars', 'hugPct',
            'stochLength', 'stochSmooth', 'mfiLength', 'bbLength', 'bbStdDev',
            'macdFast', 'macdSlow', 'macdSignal', 'atrLength', 'adxLength', 'adxThreshold',
        ]
        for key in output_order:
            if key in best_params:
                print(f"  {key}: {best_params[key]}")
        print(f"\nProbability of profit (POP): {best_pop:.2%}")
    elif args.folder:
        # Multi-sector optimisation
        base_path = args.folder
        # Predefined sector names (custom 20‑sector taxonomy)
        sectors = [
            'Technology Services',
            'Electronic Technology',
            'Finance',
            'Health Technology',
            'Retail Trade',
            'Consumer Non-Durables',
            'Producer Manufacturing',
            'Energy Minerals',
            'Consumer Services',
            'Consumer Durables',
            'Utilities',
            'Non-Energy Minerals',
            'Industrial Services',
            'Transportation',
            'Commercial Services',
            'Process Industries',
            'Communications',
            'Health Services',
            'Distribution Services',
            'Miscellaneous',
            'Unknown',
        ]
        for sector in sectors:
            sector_path = os.path.join(base_path, sector)
            if not os.path.isdir(sector_path):
                print(f"Sector folder '{sector_path}' does not exist, skipping.")
                continue
            # Load all CSVs in this sector folder
            dfs: List[pd.DataFrame] = []
            for fname in os.listdir(sector_path):
                full_path = os.path.join(sector_path, fname)
                if os.path.isfile(full_path) and fname.lower().endswith('.csv'):
                    try:
                        df = load_price_data_from_csv(full_path)
                        dfs.append(df)
                    except Exception as e:
                        print(f"Failed to load '{full_path}': {e}")
            if not dfs:
                print(f"No valid CSV files found for sector '{sector}'. Skipping optimisation.")
                continue
            print(f"\nOptimising sector '{sector}' with {len(dfs)} datasets...")
            best_params, best_avg_pop = genetic_optimize_multi(dfs, args.population, args.generations, args.mutation)
            print(f"\nBest parameter set for sector '{sector}':")
            output_order = [
                'K_OVERSOLD', 'K_OVERBOUGHT', 'MFI_OVERSOLD', 'MFI_OVERBOUGHT',
                'EXIT_BARS', 'MIN_SCORE', 'profitFactor', 'stopFactor',
                'hugBars', 'hugPct',
                'stochLength', 'stochSmooth', 'mfiLength', 'bbLength', 'bbStdDev',
                'macdFast', 'macdSlow', 'macdSignal', 'atrLength', 'adxLength', 'adxThreshold',
            ]
            for key in output_order:
                if key in best_params:
                    print(f"  {key}: {best_params[key]}")
            print(f"\nAverage probability of profit (POP) across sector: {best_avg_pop:.2%}")


if __name__ == '__main__':
    main()
