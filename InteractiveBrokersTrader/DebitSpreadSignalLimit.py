import math
import os
import csv
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
from ib_insync import IB, Stock, Option
from scipy.stats import norm
import sys

from collections import defaultdict

def _write_alerts_csv_from_combined_df(out_dir, combined_df):
    """
    Create a TradingView-style alerts CSV (Alert ID, Ticker, Time, Description)
    for rows that produced an actionable signal (non-HOLD). The file is saved as:
        out_dir / "TradingView_Alerts_for_batch.csv"
    """
    try:
        import pandas as _pd
        from pathlib import Path as _Path
        alerts = []
        if combined_df is None or combined_df.empty:
            return None
        # Normalize columns defensively
        df = combined_df.copy()
        df.columns = [str(c) for c in df.columns]
        # Only actionable signals
        valid = {"CALL_OPEN", "PUT_OPEN", "CLOSE", "CALL_CLOSE", "PUT_CLOSE"}
        df = df[df["signal_type"].astype(str).str.upper().isin(valid)]
        if df.empty:
            # Nothing to write today
            return None

        def _fmt_desc(row):
            sym = str(row.get("symbol", "")).upper()
            sig = str(row.get("signal_type", "")).upper()
            exp = str(row.get("expiration", "")) if "expiration" in row else ""
            atm = row.get("atm_strike", "")
            oc = row.get("otm_strike_call", "")
            op = row.get("otm_strike_put", "")
            if sig == "CALL_OPEN":
                leg = f"{atm}/{oc}" if atm != "" and oc != "" else ""
                return f"{sym}: OPEN CALL debit spread {leg} exp {exp}".strip()
            if sig == "PUT_OPEN":
                leg = f"{atm}/{op}" if atm != "" and op != "" else ""
                return f"{sym}: OPEN PUT debit spread {leg} exp {exp}".strip()
            if sig in {"CALL_CLOSE","PUT_CLOSE","CLOSE"}:
                side = "CALL" if sig == "CALL_CLOSE" else ("PUT" if sig == "PUT_CLOSE" else "ANY")
                return f"{sym}: CLOSE {side} spread exp {exp}".strip()
            return f"{sym}: HOLD"

        for _, row in df.iterrows():
            alerts.append({
                "Alert ID": "debit_spread_signal",
                "Ticker": str(row.get("symbol", "")).upper(),
                "Time": str(row.get("timestamp_ny", "")),
                "Description": _fmt_desc(row)
            })

        if not alerts:
            return None

        out_path = _Path(out_dir) / "TradingView_Alerts_for_batch.csv"
        _pd.DataFrame(alerts).to_csv(out_path, index=False)
        return str(out_path)
    except Exception as _e:
        # Silent failure by design; this helper must not break signal generation
        return None

def black_scholes_call(S, K, T, r, sigma):
    """Calculate Black-Scholes call option price"""
    d1 = (math.log(S/K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    
    call_price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return call_price

def black_scholes_put(S, K, T, r, sigma):
    """Calculate Black-Scholes put option price"""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S/K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    # N(-x) = 1 - N(x)
    put_price = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    return put_price

def calculate_debit_spread_value(S, long_strike, short_strike, T, r, sigma):
    """Calculate theoretical debit spread value"""
    long_call = black_scholes_call(S, long_strike, T, r, sigma)
    short_call = black_scholes_call(S, short_strike, T, r, sigma)
    
    spread_value = long_call - short_call
    return spread_value

def calculate_put_debit_spread_value(S, long_strike, short_strike, T, r, sigma):
    """Calculate theoretical PUT debit spread value (long higher strike put, short lower strike put)."""
    long_put  = black_scholes_put(S, long_strike, T, r, sigma)
    short_put = black_scholes_put(S, short_strike, T, r, sigma)
    return long_put - short_put


# ---- Helper: Inspect IB positions for held vertical debit spreads ----
def _held_verticals_from_ib(ib) -> dict:
    """
    Inspect IB positions and return a mapping:
      held[symbol]['C'] -> list of tuples (exp_str, long_strike, short_strike, qty_int)
      held[symbol]['P'] -> list of tuples (exp_str, long_strike, short_strike, qty_int)
    Convention:
      - CALL debit spread: +1 long at lower strike (ATM-ish), -1 short at higher strike
      - PUT  debit spread: +1 long at higher strike (ATM-ish), -1 short at lower strike
    """
    held = defaultdict(lambda: {'C': [], 'P': []})
    try:
        ib.reqPositions()
    except Exception:
        pass
    try:
        pos = ib.positions()
    except Exception:
        pos = []
    # Group option legs by (symbol, exp, right, strike) with net qty
    legs = defaultdict(float)  # key = (sym, exp, right, strike) -> net qty
    for p in pos or []:
        c = getattr(p, 'contract', None)
        if not c or getattr(c, 'secType', '') != 'OPT':
            continue
        sym = str(getattr(c, 'symbol', '')).upper()
        exp = str(getattr(c, 'lastTradeDateOrContractMonth', ''))
        right = str(getattr(c, 'right', '')).upper()
        strike = float(getattr(c, 'strike', 0.0))
        qty = float(getattr(p, 'position', 0.0))
        if right in ('C', 'P') and sym and exp and strike > 0 and qty != 0:
            legs[(sym, exp, right, strike)] += qty
    # Pair longs(+) and shorts(-) into verticals per (sym, exp, right)
    by_key = defaultdict(lambda: {'longs': defaultdict(float), 'shorts': defaultdict(float)})
    for (sym, exp, right, strike), q in legs.items():
        if q > 0:
            by_key[(sym, exp, right)]['longs'][strike] += q
        elif q < 0:
            by_key[(sym, exp, right)]['shorts'][strike] += abs(q)
    for (sym, exp, right), d in by_key.items():
        longs = sorted(d['longs'].items())
        shorts = sorted(d['shorts'].items())
        if not longs or not shorts:
            continue
        for ls, lq in longs:
            if right == 'C':
                # need a short with higher strike
                cands = [(ss, sq) for ss, sq in shorts if ss > ls and sq > 0]
            else:
                # PUT: need a short with lower strike
                cands = [(ss, sq) for ss, sq in shorts if ss < ls and sq > 0]
            if not cands:
                continue
            # choose closest strike pairing
            ss, sq = min(cands, key=lambda t: abs(t[0] - ls))
            qty = int(min(lq, sq))
            if qty <= 0:
                continue
            if right == 'C':
                held[sym]['C'].append((exp, float(ls), float(ss), qty))
            else:
                held[sym]['P'].append((exp, float(ls), float(ss), qty))
    return held


def _pinescript_exit_flags_for_today(close, lower, upper, hugLowerCount, hugUpperCount,
                                     fastMA, slowMA, macdLine, signalLine,
                                     k, mfi, adxValue,
                                     params) -> dict:
    """
    Compute a subset of Pine-style exit/flip conditions that do not require stored entry bar or TP/SL.
    Returns dict with keys: call_hug_flip, put_hug_flip, oppositeCall, oppositePut, strong_bear, strong_bull.
    """
    K_OVERSOLD = params['K_OVERSOLD']; K_OVERBOUGHT = params['K_OVERBOUGHT']
    MFI_OVERSOLD = params['MFI_OVERSOLD']; MFI_OVERBOUGHT = params['MFI_OVERBOUGHT']
    adxThreshold = params['adxThreshold']; hugBars = params['hugBars']
    last = close.index[-1]
    # dynamic thresholds by volatility regime (reuse same high/low as in main)
    basis = (upper + lower) / 2.0
    bbWidth = (upper - lower) / basis.replace(0, np.nan)
    bbWidthMA = bbWidth.rolling(20, min_periods=1).mean()
    hv = bool(bbWidth.loc[last] > bbWidthMA.loc[last])
    dynKLow    = max(5,  K_OVERSOLD  - 10) if hv else K_OVERSOLD
    dynKHigh   = min(100, K_OVERBOUGHT + 10) if hv else K_OVERBOUGHT
    dynMFILow  = max(5,  MFI_OVERSOLD  - 10) if hv else MFI_OVERSOLD
    dynMFIHigh = min(100, MFI_OVERBOUGHT + 10) if hv else MFI_OVERBOUGHT

    # Opposite signal heuristics (strong directional shift)
    oppositeCall = ((k.loc[last] > dynKHigh) or (mfi.loc[last] > dynMFIHigh)) and (macdLine.loc[last] < signalLine.loc[last])
    oppositePut  = ((k.loc[last] < dynKLow ) or (mfi.loc[last] < dynMFILow )) and (macdLine.loc[last] > signalLine.loc[last])
    # Hugging flip candidates
    call_hug_flip = (hugLowerCount.loc[last] >= hugBars) and (fastMA.loc[last] < slowMA.loc[last])
    put_hug_flip  = (hugUpperCount.loc[last] >= hugBars) and (fastMA.loc[last] > slowMA.loc[last])
    strong_bear = (fastMA.loc[last] < slowMA.loc[last]) and (adxValue.loc[last] >= adxThreshold)
    strong_bull = (fastMA.loc[last] > slowMA.loc[last]) and (adxValue.loc[last] >= adxThreshold)
    return {
        'call_hug_flip': bool(call_hug_flip),
        'put_hug_flip': bool(put_hug_flip),
        'oppositeCall': bool(oppositeCall),
        'oppositePut': bool(oppositePut),
        'strong_bear': bool(strong_bear),
        'strong_bull': bool(strong_bull),
    }

def generate_trade_signals(sector_data, symbols_txt=None, output_base=None, dte_target_days=21, risk_free_rate=0.05):
    """
    Generate today's debit-spread OPEN/CLOSE signals using logic aligned with the Pine script.
    If symbols_txt is provided, it should be a path to a text file listing symbols like 'NYSE:ABR, NASDAQ:PEP, ...'.
    Results are written to {OUTPUT_BASE}/{yy_mm_dd}/combined_listener_spreads.csv (appended/updated per symbol).
    """
    # --- Load symbols (optionally from a {exchange:ticker} list) ---
    def _load_symbols_from_txt(path):
        if not path or not os.path.exists(path):
            return None
        raw = open(path, "r", encoding="utf-8").read()
        parts = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
        syms = []
        for p in parts:
            if ":" in p:
                p = p.split(":", 1)[1]
            syms.append(p.strip().upper())
        # de-dup preserving order
        seen = set(); out = []
        for s in syms:
            if s and s not in seen:
                out.append(s); seen.add(s)
        return out

    # --- Paths / day folder ---
    from pathlib import Path
    from zoneinfo import ZoneInfo

    yy_mm_dd = datetime.now().strftime("%y_%m_%d")
    # Resolve base output directory:
    # 1) explicit argument wins
    # 2) OUTPUT_BASE env var
    # 3) per-OS default (macOS vs Windows)
    if output_base:
        default_base = output_base
    else:
        env_base = os.getenv("OUTPUT_BASE")
        if env_base and env_base.strip():
            default_base = env_base
        else:
            if sys.platform == "darwin":
                default_base = "/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Stock History and Backtesting Data"
            else:
                default_base = r"C:\OptionsHistory"
    base = Path(default_base)
    out_dir = base / yy_mm_dd
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "combined_listener_spreads.csv"

    # --- Determine symbol universe ---
    symbols_from_txt = _load_symbols_from_txt(symbols_txt)
    if symbols_from_txt:
        universe = symbols_from_txt
    else:
        universe = list(sector_data.keys())

    # --- Indicator helpers computed on 'data' DataFrame (expects columns: open, high, low, close, volume) ---
    def _ema(x, n): return x.ewm(span=n, adjust=False).mean()
    def _sma(x, n): return x.rolling(n, min_periods=n).mean()
    def _stdev(x, n): return x.rolling(n, min_periods=n).std()
    def _atr(df, n):
        h, l, c = df["high"], df["low"], df["close"]
        prev_c = c.shift(1)
        tr = np.maximum(h - l, np.maximum(abs(h - prev_c), abs(l - prev_c)))
        return _sma(tr, n)

    def _dmi_adx(df, n):
        h, l, c = df["high"], df["low"], df["close"]
        upMove = h.diff()
        downMove = -l.diff()
        plusDM = np.where((upMove > downMove) & (upMove > 0), upMove, 0.0)
        minusDM = np.where((downMove > upMove) & (downMove > 0), downMove, 0.0)
        tr = np.maximum(h - l, np.maximum(abs(h - c.shift(1)), abs(l - c.shift(1))))
        # Wilder smoothing
        trN = pd.Series(tr).ewm(alpha=1/n, adjust=False).mean()
        pDMN = pd.Series(plusDM).ewm(alpha=1/n, adjust=False).mean()
        mDMN = pd.Series(minusDM).ewm(alpha=1/n, adjust=False).mean()
        pDI = 100 * (pDMN / trN)
        mDI = 100 * (mDMN / trN)
        dx = (100 * (abs(pDI - mDI) / (pDI + mDI))).fillna(0.0)
        adx = dx.ewm(alpha=1/n, adjust=False).mean()
        return adx

    def _mfi(df, n):
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        raw = tp * df["volume"].astype(float)
        pos = np.where(tp > tp.shift(1), raw, 0.0)
        neg = np.where(tp < tp.shift(1), raw, 0.0)
        posN = pd.Series(pos).rolling(n, min_periods=n).sum()
        negN = pd.Series(neg).rolling(n, min_periods=n).sum()
        ratio = posN / negN.replace(0, np.nan)
        mfi = 100 - 100 / (1 + ratio)
        return mfi.bfill().fillna(50.0)

    # --- Connect to IB if we need live/IV data from sector_data gaps ---
    ib = IB()
    try:
        ib.connect("127.0.0.1", 7497, clientId=9021)
        ib.reqMarketDataType(4)  # delayed-frozen OK for offline pricing
    except Exception:
        pass  # operate with provided sector_data only if IB is unavailable

    # Snapshot of currently held vertical debit spreads in the IB account
    try:
        held = _held_verticals_from_ib(ib)
    except Exception:
        held = {}

    # --- CSV I/O: load existing rows so we can upsert per symbol ---
    existing = None
    if csv_path.exists():
        try:
            existing = pd.read_csv(csv_path)
        except Exception:
            existing = None

    rows = []
    for symbol in universe:
        sym_u = symbol.upper()
        # Acquire historical candles for indicators; prefer sector_data if it carries a DataFrame
        df = None
        data = sector_data.get(sym_u) or sector_data.get(symbol)
        if isinstance(data, pd.DataFrame):
            df = data.copy()
        else:
            # Try pulling ~250 trading days from IB if possible
            try:
                from ib_insync import util
                stk = Stock(sym_u, "SMART", "USD")
                ib.qualifyContracts(stk)
                bars = ib.reqHistoricalData(stk, endDateTime="", durationStr="1 Y", barSizeSetting="1 day",
                                            whatToShow="TRADES", useRTH=True, formatDate=1)
                if bars:
                    df = util.df(bars)[["open","high","low","close","volume"]].rename(columns=str.lower)
            except Exception:
                df = None
        if df is None or len(df) < 60:
            # Not enough data to compute indicators — skip gracefully
            continue

        # Tuned defaults aligned to Pine inputs requested
        K_OVERSOLD = 20; K_OVERBOUGHT = 73
        MFI_OVERSOLD = 20; MFI_OVERBOUGHT = 79
        EXIT_BARS = 8
        MIN_SCORE = 1
        profitFactor = 1.07; stopFactor = 2.33

        # Additional hugging inputs
        hugBars = 17; hugPct = 0.13

        # Indicator lengths
        stochLength = 18; stochSmooth = 10; mfiLength = 28
        bbLength = 30; bbStdDev = 1.6
        macdFast = 15; macdSlow = 23; macdSignal = 9
        atrLength = 24; adxLength = 13; adxThreshold = 52.0

        close = df["close"]
        high  = df["high"]; low = df["low"]; vol = df["volume"].astype(float)
        basis = _sma(close, bbLength)
        deviation = _stdev(close, bbLength)
        upper = basis + bbStdDev * deviation
        lower = basis - bbStdDev * deviation
        bbWidth = (upper - lower) / basis.replace(0, np.nan)
        bbWidthMA = _sma(bbWidth, 20)
        highVol = bbWidth > bbWidthMA

        # hugging zones & flags
        hugLower = lower + (upper - lower) * hugPct
        hugUpper = upper - (upper - lower) * hugPct
        isHugLower = close <= hugLower
        isHugUpper = close >= hugUpper

        # hugging counters (consecutive)
        def _consec(series):
            # length of the last consecutive True run
            grp = (series != series.shift()).cumsum()
            return series.groupby(grp).cumsum()
        hugLowerCount = _consec(isHugLower).fillna(0)
        hugUpperCount = _consec(isHugUpper).fillna(0)

        # RSI→Stoch-like %K (on RSI to mirror Stoch RSI intent)
        rsi = _ema((close.diff().clip(lower=0)).rolling(stochLength).mean() /
                   (close.diff().abs()).rolling(stochLength).mean(), stochSmooth).fillna(50.0)
        # simple proxy for %K using normalized close position in n-window
        lowest = close.rolling(stochLength).min()
        highest = close.rolling(stochLength).max()
        k_raw = (close - lowest) / (highest - lowest).replace(0, np.nan) * 100.0
        k = _sma(k_raw, stochSmooth).fillna(50.0)
        d = _sma(k, stochSmooth).fillna(50.0)

        mfi = _mfi(df, mfiLength)
        fastMA = _ema(close, macdFast)
        slowMA = _ema(close, macdSlow)
        macdLine = fastMA - slowMA
        signalLine = _ema(macdLine, macdSignal)
        adxValue = _dmi_adx(df, adxLength).fillna(0.0)
        atrValue = _atr(df, atrLength).bfill()

        # --- Adaptive thresholds and scores on the last row (today) ---
        last = df.index[-1]
        hv = bool(highVol.loc[last]) if last in highVol.index else False
        dynKLow    = max(5,  K_OVERSOLD  - 10) if hv else K_OVERSOLD
        dynKHigh   = min(100, K_OVERBOUGHT + 10) if hv else K_OVERBOUGHT
        dynMFILow  = max(5,  MFI_OVERSOLD  - 10) if hv else MFI_OVERSOLD
        dynMFIHigh = min(100, MFI_OVERBOUGHT + 10) if hv else MFI_OVERBOUGHT

        scoreBull = (1 if k.loc[last] < dynKLow else 0) + \
                    (1 if mfi.loc[last] < dynMFILow else 0) + \
                    (1 if close.loc[last] <= (lower.loc[last]) else 0) + \
                    (1 if macdLine.loc[last] > signalLine.loc[last] else 0)
        scoreBear = (1 if k.loc[last] > dynKHigh else 0) + \
                    (1 if mfi.loc[last] > dynMFIHigh else 0) + \
                    (1 if close.loc[last] >= (upper.loc[last]) else 0) + \
                    (1 if macdLine.loc[last] < signalLine.loc[last] else 0)

        trendBull = (fastMA.loc[last] >= slowMA.loc[last]) or (adxValue.loc[last] < adxThreshold)
        trendBear = (fastMA.loc[last] <= slowMA.loc[last]) or (adxValue.loc[last] < adxThreshold)

        bullCondition = (scoreBull >= MIN_SCORE) and trendBull
        bearCondition = (scoreBear >= MIN_SCORE) and trendBear

        # --- Strike selection (width buckets 1/2.5/5) ---
        spot = float(close.loc[last])
        if spot < 25: width = 1.0
        elif spot < 150: width = 2.5
        else: width = 5.0

        def _round_to_increment(x, inc):
            return round(round(x / inc) * inc, 2)

        atm = _round_to_increment(spot, width)
        call_otm = _round_to_increment(atm + width, width)
        put_otm  = _round_to_increment(atm - width, width)

        # --- Expiration target (nearest Friday around D+21) ---
        def _nearest_friday(days):
            target = datetime.now().date() + timedelta(days=days)
            # roll forward to Friday (weekday() Mon=0..Sun=6)
            while target.weekday() != 4:
                target += timedelta(days=1)
            return target
        exp_date = _nearest_friday(dte_target_days)
        exp_str  = exp_date.strftime("%Y%m%d")
        T_years  = max(1/365, (exp_date - datetime.now().date()).days / 365.0)

        # --- IV: try sector_data, else 20d HV proxy ---
        iv = None
        if isinstance(data, dict):
            iv = data.get("implied_volatility")
            if iv is not None:
                try: iv = float(iv)
                except: iv = None
        if iv is None:
            ret = close.pct_change()
            iv = float(ret.rolling(20).std().iloc[-1] * math.sqrt(252))
            iv = max(0.05, min(iv, 0.50))

        # --- Theoretical debit prices ---
        call_theo = calculate_debit_spread_value(spot, atm, call_otm, T_years, risk_free_rate, iv)
        put_theo  = calculate_put_debit_spread_value(spot, atm, put_otm,  T_years, risk_free_rate, iv)

        # --- Compose row mirroring combined_listener_spreads.csv fields used by PlaceAnOrder ---
        ts_ny = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
        sig = "HOLD"
        if bullCondition and not bearCondition:
            sig = "CALL_OPEN"
        elif bearCondition and not bullCondition:
            sig = "PUT_OPEN"

        row = {
            "timestamp_ny": ts_ny,
            "symbol": sym_u,
            "last": round(spot, 2),
            "expiration": exp_str,
            "atm_strike": atm,
            "otm_strike_call": call_otm,
            "otm_strike_put":  put_otm,
            "call_debit_theo_1": (round(call_theo, 2) if width == 1.0 else np.nan),
            "call_debit_theo_2_5": (round(call_theo, 2) if width == 2.5 else np.nan),
            "call_debit_theo_5": (round(call_theo, 2) if width == 5.0 else np.nan),
            "put_debit_theo_1": (round(put_theo, 2) if width == 1.0 else np.nan),
            "put_debit_theo_2_5": (round(put_theo, 2) if width == 2.5 else np.nan),
            "put_debit_theo_5": (round(put_theo, 2) if width == 5.0 else np.nan),
            "signal_type": sig,
            "strategy_position": (1 if sig == "CALL_OPEN" else (-1 if sig == "PUT_OPEN" else 0)),
        }
        rows.append(row)

        # ---- If we HOLD actual positions in IB, compute exit/flip conditions and emit CLOSE rows ----
        try:
            params = {
                'K_OVERSOLD': K_OVERSOLD, 'K_OVERBOUGHT': K_OVERBOUGHT,
                'MFI_OVERSOLD': MFI_OVERSOLD, 'MFI_OVERBOUGHT': MFI_OVERBOUGHT,
                'adxThreshold': adxThreshold, 'hugBars': hugBars
            }
            flags = _pinescript_exit_flags_for_today(
                close, lower, upper, hugLowerCount, hugUpperCount,
                fastMA, slowMA, macdLine, signalLine, k, mfi, adxValue, params
            )
            held_sym = held.get(sym_u, {}) if isinstance(held, dict) else {}
            # For each held CALL vertical, decide if we should CLOSE
            for (exp_held, longK, shortK, qty) in held_sym.get('C', []):
                # CLOSE conditions: opposite/bearish or hugging-flip to put
                if flags['oppositeCall'] or flags['call_hug_flip'] or flags['strong_bear']:
                    rows.append({
                        "timestamp_ny": ts_ny,
                        "symbol": sym_u,
                        "last": round(spot, 2),
                        "expiration": exp_held,
                        "atm_strike": float(longK),
                        "otm_strike_call": float(shortK),
                        "otm_strike_put":  np.nan,
                        "call_debit_theo_1": np.nan,
                        "call_debit_theo_2_5": np.nan,
                        "call_debit_theo_5": np.nan,
                        "put_debit_theo_1": np.nan,
                        "put_debit_theo_2_5": np.nan,
                        "put_debit_theo_5": np.nan,
                        "signal_type": "CALL_CLOSE",
                        "strategy_position": 0
                    })
            # For each held PUT vertical, decide if we should CLOSE
            for (exp_held, longK, shortK, qty) in held_sym.get('P', []):
                # CLOSE conditions: opposite/bullish or hugging-flip to call
                if flags['oppositePut'] or flags['put_hug_flip'] or flags['strong_bull']:
                    rows.append({
                        "timestamp_ny": ts_ny,
                        "symbol": sym_u,
                        "last": round(spot, 2),
                        "expiration": exp_held,
                        "atm_strike": float(longK),
                        "otm_strike_call": np.nan,
                        "otm_strike_put":  float(shortK),
                        "call_debit_theo_1": np.nan,
                        "call_debit_theo_2_5": np.nan,
                        "call_debit_theo_5": np.nan,
                        "put_debit_theo_1": np.nan,
                        "put_debit_theo_2_5": np.nan,
                        "put_debit_theo_5": np.nan,
                        "signal_type": "PUT_CLOSE",
                        "strategy_position": 0
                    })
        except Exception:
            # Never block open-signal generation if close calc fails
            pass

    # --- Upsert into today's combined_listener_spreads.csv ---
    out_df = pd.DataFrame(rows)
    if out_df.empty:
        # nothing to write
        return out_df

    if existing is not None and not existing.empty:
        # Keep any columns not provided today, but replace rows by symbol with today's computation
        ex = existing.copy()
        ex = ex[~ex["symbol"].astype(str).str.upper().isin(out_df["symbol"].astype(str).str.upper())]
        merged = pd.concat([ex, out_df], ignore_index=True)
    else:
        merged = out_df

    # Deterministic sort by symbol
    merged = merged.sort_values("symbol")
    merged.to_csv(csv_path, index=False)
    # Also emit a TradingView-style alerts CSV for symbols that produced signals today
    _write_alerts_csv_from_combined_df(out_dir, merged)
    return merged

if __name__ == "__main__":
    # Optional: read symbols from an attached candidate list (exchange prefixes allowed)
    symbols_file = os.getenv("SYMBOLS_FILE", os.path.join(os.path.dirname(__file__), "..", "Debit Spread Candidate_124ff.txt"))
    try:
        result = generate_trade_signals(
            sector_data={},  # historical will be pulled via IB if available
            symbols_txt=symbols_file,
            output_base=os.getenv("OUTPUT_BASE", r"C:\OptionsHistory"),
            dte_target_days=int(os.getenv("DTE_TARGET_DAYS", "21")),
            risk_free_rate=float(os.getenv("RISK_FREE_RATE", "0.05")),
        )
        from pathlib import Path as _Path
        yy_mm_dd = datetime.now().strftime("%y_%m_%d")
        base = os.getenv("OUTPUT_BASE", r"C:\OptionsHistory")
        if sys.platform == "darwin":
            base = os.getenv("OUTPUT_BASE", "/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Stock History and Backtesting Data")
        out_dir = _Path(base) / yy_mm_dd
        alerts_path = _write_alerts_csv_from_combined_df(out_dir, result)
        msg = f"Wrote {len(result)} row(s) (opens/closes) to {out_dir/'combined_listener_spreads.csv'}"
        if alerts_path:
            msg += f" and alerts CSV to {alerts_path}"
        print(msg)
    except Exception as e:
        print(f"Signal generation failed: {e}")
