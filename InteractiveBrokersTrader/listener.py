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
# --- Force this process (and any re-exec) to remain in the venv on Windows ---
import platform
if platform.system() == "Windows":
    try:
        import os as _os, sys as _sys
        # Hard stop if we're not the venv interpreter
        _venv_root = r"C:\Users\Administrator\code\OptionsTradingStrategy\.venv\Scripts".lower()
        if not _sys.executable.lower().startswith(_venv_root):
            print(f"listener: refusing non-venv interpreter ({_sys.executable}); exiting.", flush=True)
            _os._exit(0)  # immediate exit (no atexit), so system python can't bind :5001

        # Make any internal re-exec use the venv interpreter, not the base install
        _os.environ["PYTHONEXECUTABLE"] = _sys.executable
        if hasattr(_sys, "_base_executable"):
            _sys._base_executable = _sys.executable
    except Exception:
        # If anything goes wrong, be conservative and exit
        import os as __os
        __os._exit(0)
# --- HARD EXIT if not launched from the venv interpreter (Windows safety) ---
# This runs before any server binding so a stray system Python can’t grab :5001.
if platform.system() == "Windows":
    try:
        _exe = sys.executable.lower()
        _venv_frag = r"\optionsTradingStrategy\.venv\scripts".lower()
        _system_exe = r"c:\program files\python312\python.exe".lower()
        in_venv = (_venv_frag in _exe) or (hasattr(sys, "base_prefix") and sys.prefix != sys.base_prefix)
        if (not in_venv) or (_exe == _system_exe):
            print(f"listener: refusing non-venv interpreter ({sys.executable}); exiting.", flush=True)
            import os as _os
            _os._exit(0)  # hard exit: skip atexit and any server startup
    except Exception:
        # If any check fails, be conservative and exit
        import os as _os
        _os._exit(0)
# --- single-instance guard (Windows-safe) ---
import socket, atexit
from pathlib import Path as _Path
def _ib_ports_status():
    import socket
    res = {}
    for p in (7497, 7496):
        s=socket.socket(); s.settimeout(0.5)
        try: s.connect(('127.0.0.1', p)); res[p]=True
        except: res[p]=False
        finally: s.close()
    return res
# --- helper to test if port is already open ---
def _port_is_open(_host="127.0.0.1", _port=5001, _timeout=0.3):
    try:
        _s = socket.socket()
        _s.settimeout(_timeout)
        _s.connect((_host, _port))
        _s.close()
        return True
    except Exception:
        return False

# --- refuse to run outside the venv (enforce venv/service) ---
def _is_running_in_venv() -> bool:
    """Return True if running inside a Python virtual environment on any OS."""
    try:
        # Standard venv detection works cross‑platform
        if hasattr(sys, "base_prefix") and sys.prefix != sys.base_prefix:
            return True
        # Virtualenv sets this
        if os.environ.get("VIRTUAL_ENV"):
            return True
        return False
    except Exception:
        return False

if not _is_running_in_venv():
    print(f"listener: non-venv interpreter ({sys.executable}); exiting.", flush=True)
    sys.exit(0)

# --- prefer venv instance over system-Python when port is already serving ---
try:
    _is_system_py = r"\Program Files\Python312\python.exe".lower() in sys.executable.lower()
    if _is_system_py and _port_is_open():
        print("listener: system-python duplicate detected; exiting.", flush=True)
        sys.exit(0)
except Exception:
    pass

# --- single-instance lock (after system-python checks) ---
_LOCK_DIR  = _Path(os.getenv("PROGRAMDATA", r"C:\ProgramData")) / "OptionsTradingStrategy"
_LOCK_DIR.mkdir(parents=True, exist_ok=True)
_LOCK_FILE = _LOCK_DIR / "listener.lock"

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
        return int(os.getenv("MARKET_DATA_TYPE", "4"))
    except Exception:
        return 4

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

# --- Fallback expiration helper ---
def _fallback_expiration_str(days: int = 30) -> str:
    try:
        d = datetime.now().date() + timedelta(days=int(days))
        return d.strftime('%Y%m%d')
    except Exception:
        # Hard fallback: 30 days from epoch-now if anything odd occurs
        return (datetime.utcnow().date() + timedelta(days=30)).strftime('%Y%m%d')

# --- SecDef & strike helpers to force "odd if available" ---
from typing import Iterable
_EXCHANGE_TRY_ORDER: tuple[str, ...] = ('SMART','BOX','CBOE','ISE','NASDAQOM','PHLX','BATS','AMEX')

def _collect_secdef(ib: IB, symbol: str, con_id: int, max_retries: int = 3) -> tuple[list[str], list[float], list[str], list[str]]:
    """
    Returns (expirations, strikes, tradingClasses, multipliers) from reqSecDefOptParams.
    Retries with exponential backoff if strikes list is empty.
    """
    delays = [0.5, 1.0, 2.0]  # Exponential backoff delays

    for attempt in range(max_retries):
        params = ib.reqSecDefOptParams(symbol, '', 'STK', con_id)
        delay = delays[attempt] if attempt < len(delays) else delays[-1]
        ib.sleep(delay)

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

        # Success if we got strikes
        if strikes_all:
            if attempt > 0:
                logger.info(f"[SECDEF] {symbol}: got {len(strikes_all)} strikes on attempt {attempt + 1}")
            return expirations, list(strikes_all), trading_classes, multipliers

        # Log retry
        if attempt < max_retries - 1:
            logger.warning(f"[SECDEF] {symbol}: no strikes on attempt {attempt + 1}, retrying in {delays[attempt + 1] if attempt + 1 < len(delays) else delays[-1]}s...")

    # All retries exhausted - log and return empty
    logger.warning(f"[SECDEF] {symbol}: no strikes after {max_retries} attempts")
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
import time
# Optional production WSGI server on Windows (safer under services)
try:
    from waitress import serve as _serve
    _USE_WAITRESS = True
except Exception:
    _serve = None
    _USE_WAITRESS = False

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


def _theo_spread_debits(S: float, atm: float, T: float, sigma_atm: float,
                        sigma_otm: float | None = None,
                        r: float = 0.045, widths=(1.0, 2.5, 5.0)) -> Dict[str, float]:
    """Calculate theoretical debit spread prices using Black-Scholes.

    Args:
        sigma_atm: IV for ATM (long) leg
        sigma_otm: IV for OTM (short) leg - defaults to sigma_atm if None
    """
    if sigma_otm is None:
        sigma_otm = sigma_atm

    out: Dict[str, float] = {}
    for W in widths:
        call_long = _bs_price(S, atm, T, r, sigma_atm, call=True)
        call_short = _bs_price(S, atm + W, T, r, sigma_otm, call=True)
        put_long  = _bs_price(S, atm, T, r, sigma_atm, call=False)
        put_short = _bs_price(S, max(atm - W, 0.01), T, r, sigma_otm, call=False)
        key = "2_5" if abs(W - 2.5) < 1e-9 else str(int(W))
        # Fix Y2b: clamp to >= 0 (debit spread value cannot be negative)
        out[f"call_debit_theo_{key}"] = max(0.0, float(call_long - call_short))
        out[f"put_debit_theo_{key}"]  = max(0.0, float(put_long - put_short))
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


def _get_position_for_symbol(ib: IB, symbol: str) -> dict | None:
    """
    Look up held option positions for a symbol.
    Returns dict with {expiration, atm_strike, right, width, strikes_all} or None if no position.
    Used by get_option_data() to price CLOSE signals based on actual held positions.
    """
    try:
        if not ib.isConnected():
            return None
        ib.reqPositions()
        ib.sleep(0.3)
        positions = list(ib.positions())

        # Find option positions for this symbol
        opts = []
        for p in positions:
            c = p.contract
            if getattr(c, 'symbol', '').upper() == symbol.upper() and getattr(c, 'secType', '') == 'OPT':
                opts.append({
                    'expiration': getattr(c, 'lastTradeDateOrContractMonth', ''),
                    'strike': getattr(c, 'strike', 0),
                    'right': getattr(c, 'right', ''),  # C or P
                    'qty': p.position,
                })

        if not opts:
            return None

        # Group by expiration and right to find spread structure
        # Long leg (qty > 0) is ATM, short leg (qty < 0) is OTM
        long_legs = [o for o in opts if o['qty'] > 0]
        short_legs = [o for o in opts if o['qty'] < 0]

        if not long_legs:
            return None

        # Use first long leg as ATM
        atm = long_legs[0]
        width = None
        otm_strike = None
        if short_legs:
            # Find short leg with same right and expiration
            matching_short = [s for s in short_legs if s['right'] == atm['right'] and s['expiration'] == atm['expiration']]
            if matching_short:
                otm_strike = matching_short[0]['strike']
                width = abs(atm['strike'] - otm_strike)

        # Collect all strikes from position for reference
        strikes_all = sorted(set(o['strike'] for o in opts))

        return {
            'expiration': atm['expiration'],
            'atm_strike': atm['strike'],
            'otm_strike': otm_strike,
            'right': atm['right'],
            'width': width or 1.0,  # Default to $1 if can't determine
            'strikes_all': strikes_all,
        }
    except Exception as e:
        logger.warning(f"Position lookup failed for {symbol}: {e}")
        return None


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
def _webhook_results_jsonl() -> Path:
    return _dated_dir() / "webhook_results.jsonl"

def _append_webhook_result(rec: dict) -> None:
    try:
        from json import dumps as _dumps
        out = _webhook_results_jsonl()
        with out.open("a", encoding="utf-8") as f:
            f.write(_dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _log_reject(symbol: str, err: str) -> None:
    try:
        import csv as _csv
        p = _dated_dir() / "rejected_webhooks.csv"
        write_header = not p.exists()
        with p.open("a", newline="", encoding="utf-8") as fh:
            w = _csv.writer(fh)
            if write_header:
                w.writerow(["timestamp_ny", "symbol", "error"])
            try:
                ts = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            w.writerow([ts, symbol, err])
    except Exception:
        pass
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
        # Capture raw body and content-type for forensic/debug purposes
        try:
            raw = request.get_data(as_text=True)
        except Exception:
            raw = None
        try:
            ctype = request.headers.get("Content-Type")
        except Exception:
            ctype = None
        rec = {"ts": ts, "route": route, "payload": payload, "raw": raw, "content_type": ctype}
        out = _webhook_jsonl()
        with out.open("a", encoding="utf-8") as f:
            f.write(_dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"[WEBHOOK_JSONL] failed to append: {e}")

# --- Helper to robustly parse JSON or fallback to text/plain bodies ---
def _get_payload():
    """
    Return a best-effort payload:
      - If Content-Type is JSON, parse it (silent).
      - Else, try to json.loads(raw) if body looks like JSON.
      - Else, return {"text": raw_string}.
    """
    try:
        if request.is_json:
            data = request.get_json(silent=True)
            return data if isinstance(data, (dict, list)) else (data or {})
    except Exception:
        pass
    # Fallback: get raw text and try to parse JSON if it appears to be JSON
    try:
        raw = request.get_data(as_text=True) or ""
    except Exception:
        raw = ""
    s = raw.strip()
    if s.startswith("{") or s.startswith("["):
        try:
            import json as _json
            data = _json.loads(s)
            return data if isinstance(data, (dict, list)) else {"text": s}
        except Exception:
            return {"text": s}
    return {"text": s}

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
                if isinstance(v, str) and v.strip().lower() in ("nan","none",""):
                    rounded_row[k] = None
                else:
                    rounded_row[k] = v
        w.writerow(rounded_row)
    logger.info(f"[CSV] Appended row to {out_csv}")

# Fix M: After hours, quote-based prices are unreliable (stale bids/asks).
# Always use theo (Black-Scholes) values in the limit columns.
# Live prices will be backfilled at market open via backfill_theo.py --live


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

    # Theo inputs - use separate IVs for ATM (long) and OTM (short) legs
    S   = result.get("current_price")
    atm = result.get("atm_strike")
    iv_atm = result.get("implied_volatility_atm")
    iv_otm = result.get("implied_volatility_otm")

    # Parse OTM IV (for short leg) first - needed for ATM fallback below
    sigma_otm = None
    if not _is_nan(iv_otm):
        try: sigma_otm = float(iv_otm)
        except Exception: sigma_otm = None

    # Parse ATM IV (for long leg)
    sigma_atm = None
    if not _is_nan(iv_atm):
        try: sigma_atm = float(iv_atm)
        except Exception: sigma_atm = None
    if sigma_atm is None or _is_nan(sigma_atm):
        # Fix Y2a: prefer iv_otm over hardcoded 0.25 when iv_atm is missing
        sigma_atm = sigma_otm if (sigma_otm is not None and not _is_nan(sigma_otm)) else 0.25

    theo = {"call_debit_theo_1": None,"put_debit_theo_1": None,"call_debit_theo_2_5": None,"put_debit_theo_2_5": None,"call_debit_theo_5": None,"put_debit_theo_5": None}
    if not _is_nan(S) and not _is_nan(atm) and (days_to_exp is not None and days_to_exp > 0):
        try:
            theo = _theo_spread_debits(float(S), float(atm), float(T), float(sigma_atm), sigma_otm=sigma_otm)
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
        "call_debit_limit": None,
        "put_debit_limit":  None,
        # Fix X5: Limit columns reserved for live market prices (populated by
        # LiquidityFilter at 9:35 AM).  Theo columns carry Black-Scholes values.
        # PlaceAnOrder falls back to theo when limit is empty (Fix B).
        "call_debit_limit_1": None,
        "put_debit_limit_1": None,
        "call_debit_limit_2_5": None,
        "put_debit_limit_2_5": None,
        "call_debit_limit_5": None,
        "put_debit_limit_5": None,
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

def _safe_cancel_md(t):
    """Cancel a live market data subscription if possible and ignore any errors; flush a tiny delay first."""
    try:
        if t is not None:
            try: IB_SHARED.sleep(0.05)
            except Exception: pass
            IB_SHARED.cancelMktData(t.contract)
    except Exception:
        pass

def get_option_data(symbol: str, width: int = 5, signal_type: str | None = None):
    # Normalize any malformed symbol (e.g., 'NWSA.', 'BATS:EQH, 1D')
    symbol = _clean_symbol(symbol)
    ib = IB_SHARED
    stage = "connect"
    # Ensure connected and set market data type (do not early-return; we can always price theo if later pieces fail)
    try:
        _ensure_ib_connected(ib, mkt_type=_preferred_md_type())
    except Exception as exc:
        logger.warning(f"IB connect failed (will attempt theo-only later if needed): {exc}")

    # For CLOSE signals, try to use actual position's strikes/expiration
    # This ensures we price the ACTUAL held spread, not a hypothetical new one
    position_info = None
    if signal_type == "CLOSE":
        position_info = _get_position_for_symbol(ib, symbol)
        if position_info:
            logger.info(f"[CLOSE] Using position data for {symbol}: exp={position_info['expiration']}, atm={position_info['atm_strike']}, right={position_info['right']}, width={position_info.get('width')}")

    # Define the stock and try to get a current price; fall back to historical with polling first
    stage = "stock_price"
    stock = Stock(symbol, 'SMART', 'USD')
    ticker_stock = None
    try:
        ticker_stock = ib.reqMktData(stock, '', False, False)
    except Exception as exc:
        logger.warning(f"reqMktData stock failed: {exc}")
        ticker_stock = None

    current_price = None
    if ticker_stock is not None:
        vals, _waited = _wait_for_fields(ticker_stock, fields=("last","close","bid","ask"), timeout_ms=10000, step_ms=200)
        # escalate to mdtype=3 if all are missing
        empty_stock = all(vals.get(k) is None for k in ("last","close","bid","ask"))
        if empty_stock:
            try: ib.reqMarketDataType(3)
            except Exception: pass
            vals2, _w2 = _wait_for_fields(ticker_stock, fields=("last","close","bid","ask"), timeout_ms=3000, step_ms=200)
            for k in ("last","close","bid","ask"):
                if vals2.get(k) is not None:
                    vals[k] = vals2[k]
        # Fix AL: After market hours, prefer official close over AH last price.
        # AH 'last' is from thin/illiquid after-hours trading and is unreliable for
        # Black-Scholes options pricing. Official close (4 PM settlement) is correct.
        # Server clock is ET; datetime.now() returns local ET time.
        try:
            _now_al = datetime.now()
            # Fix AN: IB's 'close' tick shows previous session's settlement until ~16:30 ET.
            # Batch signals arrive 16:01-16:16 ET — 'last' is today's close trade at that point.
            # Only prefer close>last after 16:30, when today's settlement has propagated.
            _after_hours_al = (
                _now_al.weekday() >= 5          # weekend
                or _now_al.hour < 9
                or (_now_al.hour == 9 and _now_al.minute < 30)
                or (_now_al.hour == 16 and _now_al.minute >= 30)
                or _now_al.hour > 16
            )
            if _after_hours_al:
                # After hours: close (official settlement) > last (AH trade) > mid
                if vals.get("close") is not None:
                    current_price = float(vals["close"])
                elif vals.get("last") is not None:
                    current_price = float(vals["last"])
                elif vals.get("bid") is not None and vals.get("ask") is not None:
                    current_price = (float(vals["bid"]) + float(vals["ask"])) / 2.0
            else:
                # Market hours: last (real-time trade) > close > mid
                if vals.get("last") is not None:
                    current_price = float(vals["last"])
                elif vals.get("close") is not None:
                    current_price = float(vals["close"])
                elif vals.get("bid") is not None and vals.get("ask") is not None:
                    current_price = (float(vals["bid"]) + float(vals["ask"])) / 2.0
        except Exception:
            current_price = None
    # Always cancel stock market data after polling
    _safe_cancel_md(ticker_stock)

    if current_price is None:
        # fallback to 1-day historical bar close (after-hours friendly)
        try:
            bars = ib.reqHistoricalData(
                stock, endDateTime='', durationStr='1 D', barSizeSetting='1 day',
                whatToShow='TRADES', useRTH=False, formatDate=1
            )
            if (not bars) or (hasattr(bars[-1], "close") and str(bars[-1].close) == "nan"):
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
        # Still produce a minimal theo-only result so CSV captures the signal context
        return {
            "_error": False,
            "_theo_only": True,
            "symbol": symbol,
            "current_price": None,
            "atm_strike": None,
            "otm_strike": None,
            "put_otm_strike": None,
            "expiration": _fallback_expiration_str(TARGET_DTE),
            "implied_volatility_atm": None,
            "open_interest_atm": None,
            "implied_volatility_otm": None,
            "open_interest_otm": None,
            "call_debit": None,
            "put_debit": None,
            "call_debit_limit_1": None,
            "put_debit_limit_1":  None,
            "call_debit_limit_2_5": None,
            "put_debit_limit_2_5":  None,
            "call_debit_limit_5": None,
            "put_debit_limit_5":  None,
        }

    # Placeholders to support a theo-only fallback if option qualification fails later
    expiry_str = None
    strikes_all = []
    preferred_tc = None
    multiplier = None

    try:
        # Get contract details / conId
        stage = "contract_details"
        details = ib.reqContractDetails(stock)
        if not details:
            raise RuntimeError("No contract details returned for underlying")
        con_id = details[0].contract.conId

        # Get expirations, strikes, tradingClass & multiplier from SecDef (enables odd strikes if listed)
        stage = "secdef"
        expirations, strikes_all, trading_classes, multipliers = _collect_secdef(ib, symbol, con_id)
        preferred_tc = _pick_preferred_tc(symbol, trading_classes)
        multiplier = multipliers[0] if multipliers else None

        # Choose expiry nearest to 30 calendar days (or fallback)
        # For CLOSE signals with position data, use the position's expiration
        stage = "pick_expiry"
        target_date = datetime.now().date()
        expiry_str = None
        if position_info and position_info.get('expiration'):
            # Use actual position's expiration for CLOSE signals
            expiry_str = position_info['expiration']
            logger.info(f"[CLOSE] Using position expiration: {expiry_str}")
        elif expirations:
            exps = []
            for d in expirations:
                try:
                    ed = datetime.strptime(d, '%Y%m%d').date()
                    dte = (ed - target_date).days
                    exps.append((d, dte))
                except Exception:
                    continue
            if not exps:
                expiry_str = _fallback_expiration_str(TARGET_DTE)
            else:
                valid = [(d, dte) for (d, dte) in exps if dte >= MIN_DTE]
                if valid:
                    expiry_str = min(valid, key=lambda t: abs(t[1] - TARGET_DTE))[0]
                else:
                    expiry_str = min(exps, key=lambda t: abs(t[1] - TARGET_DTE))[0]
        else:
            expiry_str = _fallback_expiration_str(TARGET_DTE)

        # Choose ATM strike from available strikes (closest to current_price)
        # For CLOSE signals with position data, use the position's actual strikes
        stage = "pick_strikes"
        if position_info and position_info.get('atm_strike'):
            # Use actual position's strikes for CLOSE signals
            atm_strike = position_info['atm_strike']
            pos_width = position_info.get('width') or 1.0
            pos_right = position_info.get('right', 'C')
            if position_info.get('otm_strike'):
                otm_strike = position_info['otm_strike']
            elif pos_right == 'C':
                otm_strike = atm_strike + pos_width
            else:  # PUT
                otm_strike = atm_strike - pos_width
            logger.info(f"[CLOSE] Using position strikes: ATM={atm_strike}, OTM={otm_strike}, right={pos_right}")
        elif not strikes_all:
            atm_strike = round(current_price)
            otm_strike = atm_strike + 1  # placeholder for width; real quote-based widths below may adjust
        else:
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
                    raise
                option = _qualify_with_fallback(ib, symbol, expiry_str, fallback, 'C', preferred_tc, multiplier)
                strike = fallback
                if idx == 1:
                    otm_strike = strike
                else:
                    atm_strike = strike
            # Request option market data incl. generic ticks for OI (101) and IV30 (106)
            stage = "option_mktdata"
            try:
                t = ib.reqMktData(option, '101,106', False, False)
                vals, _ = _wait_for_fields(t, fields=("bid","ask"), timeout_ms=10000, step_ms=200)
                # if still empty, try delayed mdtype=3 briefly
                if all(vals.get(k) is None for k in ("bid","ask")):
                    try: ib.reqMarketDataType(3)
                    except Exception: pass
                    vals2, _ = _wait_for_fields(t, fields=("bid","ask","last","close"), timeout_ms=3000, step_ms=200)
                    for k in ("bid","ask","last","close"):
                        if vals2.get(k) is not None and vals.get(k) is None:
                            vals[k] = vals2.get(k)
                _safe_cancel_md(t)
            except Exception:
                t = None
                vals = {}
            legs_info.append({
                "strike": strike,
                "bid": vals.get("bid"),
                "ask": vals.get("ask"),
                "impliedVolatility": getattr(t, 'impliedVolatility', None) if t else None,
                "callOpenInterest": getattr(t, 'callOpenInterest', None) if t else None
            })

        # --- Fetch put legs for a put debit spread (long ATM put, short lower strike put) ---
        stage = "qualify_put_options"
        # For CLOSE signals with PUT position, use actual position's OTM strike
        if position_info and position_info.get('right') == 'P' and position_info.get('otm_strike'):
            put_otm_strike = position_info['otm_strike']
        elif strikes_all:
            put_otm_strike = _next_lower_existing(strikes_all, atm_strike)
        else:
            put_otm_strike = max(atm_strike - 1, 0.01)
        put_legs_info = []
        for strike, right in ((atm_strike, 'P'), (put_otm_strike, 'P')):
            try:
                option = _qualify_with_fallback(ib, symbol, expiry_str, strike, right, preferred_tc, multiplier)
            except Exception:
                # If even put qualification fails, continue; theo-only still possible
                option = None
            stage = "option_put_mktdata"
            if option is not None:
                try:
                    t = ib.reqMktData(option, '101,106', False, False)
                    vals, _ = _wait_for_fields(t, fields=("bid","ask"), timeout_ms=10000, step_ms=200)
                    # if still empty, try delayed mdtype=3 briefly
                    if all(vals.get(k) is None for k in ("bid","ask")):
                        try: ib.reqMarketDataType(3)
                        except Exception: pass
                        vals2, _ = _wait_for_fields(t, fields=("bid","ask","last","close"), timeout_ms=3000, step_ms=200)
                        for k in ("bid","ask","last","close"):
                            if vals2.get(k) is not None and vals.get(k) is None:
                                vals[k] = vals2.get(k)
                    _safe_cancel_md(t)
                except Exception:
                    t = None
                    vals = {}
            else:
                t = None
                vals = {}
            put_legs_info.append({
                "strike": strike,
                "bid": vals.get("bid"),
                "ask": vals.get("ask"),
                "impliedVolatility": getattr(t, 'impliedVolatility', None) if t else None,
                "putOpenInterest": getattr(t, 'putOpenInterest', None) if t else None
            })

        # Compute net debit limits from quotes (may be None)
        stage = "compute_debit"
        call_buy_ask = legs_info[0]["ask"] if legs_info else None
        call_sell_bid = legs_info[1]["bid"] if len(legs_info) > 1 else None
        call_debit = None
        if call_buy_ask is not None and call_sell_bid is not None:
            try:
                call_debit = float(call_buy_ask) - float(call_sell_bid)
            except Exception:
                call_debit = None

        put_buy_ask = put_legs_info[0]["ask"] if put_legs_info else None
        put_sell_bid = put_legs_info[1]["bid"] if len(put_legs_info) > 1 else None
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
            # Calls
            try:
                c_long = _qualify_with_fallback(ib, symbol, expiry_str, atm_strike, 'C', preferred_tc, multiplier)
                target_c = atm_strike + W
                c_candidates = [s for s in strikes_all if s >= target_c]
                c_shortK = c_candidates[0] if c_candidates else _next_higher_existing(strikes_all, atm_strike)
                c_short = _qualify_with_fallback(ib, symbol, expiry_str, c_shortK, 'C', preferred_tc, multiplier)
                tcL = ib.reqMktData(c_long, '', False, False)
                tcS = ib.reqMktData(c_short, '', False, False)
                vL, _ = _wait_for_fields(tcL, fields=("ask","last","close"), timeout_ms=10000, step_ms=200)
                vS, _ = _wait_for_fields(tcS, fields=("bid","last","close"), timeout_ms=10000, step_ms=200)
                _safe_cancel_md(tcL); _safe_cancel_md(tcS)
                val = None
                try:
                    askL = vL.get("ask")
                    bidS = vS.get("bid")
                    if askL is not None and bidS is not None:
                        val = float(askL) - float(bidS)
                    elif vL.get("last") is not None and vS.get("close") is not None:
                        val = float(vL["last"]) - float(vS["close"])
                except Exception:
                    val = None
                if abs(W - 1.0) < 1e-9:
                    call_debit_limit_1 = val
                elif abs(W - 2.5) < 1e-9:
                    call_debit_limit_2_5 = val
                else:
                    call_debit_limit_5 = val
            except Exception:
                pass
            # Puts
            try:
                p_long = _qualify_with_fallback(ib, symbol, expiry_str, atm_strike, 'P', preferred_tc, multiplier)
                target_p = max(atm_strike - W, 0.01)
                p_candidates = [s for s in strikes_all if s <= target_p]
                p_shortK = p_candidates[-1] if p_candidates else _next_lower_existing(strikes_all, atm_strike)
                p_short = _qualify_with_fallback(ib, symbol, expiry_str, p_shortK, 'P', preferred_tc, multiplier)
                tpL = ib.reqMktData(p_long, '', False, False)
                tpS = ib.reqMktData(p_short, '', False, False)
                vL, _ = _wait_for_fields(tpL, fields=("ask","last","close"), timeout_ms=10000, step_ms=200)
                vS, _ = _wait_for_fields(tpS, fields=("bid","last","close"), timeout_ms=10000, step_ms=200)
                _safe_cancel_md(tpL); _safe_cancel_md(tpS)
                val = None
                try:
                    askL = vL.get("ask")
                    bidS = vS.get("bid")
                    if askL is not None and bidS is not None:
                        val = float(askL) - float(bidS)
                    elif vL.get("last") is not None and vS.get("close") is not None:
                        val = float(vL["last"]) - float(vS["close"])
                except Exception:
                    val = None
                if abs(W - 1.0) < 1e-9:
                    put_debit_limit_1 = val
                elif abs(W - 2.5) < 1e-9:
                    put_debit_limit_2_5 = val
                else:
                    put_debit_limit_5 = val
            except Exception:
                pass

        hint = {}
        if call_debit is None and call_buy_ask is None and call_sell_bid is None:
            hint["no_ticks_calls"] = True
        if put_debit is None and put_buy_ask is None and put_sell_bid is None:
            hint["no_ticks_puts"] = True
        # attach hint only if present
        return {
            "_error": False,
            "symbol": symbol,
            "current_price": current_price,
            "atm_strike": atm_strike,
            "otm_strike": otm_strike,
            "put_otm_strike": put_otm_strike,
            "expiration": expiry_str,
            "implied_volatility_atm": legs_info[0]['impliedVolatility'] if legs_info else None,
            "open_interest_atm": legs_info[0]['callOpenInterest'] if legs_info else None,
            "implied_volatility_otm": legs_info[1]['impliedVolatility'] if len(legs_info) > 1 else None,
            "open_interest_otm": legs_info[1]['callOpenInterest'] if len(legs_info) > 1 else None,
            "call_debit": call_debit,
            "put_debit": put_debit,
            "call_debit_limit_1": call_debit_limit_1,
            "put_debit_limit_1":  put_debit_limit_1,
            "call_debit_limit_2_5": call_debit_limit_2_5,
            "put_debit_limit_2_5":  put_debit_limit_2_5,
            "call_debit_limit_5": call_debit_limit_5,
            "put_debit_limit_5":  put_debit_limit_5,
            **({"_hint": hint} if hint else {}),
        }
    except Exception as e:
        # Theo-only fallback: we have current_price but something later failed (e.g., secdef/qualification).
        # Build a minimal result so CSV still gets a theoretical row.
        logger.warning(f"[THEO_ONLY] stage={stage} symbol={symbol} reason={e}")
        atm_strike = round(current_price) if current_price is not None else None
        expiry_str = expiry_str or _fallback_expiration_str(TARGET_DTE)
        if current_price is None:
            # Only full theo-only when even the underlying price is unknown
            return {
                "_error": False,
                "_theo_only": True,
                "symbol": symbol,
                "current_price": None,
                "atm_strike": None,
                "otm_strike": None,
                "put_otm_strike": None,
                "expiration": expiry_str,
                "implied_volatility_atm": None,
                "open_interest_atm": None,
                "implied_volatility_otm": None,
                "open_interest_otm": None,
                "call_debit": None,
                "put_debit": None,
                "call_debit_limit_1": None,
                "put_debit_limit_1":  None,
                "call_debit_limit_2_5": None,
                "put_debit_limit_2_5":  None,
                "call_debit_limit_5": None,
                "put_debit_limit_5":  None,
            }
        # Otherwise, return a partial result with at least price + ATM and fallback expiry so CSV has useful data
        return {
            "_error": False,
            "symbol": symbol,
            "current_price": current_price,
            "atm_strike": atm_strike,
            "otm_strike": None,
            "put_otm_strike": None,
            "expiration": expiry_str,
            "implied_volatility_atm": None,
            "open_interest_atm": None,
            "implied_volatility_otm": None,
            "open_interest_otm": None,
            "call_debit": None,
            "put_debit": None,
            "call_debit_limit_1": None,
            "put_debit_limit_1":  None,
            "call_debit_limit_2_5": None,
            "put_debit_limit_2_5":  None,
            "call_debit_limit_5": None,
            "put_debit_limit_5":  None,
        }
# --- Active polling helper for IB market data (replaces fixed sleeps) ---
def _wait_for_fields(ticker, fields=("bid","ask","last","close"), timeout_ms=3000, step_ms=200):
    """
    Polls a live ib_insync ticker object until any of the desired fields becomes non-None (and not NaN / zero for first sample),
    or timeout is reached. Returns (values: dict[str, any], waited_ms: int).
    """
    waited = 0
    def _clean(v):
        try:
            from math import isnan, isinf
            if v is None: return None
            if isinstance(v, float) and (isnan(v) or isinf(v)): return None
            return v
        except Exception:
            return v
    def _empty(x):
        # treat None/NaN/Inf and zero as empty to avoid immediate t=0 "present" values
        if x is None: return True
        if isinstance(x, (int, float)) and x == 0.0: return True
        return False

    vals = {f: _clean(getattr(ticker, f, None)) for f in fields}
    # prefer cooperative ib.sleep, but also do a hard sleep to advance time
    while waited <= timeout_ms and all(_empty(vals.get(f)) for f in fields):
        try:
            IB_SHARED.sleep(step_ms/1000.0)
        except Exception:
            pass
        import time as _time
        _time.sleep(step_ms/1000.0)
        waited += step_ms
        for f in fields:
            v = _clean(getattr(ticker, f, None))
            if not _empty(v):
                vals[f] = v
        # Early-exit if we have bid&ask, or ask&last, or close&last
        if (vals.get("bid") is not None and vals.get("ask") is not None) \
           or (vals.get("ask") is not None and vals.get("last") is not None) \
           or (vals.get("close") is not None and vals.get("last") is not None):
            break
    return vals, waited

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
    data = _get_payload()
    _append_webhook_event(data if isinstance(data, (dict, list)) else {"raw": str(data)}, route="/signal/text")
    raw_text = data.get('text') or data.get('message') or data.get('alert')
    # allow explicit ticker override via ticker/symbol
    symbol = _clean_symbol(data.get('ticker') or data.get('symbol') or _extract_ticker_from_text(raw_text))
    if not symbol:
        return _fail("payload", "ticker missing and could not extract from text", 400)
    sig = _parse_signal_fields(raw_text)
    result = get_option_data(symbol, signal_type=sig.get("signal_type"))
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
    data = _get_payload()
    _append_webhook_event(data if isinstance(data, (dict, list)) else {"raw": str(data)}, route="/signal")
    raw_text = data.get('text') or data.get('message') or data.get('alert') or data.get('alert_message')
    symbol = _clean_symbol(data.get('ticker') or data.get('symbol') or _extract_ticker_from_text(raw_text))
    if not symbol:
        return _fail("payload", "ticker missing (and text did not contain an extractable ticker)", 400)
    sig = _parse_signal_fields(raw_text)
    result = get_option_data(symbol, signal_type=sig.get("signal_type"))
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
    data = _get_payload()
    _append_webhook_event(data if isinstance(data, (dict, list)) else {"raw": str(data)}, route="/webhook")
    # Accept both ticker and symbol, or extract from free‑form message/text
    msg = data.get('message') or data.get('alert_message') or data.get('alert') or data.get('text')
    symbol = _clean_symbol(data.get('ticker') or data.get('symbol') or _extract_ticker_from_text(msg))
    if not symbol:
        return _fail("payload", "ticker missing from payload and not found in text", 400)
    sig = _parse_signal_fields(msg)
    result = get_option_data(symbol, signal_type=sig.get("signal_type"))
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
    data = _get_payload()
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
        res = get_option_data(tick, signal_type=sig.get("signal_type") if sig else None)
        if not res or res.get("_error"):
            results.append({"symbol": tick or item, "error": (res or {}).get("detail", "unknown")})
            try:
                IB_SHARED.sleep(0.05)
            except Exception:
                pass
        else:
            _append_listener_result_to_csv(res, sig)
            if sig:
                res.update({
                    "signal_side": sig.get("signal_side"),
                    "signal_type": sig.get("signal_type"),
                    "strategy_position": sig.get("strategy_position"),
                    "raw_message": sig.get("raw_message"),
                })
            # annotate a tiny hint if the listener needed to fallback or had no debits
            hint = {}
            for k in ("call_debit","put_debit","call_debit_limit_1","put_debit_limit_1","call_debit_limit_2_5","put_debit_limit_2_5","call_debit_limit_5","put_debit_limit_5"):
                if res.get(k) is None:
                    hint.setdefault("missing", []).append(k)
            if hint:
                res["_hint"] = hint
            results.append(res)
            try:
                IB_SHARED.sleep(0.05)
            except Exception:
                pass
    return jsonify({"results": results})
@app.route('/mdtest', methods=['GET'])
def mdtest():
    sym = _clean_symbol(request.args.get('symbol', 'AAPL'))
    mdtype = request.args.get('mdtype', '1')  # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
    try:
        # Ensure connected and set market data type
        _ensure_ib_connected(IB_SHARED, mkt_type=int(mdtype))
        try:
            IB_SHARED.reqMarketDataType(int(mdtype))
        except Exception:
            pass

        timeout_ms = int(request.args.get('timeout_ms', '5000'))
        poll_ms = 200
        waited = 0
        waited2 = 0

        # fresh quote
        t = IB_SHARED.reqMktData(Stock(sym, 'SMART', 'USD'), '', False, False)

        # initialize fields
        bid = ask = last = close = None
        delayed = getattr(getattr(t, 'tickAttrib', None), 'delayed', None)

        from math import isnan, isinf
        def _nz(x, prior=None):
            if x is None: return prior
            if isinstance(x, float) and (isnan(x) or isinf(x)): return prior
            return x

        # sleep-first loop to ensure we don't exit at t=0
        while True:
            try:
                IB_SHARED.sleep(poll_ms/1000.0)
            except Exception:
                pass
            time.sleep(poll_ms/1000.0)
            waited += poll_ms

            bid   = _nz(getattr(t, 'bid',   None), bid)
            ask   = _nz(getattr(t, 'ask',   None), ask)
            last  = _nz(getattr(t, 'last',  None), last)
            close = _nz(getattr(t, 'close', None), close)

            if (bid is not None and ask is not None) or (last is not None or close is not None):
                break
            if waited >= timeout_ms:
                break
            delayed = getattr(getattr(t, 'tickAttrib', None), 'delayed', delayed)

        # If nothing arrived under mdtype=4 (delayed-frozen), try mdtype=3 (delayed) once
        if all(v is None for v in (bid, ask, last, close)) and int(mdtype) == 4:
            try:
                IB_SHARED.cancelMktData(t.contract)
            except Exception:
                pass
            try:
                IB_SHARED.reqMarketDataType(3)
            except Exception:
                pass
            t = IB_SHARED.reqMktData(Stock(sym, 'SMART', 'USD'), '', False, False)
            # second window ~ 3 seconds
            waited2 = 0
            while waited2 < 3000:
                try: IB_SHARED.sleep(poll_ms/1000.0) 
                except Exception: pass
                time.sleep(poll_ms/1000.0)
                waited2 += poll_ms
                bid   = _nz(getattr(t, 'bid',   None), bid)
                ask   = _nz(getattr(t, 'ask',   None), ask)
                last  = _nz(getattr(t, 'last',  None), last)
                close = _nz(getattr(t, 'close', None), close)
                if (bid is not None and ask is not None) or (last is not None or close is not None):
                    break

        # compute mid if possible
        mid = None
        try:
            if bid is not None and ask is not None:
                mid = (float(bid) + float(ask)) / 2.0
        except Exception:
            mid = None

        total_waited = waited + (waited2 if 'waited2' in locals() else 0)
        out = {
            "symbol": sym,
            "mdtype": int(mdtype),
            "timeout_ms": timeout_ms,
            "waited_ms": total_waited,
            "delayed": delayed,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "last": last,
            "close": close
        }
        if all(v is None for v in (bid, ask, last, close)):
            out["note"] = "No ticks within timeout; tried mdtype=4 (delayed-frozen) and mdtype=3 (delayed). Verify delayed permissions and try real curl: %SystemRoot%\\System32\\curl.exe"
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
                if hasattr(IB_SHARED, "pendingTickers") and IB_SHARED.pendingTickers():
                    IB_SHARED.sleep(0.05)
            except Exception:
                pass
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
        try:
            IB_SHARED.reqMarketDataType(_preferred_md_type())
        except Exception:
            pass
    except Exception as exc:
        logger.exception(f"Initial IB connect failed: {exc}")
    logger.info(f"Listener version: {VERSION}")
    logger.info(f"Starting listener on 0.0.0.0:{port} (threaded=False)")
    app.run(host='0.0.0.0', port=port, threaded=False, use_reloader=False)