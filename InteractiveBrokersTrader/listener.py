# listener.py
"""
A simple webhook listener that returns option data and a basic ATM debit spread.
Run this with `python listener.py` and expose it via ngrok or your own reverse proxy.
"""

from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from ib_insync import IB, Stock, Option, util
import logging
from typing import Optional, Tuple, List
import asyncio
import csv
from pathlib import Path
# Ensure os is imported before _preferred_md_type
import os
import sys
# --- single-instance guard (Windows-safe) ---
import socket, atexit
from pathlib import Path as _Path

_LOCK_DIR  = _Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "OptionsTradingStrategy"
_LOCK_DIR.mkdir(parents=True, exist_ok=True)
_LOCK_FILE = _LOCK_DIR / "listener.lock"

def _port_is_open(_host="127.0.0.1", _port=5001, _timeout=0.3):
    try:
        _s = socket.socket()
        _s.settimeout(_timeout)
        _s.connect((_host, _port))
        _s.close()
        return True
    except Exception:
        return False
    
# --- refuse to run under system Python (enforce venv/service) ---
try:
    if r"\Program Files\Python312\python.exe".lower() in sys.executable.lower():
        print("listener: system-python detected; exiting (use IB_Listener service / venv).", flush=True)
        sys.exit(0)
except Exception:
    pass
# --- prefer venv instance over system-Python when port is already serving ---
try:
    _is_system_py = r"\Program Files\Python312\python.exe".lower() in sys.executable.lower()
    if _is_system_py and _port_is_open():
        print("listener: system-python duplicate detected; exiting.", flush=True)
        sys.exit(0)
except Exception:
    pass

# If the lock exists and port is already serving, exit duplicate
try:
    if _LOCK_FILE.exists() and _port_is_open():
        print("listener: already running; exiting duplicate.", flush=True)
        sys.exit(0)
    with open(_LOCK_FILE, "w", encoding="ascii") as _f:
        _f.write(str(os.getpid()))
except Exception:
    pass

@atexit.register
def _cleanup_lock():
    try:
        if _LOCK_FILE.exists():
            with open(_LOCK_FILE, "r", encoding="ascii") as _f:
                _pid = _f.read().strip()
            if _pid == str(os.getpid()):
                _LOCK_FILE.unlink()
    except Exception:
        pass
# --- MD type selection (env override) and strike fallbacks ---
def _preferred_md_type() -> int:
    """Read preferred market data type from env (MARKET_DATA_TYPE), default to 1 (live)."""
    try:
        return int(os.getenv("MARKET_DATA_TYPE", "1"))
    except Exception:
        return 1

def _nearest_valid_strike(strikes_all: list[float], target: float, prefer: str = "above") -> float | None:
    """Pick nearest available strike to target. prefer 'above' or 'below' when tie/choice."""
    if not strikes_all:
        return None
    above = [s for s in strikes_all if s >= target]
    below = [s for s in strikes_all if s <= target]
    if prefer == "above":
        if above:
            return above[0]
        return below[-1] if below else None
    else:
        if below:
            return below[-1]
        return above[0] if above else None

# --- SecDef & strike helpers to force "odd if available" ---
from typing import Iterable
_EXCHANGE_TRY_ORDER: tuple[str, ...] = ('SMART','BOX','CBOE','ISE','NASDAQOM','PHLX','BATS','AMEX')

def _collect_secdef(ib: IB, symbol: str, con_id: int) -> tuple[list[str], list[float], list[str], list[str]]:
    """
    Returns (expirations, strikes, tradingClasses, multipliers) from reqSecDefOptParams.
    """
    params = ib.reqSecDefOptParams(symbol, '', 'STK', con_id)
    ib.sleep(0.25)
    expirations: list[str] = []
    strikes_all: list[float] = []
    trading_classes: list[str] = []
    multipliers: list[str] = []
    for p in params:
        if getattr(p, 'expirations', None):
            expirations.extend(p.expirations)
        if getattr(p, 'strikes', None):
            strikes_all.extend([float(s) for s in p.strikes if s])
        tc = getattr(p, 'tradingClass', None)
        if tc:
            trading_classes.append(tc)
        mul = getattr(p, 'multiplier', None)
        if mul:
            multipliers.append(str(mul))
    expirations = sorted(set(expirations))
    strikes_all = sorted({s for s in strikes_all if s > 0})
    trading_classes = sorted(set([t for t in trading_classes if t]))
    multipliers = sorted(set([m for m in multipliers if m]))
    return expirations, list(strikes_all), trading_classes, multipliers

def _pick_preferred_tc(symbol: str, tcs: Iterable[str]) -> str | None:
    for tc in tcs:
        if tc.upper() == symbol.upper():
            return tc
    return next(iter(tcs), None)

def _closest_existing(strikes_all: list[float], target: float) -> float:
    if not strikes_all:
        return target
    return min(strikes_all, key=lambda s: abs(s - target))

def _next_higher_existing(strikes_all: list[float], k: float) -> float:
    higher = [s for s in strikes_all if s > k]
    return higher[0] if higher else k

def _next_lower_existing(strikes_all: list[float], k: float) -> float:
    lower = [s for s in strikes_all if s < k]
    return lower[-1] if lower else k

def _qualify_with_fallback(ib: IB, symbol: str, expiry: str, strike: float, right: str, tradingClass: str | None, multiplier: str | None):
    """
    Try SMART first then common option exchanges; include tradingClass/multiplier when available.
    """
    last_exc: Exception | None = None
    for ex in _EXCHANGE_TRY_ORDER:
        opt = Option(symbol=symbol, lastTradeDateOrContractMonth=expiry, strike=float(strike),
                     right=right, exchange=ex, currency='USD')
        if tradingClass:
            opt.tradingClass = tradingClass
        if multiplier:
            opt.multiplier = multiplier
        try:
            q = ib.qualifyContracts(opt)
            if q:
                return q[0]
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc:
        raise last_exc
    raise RuntimeError(f"Could not qualify {symbol} {right} {strike} {expiry}")

# --- Normalize incoming symbols (strip exchange prefixes, timeframes, trailing punctuation) ---
def _clean_symbol(raw: str | None) -> str | None:
    """
    Examples:
      'BATS:EQH, 1D' -> 'EQH'
      'NYSE:OXY'     -> 'OXY'
      'NWSA.'        -> 'NWSA'
    Keeps letters, numbers, dots and hyphens (for tickers like BRK.B), trims junk.
    """
    if not raw or not isinstance(raw, str):
        return raw
    s = raw.strip().upper()
    # Cut at comma (drop timeframe like ", 1D")
    if ',' in s:
        s = s.split(',', 1)[0].strip()
    # Keep part after a colon (drop exchange prefix like 'BATS:')
    if ':' in s:
        s = s.split(':', 1)[1].strip()
    # Trim whitespace tokens like '1D' if present
    if ' ' in s:
        s = s.split()[0].strip()
    # Remove trailing punctuation like '.' or ':' or ';'
    while s and s[-1] in '.:;,/':
        s = s[:-1]
    # Final allowlist (A-Z 0-9 . -)
    import re as _re
    m = _re.match(r'^[A-Z0-9][A-Z0-9\.\-]*$', s)
    return s if m else s
from zoneinfo import ZoneInfo
import math
from typing import Dict
import re
import platform
from pathlib import Path
import time

# --- IB connection helper (robust connect + market data type) ---
_IB_CLIENT_BASE_ID = 42
_IB_MAX_RETRIES = 4
def _ensure_ib_connected(ib: IB, mkt_type: int = 4) -> None:
    """
    Ensure a stable connection to IB. If clientId is in-use or socket dropped,
    try a few nearby clientIds before giving up. Also sets the requested
    market data type (1=live, 2=frozen, 3=delayed, 4=delayed-frozen).
    """
    if ib.isConnected():
        try:
            ib.reqMarketDataType(int(mkt_type))
        except Exception:
            pass
        return
    last_exc: Exception | None = None
    for i in range(_IB_MAX_RETRIES):
        cid = _IB_CLIENT_BASE_ID + i
        try:
            ib.connect('127.0.0.1', 7497, clientId=cid)
            try:
                ib.reqMarketDataType(int(mkt_type))
            except Exception:
                pass
            return
        except Exception as exc:
            last_exc = exc
            try:
                ib.disconnect()
            except Exception:
                pass
            util.sleep(0.25)
    if last_exc:
        raise last_exc

def _default_output_base() -> Path:
    # 1) Allow env var override on any machine
    env = os.getenv("OUTPUT_BASE")
    if env and env.strip():
        return Path(env).expanduser()

    # 2) OS-specific sensible defaults
    if os.name == "nt":  # Windows (VPS)
        return Path(r"C:\OptionsHistory")
    else:                # macOS / Linux
        # use your existing Mac path; change if you prefer ~/OptionsHistory
        return Path("/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Stock History and Backtesting Data")

OUTPUT_BASE = _default_output_base()
OUTPUT_BASE.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)


VERSION = "listener-2025-09-12d"

# Expiration selection preferences
TARGET_DTE = 30     # target days to expiration
MIN_DTE = 21        # require at least >20 days (i.e., 21+) for new positions

# === Black–Scholes helpers and NaN guard ===
def _is_nan(x) -> bool:
    try:
        if x is None:
            return True
        if isinstance(x, str) and x.strip().lower() == "nan":
            return True
        if isinstance(x, float) and (math.isnan(x) or x in (float("inf"), float("-inf"))):
            return True
    except Exception:
        return False
    return False

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _bs_price(S: float, K: float, T: float, r: float, sigma: float, call: bool = True) -> float:
    if T <= 0 or sigma <= 0:
        intrinsic = max(S - K, 0.0) if call else max(K - S, 0.0)
        return intrinsic
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    if call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _theo_spread_debits(S: float, atm: float, T: float, sigma: float, r: float = 0.045, widths=(1.0, 2.5, 5.0)) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for W in widths:
        call_long = _bs_price(S, atm, T, r, sigma, call=True)
        call_short = _bs_price(S, atm + W, T, r, sigma, call=True)
        put_long  = _bs_price(S, atm, T, r, sigma, call=False)
        put_short = _bs_price(S, max(atm - W, 0.01), T, r, sigma, call=False)
        key = "2_5" if abs(W - 2.5) < 1e-9 else str(int(W))
        out[f"call_debit_theo_{key}"] = float(call_long - call_short)
        out[f"put_debit_theo_{key}"]  = float(put_long - put_short)
    return out

# --- Parse TradingView/alert message for signal side and strategy position ---
def _parse_signal_fields(message: str | None) -> Dict[str, object]:
    """
    Parse a free-form alert message to extract:
      - signal_side: 'buy' or 'sell' (if present)
      - strategy_position: signed int (e.g., 1, -1, 0)
      - signal_type: 'CALL_OPEN' (pos>0), 'PUT_OPEN' (pos<0), 'CLOSE' (pos==0), or None
      - raw_message: original message string
    We infer CALL vs PUT from the sign of strategy position per user's convention.
    """
    result: Dict[str, object] = {
        "signal_side": None,
        "strategy_position": None,
        "signal_type": None,
        "raw_message": message
    }
    if not message or not isinstance(message, str):
        return result
    text = message.strip()
    low = text.lower()
    # side
    m_side = re.search(r'\border\s+(buy|sell)\b', low)
    if m_side:
        result["signal_side"] = m_side.group(1)
    # position number, tolerating spaces like "- 1"
    m_pos = re.search(r'(?:^|[\.!\s])\s*new\s+strategy\s+position\s+is\s*([+-]?)\s*(\d+)', low)
    if m_pos:
        sign = -1 if m_pos.group(1) == '-' else 1
        num = int(m_pos.group(2))
        pos = sign * num
        result["strategy_position"] = pos
        if pos == 0:
            result["signal_type"] = "CLOSE"
        elif pos > 0:
            result["signal_type"] = "CALL_OPEN"
        else:
            result["signal_type"] = "PUT_OPEN"
    return result


def _dated_dir() -> Path:
    try:
        now_ny = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now_ny = datetime.utcnow()
    dated = now_ny.strftime("%y_%m_%d")
    d = OUTPUT_BASE / dated
    d.mkdir(parents=True, exist_ok=True)
    return d

def _combined_csv() -> Path:
    return _dated_dir() / "combined_listener_spreads.csv"

def _webhook_jsonl() -> Path:
    """
    Returns the dated JSON Lines file under the same OUTPUT_BASE date folder, e.g.
    C:\\OptionsHistory\\YY_MM_DD\\webhooks.jsonl
    """
    return _dated_dir() / "webhooks.jsonl"

def _append_webhook_event(payload: dict, route: str) -> None:
    """
    Persist the raw inbound webhook payload (plus route & timestamp) as JSONL.
    Never throws; logs on failure.
    """
    try:
        from json import dumps as _dumps
        try:
            ts = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        rec = {"ts": ts, "route": route, "payload": payload}
        out = _webhook_jsonl()
        with out.open("a", encoding="utf-8") as f:
            f.write(_dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[WEBHOOK_JSONL] failed to append: {e}")

def _append_csv_row(row: dict):
    """
    Append a row to the single combined daily CSV (rounding numeric fields to 2 decimals).
    """
    out_csv = _combined_csv()
    headers = [
        "timestamp_ny","symbol","current_price","expiration","days_to_exp",
        "atm_strike","otm_strike_call","otm_strike_put","iv_atm","iv_otm",
        "call_debit_limit","put_debit_limit",
        "call_debit_limit_1","put_debit_limit_1",
        "call_debit_limit_2_5","put_debit_limit_2_5",
        "call_debit_limit_5","put_debit_limit_5",
        "call_debit_theo_1","put_debit_theo_1",
        "call_debit_theo_2_5","put_debit_theo_2_5",
        "call_debit_theo_5","put_debit_theo_5",
        "open_interest_atm_call","open_interest_otm_call",
        "open_interest_atm_put","open_interest_otm_put",
        "signal_side","signal_type","strategy_position","raw_message"
    ]
    write_header = not out_csv.exists()
    with out_csv.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        if write_header:
            w.writeheader()
        # round numeric values to 2 decimals
        rounded_row = {}
        for k, v in row.items():
            if isinstance(v, (int, float)) and v is not None:
                try:
                    rounded_row[k] = round(v, 2)
                except Exception:
                    rounded_row[k] = v
            else:
                rounded_row[k] = v
        w.writerow(rounded_row)
    logger.info(f"[CSV] Appended row to {out_csv}")

def _append_listener_result_to_csv(result: dict, signal_fields: Dict[str, object] | None = None):
    """
    Normalize fields and append a row to combined dated CSV with quote-based and theoretical $1/$2.5/$5 spreads."
    Optionally includes TradingView signal fields.
    """
    signal_fields = signal_fields or {}
    symbol = (result.get("symbol") or "UNKNOWN").upper()
    # Quote-based base debits (may be None/NaN)
    call_debit_q = None if _is_nan(result.get("call_debit")) else result.get("call_debit")
    put_debit_q  = None if _is_nan(result.get("put_debit"))  else result.get("put_debit")
    if call_debit_q is None and "debit_spread" in result:
        call_debit_q = None if _is_nan(result.get("debit_spread")) else result.get("debit_spread")

    # Timestamp & days to expiry
    try:
        ts_ny = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        ts_ny = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    days_to_exp = None
    T = 0.0
    try:
        exp_dt = datetime.strptime(result.get("expiration",""), "%Y%m%d").date()
        today_ny = datetime.now(ZoneInfo("America/New_York")).date()
        days_to_exp = max((exp_dt - today_ny).days, 0)
        T = days_to_exp/365.0
    except Exception:
        pass

    # Theo inputs (fallback for IV)
    S   = result.get("current_price")
    atm = result.get("atm_strike")
    iv_atm = result.get("implied_volatility_atm")
    iv_otm = result.get("implied_volatility_otm")
    sigma = None
    if not _is_nan(iv_atm):
        try: sigma = float(iv_atm)
        except Exception: sigma = None
    if sigma is None and not _is_nan(iv_otm):
        try: sigma = float(iv_otm)
        except Exception: sigma = None
    if sigma is None or _is_nan(sigma):
        sigma = 0.25

    theo = {"call_debit_theo_1": None,"put_debit_theo_1": None,"call_debit_theo_2_5": None,"put_debit_theo_2_5": None,"call_debit_theo_5": None,"put_debit_theo_5": None}
    if not _is_nan(S) and not _is_nan(atm) and T is not None:
        try:
            theo = _theo_spread_debits(float(S), float(atm), float(T), float(sigma))
        except Exception as e:
            logger.warning(f"[THEO] pricing error: {e}")

    row = {
        "timestamp_ny": ts_ny,
        "symbol": symbol,
        "current_price": None if _is_nan(S) else S,
        "expiration": result.get("expiration"),
        "days_to_exp": days_to_exp,
        "atm_strike": None if _is_nan(atm) else atm,
        "otm_strike_call": result.get("otm_strike"),
        "otm_strike_put": result.get("put_otm_strike"),
        "iv_atm": None if _is_nan(iv_atm) else iv_atm,
        "iv_otm": None if _is_nan(iv_otm) else iv_otm,
        "call_debit_limit": call_debit_q,
        "put_debit_limit":  put_debit_q,
        "call_debit_limit_1": result.get("call_debit_limit_1"),
        "put_debit_limit_1":  result.get("put_debit_limit_1"),
        "call_debit_limit_2_5": result.get("call_debit_limit_2_5"),
        "put_debit_limit_2_5":  result.get("put_debit_limit_2_5"),
        "call_debit_limit_5": result.get("call_debit_limit_5"),
        "put_debit_limit_5":  result.get("put_debit_limit_5"),
        "call_debit_theo_1": (theo or {}).get("call_debit_theo_1"),
        "put_debit_theo_1":  (theo or {}).get("put_debit_theo_1"),
        "call_debit_theo_2_5": (theo or {}).get("call_debit_theo_2_5"),
        "put_debit_theo_2_5":  (theo or {}).get("put_debit_theo_2_5"),
        "call_debit_theo_5": (theo or {}).get("call_debit_theo_5"),
        "put_debit_theo_5":  (theo or {}).get("put_debit_theo_5"),
        "open_interest_atm_call": result.get("open_interest_atm"),
        "open_interest_otm_call": result.get("open_interest_otm"),
        "open_interest_atm_put": result.get("open_interest_atm_put"),
        "open_interest_otm_put": result.get("open_interest_otm_put"),
        "signal_side": signal_fields.get("signal_side"),
        "signal_type": signal_fields.get("signal_type"),
        "strategy_position": signal_fields.get("strategy_position"),
        "raw_message": signal_fields.get("raw_message"),
    }
    _append_csv_row(row)

# Ensure an asyncio event loop exists in the main thread for ib_insync
util.startLoop()
IB_SHARED = IB()

def _fail(stage: str, msg: str, status: int = 500):
    logger.error(f"[{stage}] {msg}")
    return jsonify({"error": msg, "stage": stage}), status

def get_option_data(symbol: str, width: int = 5):
    # Normalize any malformed symbol (e.g., 'NWSA.', 'BATS:EQH, 1D')
    symbol = _clean_symbol(symbol)
    ib = IB_SHARED
    stage = "connect"
    try:
        if not ib.isConnected():
            ib.connect('127.0.0.1', 7497, clientId=42)  # paper trading port
    except Exception as exc:
        logger.exception("IB connect failed")
        return {"_error": True, "stage": stage, "detail": f"Could not connect to IB API: {exc}"}

    stage = "market_data_type"
    try:
        ib.reqMarketDataType(_preferred_md_type())  # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
    except Exception as exc:
        logger.warning(f"Could not set market data type: {exc}")

    # Define the stock and try to get a current price; fall back to historical
    stage = "stock_price"
    stock = Stock(symbol, 'SMART', 'USD')
    try:
        ticker_stock = ib.reqMktData(stock, '', False, False)
        ib.sleep(1.0)
    except Exception as exc:
        logger.warning(f"reqMktData stock failed: {exc}")
        ticker_stock = None

    current_price = None
    if ticker_stock:
        if getattr(ticker_stock, 'last', None) and not str(ticker_stock.last) == 'nan':
            current_price = float(ticker_stock.last)
        elif getattr(ticker_stock, 'close', None) and not str(ticker_stock.close) == 'nan':
            current_price = float(ticker_stock.close)

    if current_price is None:
        # fallback to 1-day historical bar close (after-hours friendly)
        try:
            bars = ib.reqHistoricalData(
                stock, endDateTime='', durationStr='1 D', barSizeSetting='1 day',
                whatToShow='TRADES', useRTH=False, formatDate=1
            )
            if (not bars) or (hasattr(bars[-1], "close") and str(bars[-1].close) == "nan"):
                # second try with BID_ASK in case TRADES is unavailable
                bars = ib.reqHistoricalData(
                    stock, endDateTime='', durationStr='1 D', barSizeSetting='1 day',
                    whatToShow='BID_ASK', useRTH=False, formatDate=1
                )
            if bars:
                try:
                    current_price = float(bars[-1].close)
                except Exception:
                    current_price = None
        except Exception as exc:
            logger.warning(f"Historical data fallback failed: {exc}")

    if current_price is None:
        return {"_error": True, "stage": stage, "detail": "Unable to determine current price"}

    # Get contract details / conId
    stage = "contract_details"
    details = ib.reqContractDetails(stock)
    if not details:
        return {"_error": True, "stage": stage, "detail": "No contract details returned for underlying"}

    con_id = details[0].contract.conId

    # Get expirations, strikes, tradingClass & multiplier from SecDef (enables odd strikes if listed)
    stage = "secdef"
    expirations, strikes_all, trading_classes, multipliers = _collect_secdef(ib, symbol, con_id)
    preferred_tc = _pick_preferred_tc(symbol, trading_classes)
    multiplier = multipliers[0] if multipliers else None

    if not expirations:
        return {"_error": True, "stage": stage, "detail": "No expirations available"}

    # Pick expiry nearest to 30 calendar days
    stage = "pick_expiry"
    target_date = datetime.now().date()
    try:
        # Build list of (exp_str, dte) tuples
        exps = []
        for d in expirations:
            try:
                ed = datetime.strptime(d, '%Y%m%d').date()
                dte = (ed - target_date).days
                exps.append((d, dte))
            except Exception:
                continue
        if not exps:
            return {"_error": True, "stage": stage, "detail": "No valid expiration dates parsed"}

        # Prefer expirations with DTE >= MIN_DTE, then pick nearest to TARGET_DTE
        valid = [(d, dte) for (d, dte) in exps if dte >= MIN_DTE]
        if valid:
            expiry_str = min(valid, key=lambda t: abs(t[1] - TARGET_DTE))[0]
        else:
            # Fallback: original behavior (nearest to TARGET_DTE ignoring the threshold)
            expiry_str = min(exps, key=lambda t: abs(t[1] - TARGET_DTE))[0]
    except Exception as exc:
        return {"_error": True, "stage": stage, "detail": f"Failed to choose expiry: {exc}"}

    # Choose ATM strike from available strikes (closest to current_price)
    stage = "pick_strikes"
    if not strikes_all:
        atm_strike = round(current_price)
        otm_strike = atm_strike + width
    else:
        # Force "odd if available": always pick from existing strikes list
        atm_strike = _closest_existing(strikes_all, current_price)
        otm_strike = _next_higher_existing(strikes_all, atm_strike)

    # Qualify and request option mkt data for both call legs (ATM long, OTM short)
    stage = "qualify_options"
    legs_info = []
    call_strikes = [atm_strike, otm_strike]
    for idx, strike in enumerate(call_strikes):
        try:
            option = _qualify_with_fallback(ib, symbol, expiry_str, strike, 'C', preferred_tc, multiplier)
        except Exception as exc:
            # Fallback: pick nearest listed strike above, then below
            fallback = _nearest_valid_strike(strikes_all, strike, prefer="above")
            if fallback is None or abs(fallback - strike) < 1e-9:
                fallback = _nearest_valid_strike(strikes_all, strike, prefer="below")
            if fallback is None:
                return {"_error": True, "stage": stage, "detail": f"qualifyContracts failed for {symbol} {strike} {expiry_str}: {exc}"}
            try:
                option = _qualify_with_fallback(ib, symbol, expiry_str, fallback, 'C', preferred_tc, multiplier)
                strike = fallback  # use the valid strike
                if idx == 1:
                    otm_strike = strike
                else:
                    atm_strike = strike
            except Exception as exc2:
                return {"_error": True, "stage": stage, "detail": f"qualifyContracts failed for {symbol} {strike} {expiry_str}: {exc2}"}

        # Request option market data incl. generic ticks for OI (101) and IV30 (106)
        stage = "option_mktdata"
        try:
            t = ib.reqMktData(option, '101,106', False, False)
            ib.sleep(1.0)
        except Exception as exc:
            # If market data not subscribed (354), continue with no quotes; we will still compute theo later
            logger.warning(f"[option_mktdata] reqMktData failed for {symbol} {strike}C {expiry_str}: {exc}")
            t = None

        legs_info.append({
            "strike": strike,
            "bid": getattr(t, 'bid', None) if t else None,
            "ask": getattr(t, 'ask', None) if t else None,
            "impliedVolatility": getattr(t, 'impliedVolatility', None) if t else None,
            "callOpenInterest": getattr(t, 'callOpenInterest', None) if t else None
        })

    # --- Fetch put legs for a put debit spread (long ATM put, short lower strike put) ---
    stage = "qualify_put_options"
    put_width = abs(otm_strike - atm_strike)
    # Force "odd if available": pick nearest existing lower strike, not invented
    put_otm_strike = _next_lower_existing(strikes_all, atm_strike)
    put_legs_info = []
    for strike, right in ((atm_strike, 'P'), (put_otm_strike, 'P')):
        try:
            option = _qualify_with_fallback(ib, symbol, expiry_str, strike, right, preferred_tc, multiplier)
        except Exception as exc:
            return {"_error": True, "stage": stage, "detail": f"qualifyContracts failed for PUT {symbol} {strike} {expiry_str}: {exc}"}
        stage = "option_put_mktdata"
        try:
            t = ib.reqMktData(option, '101,106', False, False)
            ib.sleep(1.0)
        except Exception as exc:
            logger.warning(f"[option_put_mktdata] reqMktData failed for {symbol} {strike}P {expiry_str}: {exc}")
            t = None
        put_legs_info.append({
            "strike": strike,
            "bid": getattr(t, 'bid', None) if t else None,
            "ask": getattr(t, 'ask', None) if t else None,
            "impliedVolatility": getattr(t, 'impliedVolatility', None) if t else None,
            "putOpenInterest": getattr(t, 'putOpenInterest', None) if t else None
        })

    # Compute net debit limits from quotes
    stage = "compute_debit"
    call_buy_ask = legs_info[0]["ask"]
    call_sell_bid = legs_info[1]["bid"]
    call_debit = None
    if call_buy_ask is not None and call_sell_bid is not None:
        try:
            call_debit = float(call_buy_ask) - float(call_sell_bid)
        except Exception:
            call_debit = None

    put_buy_ask = put_legs_info[0]["ask"] if put_legs_info else None
    put_sell_bid = put_legs_info[1]["bid"] if put_legs_info else None
    put_debit = None
    if put_buy_ask is not None and put_sell_bid is not None:
        try:
            put_debit = float(put_buy_ask) - float(put_sell_bid)
        except Exception:
            put_debit = None

    # Quote-based debits for $1, $2.5 and $5 widths using closest available strikes
    stage = "limit_widths_quotes"
    call_debit_limit_1 = put_debit_limit_1 = None
    call_debit_limit_2_5 = put_debit_limit_2_5 = None
    call_debit_limit_5 = put_debit_limit_5 = None
    for W in (1.0, 2.5, 5.0):
        # Calls: long ATM C, short C at ATM+W (pick from listed strikes)
        try:
            c_long = _qualify_with_fallback(ib, symbol, expiry_str, atm_strike, 'C', preferred_tc, multiplier)
            target_c = atm_strike + W
            c_candidates = [s for s in strikes_all if s >= target_c]
            c_shortK = c_candidates[0] if c_candidates else _next_higher_existing(strikes_all, atm_strike)
            c_short = _qualify_with_fallback(ib, symbol, expiry_str, c_shortK, 'C', preferred_tc, multiplier)
            tcL = ib.reqMktData(c_long, '', False, False); ib.sleep(0.4)
            tcS = ib.reqMktData(c_short, '', False, False); ib.sleep(0.4)
            val = None
            if getattr(tcL, 'ask', None) is not None and getattr(tcS, 'bid', None) is not None:
                val = float(tcL.ask) - float(tcS.bid)
            if abs(W - 1.0) < 1e-9:
                call_debit_limit_1 = val
            elif abs(W - 2.5) < 1e-9:
                call_debit_limit_2_5 = val
            else:
                call_debit_limit_5 = val
        except Exception as e:
            logger.warning(f"[LIMIT] call W={W}: {e}")
        # Puts: long ATM P, short P at ATM-W (pick from listed strikes)
        try:
            p_long = _qualify_with_fallback(ib, symbol, expiry_str, atm_strike, 'P', preferred_tc, multiplier)
            target_p = max(atm_strike - W, 0.01)
            p_candidates = [s for s in strikes_all if s <= target_p]
            p_shortK = p_candidates[-1] if p_candidates else _next_lower_existing(strikes_all, atm_strike)
            p_short = _qualify_with_fallback(ib, symbol, expiry_str, p_shortK, 'P', preferred_tc, multiplier)
            tpL = ib.reqMktData(p_long, '', False, False); ib.sleep(0.4)
            tpS = ib.reqMktData(p_short, '', False, False); ib.sleep(0.4)
            val = None
            if getattr(tpL, 'ask', None) is not None and getattr(tpS, 'bid', None) is not None:
                val = float(tpL.ask) - float(tpS.bid)
            if abs(W - 1.0) < 1e-9:
                put_debit_limit_1 = val
            elif abs(W - 2.5) < 1e-9:
                put_debit_limit_2_5 = val
            else:
                put_debit_limit_5 = val
        except Exception as e:
            logger.warning(f"[LIMIT] put W={W}: {e}")

    return {
        "_error": False,
        "symbol": symbol,
        "current_price": current_price,
        "atm_strike": atm_strike,
        "otm_strike": otm_strike,
        "put_otm_strike": put_otm_strike,
        "expiration": expiry_str,
        "implied_volatility_atm": legs_info[0]['impliedVolatility'],
        "open_interest_atm": legs_info[0]['callOpenInterest'],
        "implied_volatility_otm": legs_info[1]['impliedVolatility'],
        "open_interest_otm": legs_info[1]['callOpenInterest'],
        "call_debit": call_debit,
        "put_debit": put_debit,
        "call_debit_limit_1": call_debit_limit_1,
        "put_debit_limit_1":  put_debit_limit_1,
        "call_debit_limit_2_5": call_debit_limit_2_5,
        "put_debit_limit_2_5":  put_debit_limit_2_5,
        "call_debit_limit_5": call_debit_limit_5,
        "put_debit_limit_5":  put_debit_limit_5,
    }

# --- Extract a ticker from free‑form alert text (very permissive but biased to ALLCAPS tickers) ---
_TICKER_RE = re.compile(r"\b([A-Z]{1,5})(?:\.[A-Z])?\b")

def _extract_ticker_from_text(text: str | None) -> str | None:
    if not text or not isinstance(text, str):
        return None
    s = text.strip().upper()
    # fast-path: "... filled on TICKER. New strategy position is -1"
    m_on = re.search(r"\bFILLED\s+ON\s+([A-Z]{1,5})(?:\.[A-Z])?\b", s)
    if m_on:
        tick = _clean_symbol(m_on.group(1))
        if tick:
            return tick
    # common patterns: "on PAYX", "ticker PAYX", "symbol PAYX"
    for pat in (r"\bon\s+([A-Z]{1,5})(?:\.[A-Z])?\b",
                r"\bticker\s+([A-Z]{1,5})(?:\.[A-Z])?\b",
                r"\bsymbol\s+([A-Z]{1,5})(?:\.[A-Z])?\b"):
        m = re.search(pat, s)
        if m:
            return _clean_symbol(m.group(1))
    # fallback: first ALLCAPS token that passes _clean_symbol
    for m in _TICKER_RE.finditer(s):
        tick = _clean_symbol(m.group(1))
        if tick:
            return tick
    return None


# --- Signal endpoints accepting text or flexible JSON payloads ---
@app.route('/signal/text', methods=['POST'])
def signal_text():
    data = request.get_json(silent=True) or {}
    _append_webhook_event(data, route="/signal/text")
    raw_text = data.get('text') or data.get('message') or data.get('alert')
    # allow explicit ticker override via ticker/symbol
    symbol = _clean_symbol(data.get('ticker') or data.get('symbol') or _extract_ticker_from_text(raw_text))
    if not symbol:
        return _fail("payload", "ticker missing and could not extract from text", 400)
    sig = _parse_signal_fields(raw_text)
    result = get_option_data(symbol)
    if not result or result.get("_error"):
        return jsonify({
            "_error": True,
            "stage": (result or {}).get("stage","unknown"),
            "detail": (result or {}).get("detail","unable to retrieve option data")
        }), 200
    _append_listener_result_to_csv(result, sig)
    if sig:
        result.update({
            "signal_side": sig.get("signal_side"),
            "signal_type": sig.get("signal_type"),
            "strategy_position": sig.get("strategy_position"),
            "raw_message": sig.get("raw_message"),
        })
    return jsonify(result)

@app.route('/signal', methods=['POST'])
def signal_generic():
    """A flexible alias that accepts either {"ticker": "PAYX", ...} or {"text": "... PAYX ..."}."""
    data = request.get_json(silent=True) or {}
    _append_webhook_event(data, route="/signal")
    raw_text = data.get('text') or data.get('message') or data.get('alert') or data.get('alert_message')
    symbol = _clean_symbol(data.get('ticker') or data.get('symbol') or _extract_ticker_from_text(raw_text))
    if not symbol:
        return _fail("payload", "ticker missing (and text did not contain an extractable ticker)", 400)
    sig = _parse_signal_fields(raw_text)
    result = get_option_data(symbol)
    if not result or result.get("_error"):
        return jsonify({
            "_error": True,
            "stage": (result or {}).get("stage","unknown"),
            "detail": (result or {}).get("detail","unable to retrieve option data")
        }), 200
    _append_listener_result_to_csv(result, sig)
    if sig:
        result.update({
            "signal_side": sig.get("signal_side"),
            "signal_type": sig.get("signal_type"),
            "strategy_position": sig.get("strategy_position"),
            "raw_message": sig.get("raw_message"),
        })
    return jsonify(result)


@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(silent=True) or {}
    _append_webhook_event(data, route="/webhook")
    # Accept both ticker and symbol, or extract from free‑form message/text
    msg = data.get('message') or data.get('alert_message') or data.get('alert') or data.get('text')
    symbol = _clean_symbol(data.get('ticker') or data.get('symbol') or _extract_ticker_from_text(msg))
    if not symbol:
        return _fail("payload", "ticker missing from payload and not found in text", 400)
    sig = _parse_signal_fields(msg)
    result = get_option_data(symbol)
    if not result or result.get("_error"):
        # bubble up stage & detail if available, but do not fail the HTTP call
        msg = (result or {}).get("detail", "unable to retrieve option data")
        stage = (result or {}).get("stage", "unknown")
        return jsonify({
            "_error": True,
            "stage": stage,
            "detail": msg
        }), 200
    _append_listener_result_to_csv(result, sig)
    if sig:
        result.update({
            "signal_side": sig.get("signal_side"),
            "signal_type": sig.get("signal_type"),
            "strategy_position": sig.get("strategy_position"),
            "raw_message": sig.get("raw_message"),
        })
    return jsonify(result)

@app.route('/webhook_batch', methods=['POST'])
def webhook_batch():
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    try:
        _append_webhook_event(data if isinstance(data, (dict, list)) else {"raw": str(data)}, route="/webhook_batch")
    except Exception:
        pass

    symbols = None
    if isinstance(data, dict):
        symbols = data.get('tickers') or data.get('symbols')
        if symbols is None:
            for alt in ('data', 'payload', 'items'):
                v = data.get(alt)
                if isinstance(v, list):
                    symbols = v
                    break
        if isinstance(symbols, str):
            symbols = [s.strip() for s in symbols.split(',') if s.strip()]
    if symbols is None and isinstance(data, list):
        symbols = data

    if not symbols or not isinstance(symbols, list):
        return _fail("payload", "Expected a list of tickers. Accepted shapes: {\"tickers\":[...]}, {\"symbols\":[...]}, {\"data\":[...]}, top-level JSON array, or comma-separated string.", 400)
    results = []
    for item in symbols:
        tick = None
        msg  = None
        if isinstance(item, str):
            tick = _clean_symbol(item)
        elif isinstance(item, dict):
            msg = item.get("message") or item.get("alert_message") or item.get("alert") or item.get("text")
            tick = _clean_symbol(str(item.get("ticker") or item.get("symbol") or "")) or _extract_ticker_from_text(msg)
        elif item is not None:
            tick = _clean_symbol(str(item))
        if not tick:
            continue
        sig = _parse_signal_fields(msg)
        res = get_option_data(tick)
        if not res or res.get("_error"):
            results.append({"symbol": tick or item, "error": (res or {}).get("detail", "unknown")})
        else:
            _append_listener_result_to_csv(res, sig)
            if sig:
                res.update({
                    "signal_side": sig.get("signal_side"),
                    "signal_type": sig.get("signal_type"),
                    "strategy_position": sig.get("strategy_position"),
                    "raw_message": sig.get("raw_message"),
                })
            results.append(res)
    return jsonify({"results": results})
@app.route('/mdtest', methods=['GET'])
def mdtest():
    sym = _clean_symbol(request.args.get('symbol', 'AAPL'))
    mdtype = request.args.get('mdtype', '1')  # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
    try:
        # Ensure IB is connected; prefer helper if present
        try:
            _ensure_ib_connected(IB_SHARED, mkt_type=int(mdtype))
        except NameError:
            if not IB_SHARED.isConnected():
                IB_SHARED.connect('127.0.0.1', 7497, clientId=42)
            try:
                IB_SHARED.reqMarketDataType(int(mdtype))
            except Exception:
                pass
        # Explicitly set market data type to requested mode
        try:
            IB_SHARED.reqMarketDataType(int(mdtype))
        except Exception:
            pass

        # Request a fresh quote and patiently poll for fields
        timeout_ms = int(request.args.get('timeout_ms', '2500'))  # default ~2.5s
        poll_ms = 200
        waited = 0
        t = IB_SHARED.reqMktData(Stock(sym, 'SMART', 'USD'), '', False, False)
        bid = ask = last = close = None
        delayed = getattr(getattr(t, 'tickAttrib', None), 'delayed', None)
        while waited <= timeout_ms:
            # collect what we have so far
            bid   = getattr(t, 'bid',   bid)
            ask   = getattr(t, 'ask',   ask)
            last  = getattr(t, 'last',  last)
            close = getattr(t, 'close', close)
            # if we have at least bid/ask or last/close, we can stop early
            if (bid is not None and ask is not None) or (last is not None or close is not None):
                break
            IB_SHARED.sleep(poll_ms/1000.0)
            waited += poll_ms
            delayed = getattr(getattr(t, 'tickAttrib', None), 'delayed', delayed)

        # compute mid if possible
        mid = None
        try:
            if bid is not None and ask is not None:
                mid = (float(bid) + float(ask)) / 2.0
        except Exception:
            mid = None

        out = {
            "symbol": sym,
            "mdtype": int(mdtype),
            "timeout_ms": timeout_ms,
            "waited_ms": waited,
            "delayed": delayed,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "last": last,
            "close": close
        }
        # If nothing came in and user did not already request 4, auto retry once with mdtype=4
        if all(v is None for v in (bid, ask, last, close)) and int(mdtype) != 4:
            try:
                IB_SHARED.reqMarketDataType(4)
            except Exception:
                pass
            # quick retry with shorter budget
            waited = 0
            for _ in range(5):
                IB_SHARED.sleep(0.2)
                bid   = getattr(t, 'bid',   bid)
                ask   = getattr(t, 'ask',   ask)
                last  = getattr(t, 'last',  last)
                close = getattr(t, 'close', close)
                if (bid is not None and ask is not None) or (last is not None or close is not None):
                    break
                waited += 200
            out.update({"mdtype": 4, "waited_ms": out.get("waited_ms", 0) + waited, "bid": bid, "ask": ask, "last": last, "close": close})
        # If nothing came in, say so explicitly
        if all(v is None for v in (bid, ask, last, close)):
            out["note"] = "No ticks received within timeout; try mdtype=4 (delayed-frozen) or verify live/delayed permissions in TWS/Gateway."
        return jsonify(out)
    except Exception as e:
        import traceback
        return jsonify({
            "symbol": sym,
            "mdtype": mdtype,
            "error_type": e.__class__.__name__,
            "error": repr(e),
            "trace": traceback.format_exc()
        }), 500


@app.route("/", methods=["GET", "POST"])
def index():
    """
    Root endpoint: provides a friendly hint instead of 404.
    """
    return jsonify({
        "ok": True,
        "message": "Listener is running",
        "version": VERSION,
        "combined_csv_path": str(_combined_csv()),
        "endpoints": {
            "single_webhook": "POST /webhook",
            "batch": "POST /webhook_batch",
            "signal": "POST /signal  (json: {ticker|symbol|text})",
            "signal_text": "POST /signal/text  (json: {text})",
            "health": "GET /health",
            "mdtest": "GET /mdtest?symbol=PAYX&mdtype=4&timeout_ms=1500"
        }
    })

@app.route('/health', methods=['GET'])
def health():
    """
    Health endpoint. Returns version, CSV path, and a lightweight positions summary (paper).
    Does not fail the endpoint if the API is not reachable; it will just omit positions.
    """
    payload = {
        "version": VERSION,
        "combined_csv_path": str(_combined_csv()),
        "source_file": __file__,
        "python": sys.version.split()[0],
    }
    try:
        # Fast, non-blocking positions snapshot: do not attempt connect here to avoid hangs
        if IB_SHARED.isConnected():
            try:
                IB_SHARED.reqMarketDataType(_preferred_md_type())
            except Exception:
                pass
            try:
                IB_SHARED.sleep(0.1)
            except Exception:
                pass
            try:
                ps = list(IB_SHARED.positions())
            except Exception as _pos_exc:
                payload["positions_error"] = f"positions: {_pos_exc!r}"
                ps = []
            payload["positions_count"] = len(ps)
            if ps:
                sample = []
                for p in ps[:25]:
                    c = p.contract
                    sample.append({
                        "symbol": getattr(c, "symbol", ""),
                        "secType": getattr(c, "secType", ""),
                        "exp": getattr(c, "lastTradeDateOrContractMonth", ""),
                        "right": getattr(c, "right", ""),
                        "strike": getattr(c, "strike", ""),
                        "qty": p.position,
                        "avgCost": p.avgCost,
                    })
                payload["positions_sample"] = sample
        else:
            payload["positions_error"] = "not connected"
    except Exception as exc:
        # Do not fail health on any error
        payload["positions_error"] = repr(exc)
    return jsonify(payload)

if __name__ == '__main__':
    import os
    port = int(os.environ.get("PORT", "5001"))
    try:
        if not IB_SHARED.isConnected():
            IB_SHARED.connect('127.0.0.1', 7497, clientId=42)
        # Use LIVE by default (user has streaming bundles)
        IB_SHARED.reqMarketDataType(1)
    except Exception as exc:
        logger.exception(f"Initial IB connect failed: {exc}")
    logger.info(f"Listener version: {VERSION}")
    logger.info(f"Starting listener on 0.0.0.0:{port} (threaded=False)")
    app.run(host='0.0.0.0', port=port, threaded=False, use_reloader=False)
