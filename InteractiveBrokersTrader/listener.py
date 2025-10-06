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
import os
import platform
from pathlib import Path

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


VERSION = "listener-2025-09-12c"

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
    m_pos = re.search(r'new\s+strategy\s+position\s+is\s*([+-]?)\s*(\d+)', low)
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

    # Prefer delayed-frozen if you don't have live subscriptions; change to 1 for live
    stage = "market_data_type"
    try:
        ib.reqMarketDataType(4)  # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen
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
        # fallback to 1-day historical bar close
        try:
            bars = ib.reqHistoricalData(
                stock, endDateTime='', durationStr='1 D', barSizeSetting='1 day',
                whatToShow='TRADES', useRTH=True, formatDate=1
            )
            if bars:
                current_price = float(bars[-1].close)
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

    # Get expirations & strikes from SecDef params
    stage = "secdef"
    sec_def_params = ib.reqSecDefOptParams(symbol, '', 'STK', con_id)
    ib.sleep(0.5)

    expirations: List[str] = []
    strikes_all: List[float] = []
    for param in sec_def_params:
        if getattr(param, 'expirations', None):
            expirations.extend(param.expirations)
        if getattr(param, 'strikes', None):
            strikes_all.extend([float(s) for s in param.strikes])

    expirations = sorted(set(expirations))
    strikes_all = sorted(set([s for s in strikes_all if s > 0]))

    def _closest_strike(target: float) -> float:
        if not strikes_all:
            return target
        return min(strikes_all, key=lambda s: abs(s - target))

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
        # fall back to rounded if strikes list missing
        atm_strike = int(round(current_price))
        otm_strike = atm_strike + width
    else:
        atm_strike = min(strikes_all, key=lambda s: abs(s - current_price))
        # choose next available higher strike for the short leg
        higher_strikes = [s for s in strikes_all if s >= atm_strike]
        if len(higher_strikes) >= 2:
            otm_strike = higher_strikes[1]  # next increment
        else:
            otm_strike = atm_strike + width

    # Qualify and request option mkt data for both legs
    stage = "qualify_options"
    legs_info = []
    for strike in (atm_strike, otm_strike):
        option = Option(symbol=symbol, lastTradeDateOrContractMonth=expiry_str,
                        strike=strike, right='C', exchange='SMART', currency='USD')
        try:
            option = ib.qualifyContracts(option)[0]
        except Exception as exc:
            return {"_error": True, "stage": stage, "detail": f"qualifyContracts failed for {symbol} {strike} {expiry_str}: {exc}"}

        # Request option market data incl. generic ticks for OI (101) and IV30 (106)
        stage = "option_mktdata"
        try:
            t = ib.reqMktData(option, '101,106', False, False)
            ib.sleep(1.0)
        except Exception as exc:
            return {"_error": True, "stage": stage, "detail": f"reqMktData failed for option {strike}: {exc}"}

        legs_info.append({
            "strike": strike,
            "bid": getattr(t, 'bid', None),
            "ask": getattr(t, 'ask', None),
            "impliedVolatility": getattr(t, 'impliedVolatility', None),
            "callOpenInterest": getattr(t, 'callOpenInterest', None)
        })

    # --- Fetch put legs for a put debit spread (long ATM put, short lower strike put) ---
    stage = "qualify_put_options"
    put_width = abs(otm_strike - atm_strike)
    put_otm_strike = max(atm_strike - put_width, 0.01)
    put_legs_info = []
    for strike, right in ((atm_strike, 'P'), (put_otm_strike, 'P')):
        option = Option(symbol=symbol, lastTradeDateOrContractMonth=expiry_str,
                        strike=strike, right=right, exchange='SMART', currency='USD')
        try:
            option = ib.qualifyContracts(option)[0]
        except Exception as exc:
            return {"_error": True, "stage": stage, "detail": f"qualifyContracts failed for PUT {symbol} {strike} {expiry_str}: {exc}"}
        stage = "option_put_mktdata"
        try:
            t = ib.reqMktData(option, '101,106', False, False)
            ib.sleep(1.0)
        except Exception as exc:
            return {"_error": True, "stage": stage, "detail": f"reqMktData failed for PUT option {strike}: {exc}"}
        put_legs_info.append({
            "strike": strike,
            "bid": getattr(t, 'bid', None),
            "ask": getattr(t, 'ask', None),
            "impliedVolatility": getattr(t, 'impliedVolatility', None),
            "putOpenInterest": getattr(t, 'putOpenInterest', None)
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
        # Calls: long ATM C, short C at ATM+W (closest strike)
        try:
            c_long = ib.qualifyContracts(Option(symbol=symbol, lastTradeDateOrContractMonth=expiry_str, strike=atm_strike, right='C', exchange='SMART', currency='USD'))[0]
            c_shortK = _closest_strike(atm_strike + W)
            c_short = ib.qualifyContracts(Option(symbol=symbol, lastTradeDateOrContractMonth=expiry_str, strike=c_shortK, right='C', exchange='SMART', currency='USD'))[0]
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
        # Puts: long ATM P, short P at ATM-W (closest strike)
        try:
            p_long = ib.qualifyContracts(Option(symbol=symbol, lastTradeDateOrContractMonth=expiry_str, strike=atm_strike, right='P', exchange='SMART', currency='USD'))[0]
            p_shortK = _closest_strike(max(atm_strike - W, 0.01))
            p_short = ib.qualifyContracts(Option(symbol=symbol, lastTradeDateOrContractMonth=expiry_str, strike=p_shortK, right='P', exchange='SMART', currency='USD'))[0]
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

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(silent=True) or {}
    symbol = data.get('ticker')
    symbol = _clean_symbol(symbol)
    if not symbol:
        return _fail("payload", "ticker missing from payload", 400)
    # Attempt to capture alert text for signal parsing
    msg = data.get('message') or data.get('alert_message') or data.get('alert') or data.get('text')
    sig = _parse_signal_fields(msg)
    result = get_option_data(symbol)
    if not result or result.get("_error"):
        # bubble up stage & detail if available
        msg = (result or {}).get("detail", "unable to retrieve option data")
        stage = (result or {}).get("stage", "unknown")
        return _fail(stage, msg, 502)
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
    data = request.get_json(silent=True) or {}
    symbols = data.get('tickers') or data.get('symbols')
    if not symbols or not isinstance(symbols, list):
        return _fail("payload", "tickers (list) missing from payload", 400)
    results = []
    for item in symbols:
        # item can be 'SYM' or {'ticker':'SYM','message':'...'}
        if isinstance(item, str):
            tick = _clean_symbol(item)
            msg = None
        elif isinstance(item, dict):
            tick = _clean_symbol(str(item.get('ticker') or item.get('symbol') or ''))
            msg = item.get('message') or item.get('alert_message') or item.get('alert') or item.get('text')
        else:
            continue
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
    try:
        if not IB_SHARED.isConnected():
            IB_SHARED.connect('127.0.0.1', 7497, clientId=42)
        t = IB_SHARED.reqMktData(Stock(sym, 'SMART', 'USD'), '', False, False)
        IB_SHARED.sleep(1.0)
        delayed = getattr(getattr(t, 'tickAttrib', None), 'delayed', None)
        return jsonify({"symbol": sym, "delayed": delayed, "last": getattr(t, 'last', None), "close": getattr(t, 'close', None)})
    except Exception as e:
        return jsonify({"symbol": sym, "error": str(e)}), 500


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
            "single": "POST /webhook",
            "batch": "POST /webhook_batch",
            "health": "GET /health"
        }
    })

@app.route('/health', methods=['GET'])
def health():
    """
    Health endpoint. Returns version and CSV path.
    """
    return jsonify({"version": VERSION, "combined_csv_path": str(_combined_csv())})

if __name__ == '__main__':
    import os
    port = int(os.environ.get("PORT", "5001"))
    try:
        if not IB_SHARED.isConnected():
            IB_SHARED.connect('127.0.0.1', 7497, clientId=42)
        # Use delayed-frozen by default; switch to 1 for live when subscriptions are present
        IB_SHARED.reqMarketDataType(1)
    except Exception as exc:
        logger.exception(f"Initial IB connect failed: {exc}")
    logger.info(f"Listener version: {VERSION}")
    logger.info(f"Starting listener on 0.0.0.0:{port} (threaded=False)")
    app.run(host='0.0.0.0', port=port, threaded=False)
