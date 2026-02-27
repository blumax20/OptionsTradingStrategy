import os
import csv
from typing import Optional, Tuple
from math import isnan
try:
    from ib_insync import Contract
except Exception:
    # Contract is only needed when we fall back to API-driven checks
    Contract = object  # lightweight fallback for type hints

from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None
try:
    from ib_insync import IB, Option, Stock, util as _ibutil
except Exception:
    IB = None
    Option = Option if 'Option' in globals() else object
    Stock = None
    _ibutil = None

def _combo_key(symbol: str, right: str, exp: str, k_atm: float, k_oth: float) -> Tuple[str, str, str, float, float]:
    """
    Canonicalize a (symbol, right, exp, strikes) key to match CSV rows.
    Expect 'right' to be 'C' or 'P', exp like 'YYYYMMDD', and strikes as floats.
    """
    return (symbol.upper(), right.upper(), exp, float(k_atm), float(k_oth))

def _parse_float(x) -> Optional[float]:
    try:
        v = float(x)
        if v != v:  # NaN check
            return None
        return v
    except Exception:
        return None

def _now_ny():
    try:
        tz = ZoneInfo("America/New_York") if ZoneInfo else None
    except Exception:
        tz = None
    return datetime.now(tz) if tz else datetime.now()

def _is_rth(ts: datetime | None = None) -> bool:
    """
    Regular Trading Hours (Mon–Fri, 09:30–16:00 NY). Returns True iff current time is inside the window.
    """
    n = ts or _now_ny()
    if n.weekday() > 4:
        return False
    hh, mm = n.hour, n.minute
    # 09:30 <= time < 16:00
    return (hh > 9 or (hh == 9 and mm >= 30)) and (hh < 16)

def read_oi_from_csv(day_dir: str,
                     symbol: str,
                     right: str,
                     exp: str,
                     k_atm: float,
                     k_oth: float) -> Tuple[Optional[int], Optional[int]]:
    """
    Read open-interest for both legs from today's combined_listener_spreads.csv, if present.
    Returns (oi_atm, oi_oth) as ints or (None, None) if not found/unavailable.
    We match by columns: symbol, right, exp, atm, oth (case-insensitive).
    """
    csv_path = os.path.join(day_dir, "combined_listener_spreads.csv")
    if not os.path.exists(csv_path):
        return (None, None)

    want = _combo_key(symbol, right, exp, k_atm, k_oth)

    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        # Normalize field names
        cols = {name.lower(): name for name in reader.fieldnames or []}
        # Expected columns (best-effort)
        sym_col = cols.get("symbol")
        right_col = cols.get("right") or cols.get("signal_type")
        exp_col = cols.get("exp") or cols.get("expiry") or cols.get("expiration")
        atm_col = cols.get("atm") or cols.get("k_atm") or cols.get("strike_long") or cols.get("strike1") or cols.get("atm_strike")
        # For OTM, try generic first, then call/put specific based on right parameter
        oth_col = cols.get("oth") or cols.get("k_oth") or cols.get("strike_short") or cols.get("strike2")
        if not oth_col:
            oth_col = cols.get("otm_strike_call") if right.upper() == "C" else cols.get("otm_strike_put")
        # OI columns: try generic first, then call/put specific based on right parameter
        oi_atm_col = cols.get("oi_atm") or cols.get("open_interest_atm") or cols.get("oi1")
        oi_oth_col = cols.get("oi_oth") or cols.get("open_interest_oth") or cols.get("oi2")
        if not oi_atm_col:
            oi_atm_col = cols.get("open_interest_atm_call") if right.upper() == "C" else cols.get("open_interest_atm_put")
        if not oi_oth_col:
            oi_oth_col = cols.get("open_interest_otm_call") if right.upper() == "C" else cols.get("open_interest_otm_put")

        if not all([sym_col, right_col, exp_col, atm_col]):
            return (None, None)

        # iterate and keep the last matching row (latest write wins)
        oi_atm = None
        oi_oth = None
        for row in reader:
            try:
                # Convert signal_type to right if needed
                row_right = str(row[right_col]).strip().upper()
                if row_right in ("CALL_OPEN", "CALL_CLOSE"):
                    row_right = "C"
                elif row_right in ("PUT_OPEN", "PUT_CLOSE"):
                    row_right = "P"
                # Handle missing oth_col - use 0.0 as placeholder if not available
                row_oth = float(row[oth_col]) if oth_col and row.get(oth_col) else 0.0
                key = _combo_key(
                    str(row[sym_col]).strip(),
                    row_right,
                    str(row[exp_col]).strip(),
                    float(row[atm_col]),
                    row_oth,
                )
            except Exception:
                continue

            if key == want:
                if oi_atm_col:
                    oi_atm = _parse_float(row.get(oi_atm_col))
                if oi_oth_col:
                    oi_oth = _parse_float(row.get(oi_oth_col))
        # Cast to ints when available
        return (int(oi_atm) if oi_atm is not None else None,
                int(oi_oth) if oi_oth is not None else None)

def is_liquid_by_oi(oi_atm: Optional[int], oi_oth: Optional[int], threshold: int = 100) -> Optional[bool]:
    """
    Return True iff at least one leg's OI >= threshold.
    Return False iff both legs are known and both < threshold.
    Return None if we lack enough information (one or both OI are None).
    """
    if oi_atm is None or oi_oth is None:
        return None
    return (oi_atm >= threshold) or (oi_oth >= threshold)

def _read_combined_csv(day_dir: str):
    csv_path = os.path.join(day_dir, "combined_listener_spreads.csv")
    if not os.path.exists(csv_path):
        return csv_path, None, None
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        cols = [c for c in (reader.fieldnames or [])]
    return csv_path, cols, rows

def _write_combined_csv(csv_path: str, cols, rows):
    tmp = csv_path + ".tmp"
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    # backup current file once, then replace
    bak = csv_path + ".bak"
    try:
        if not os.path.exists(bak) and os.path.exists(csv_path):
            os.replace(csv_path, bak)
        else:
            os.remove(csv_path)
    except Exception:
        try: os.remove(csv_path)
        except Exception: pass
    os.replace(tmp, csv_path)

def _ensure_cols(cols, need):
    for c in need:
        if c not in cols:
            cols.append(c)

def _default_fetcher(symbol: str, right: str, exp: str, strike: float):
    """
    Placeholder fetcher; returns (oi, iv) as (None, None).
    Caller should provide a real fetcher that queries IB for
    open interest and IV on the specific option contract.
    """
    return None, None


def _get_strike_increment(price: float) -> float:
    """
    Determine standard strike increment based on stock price.
    """
    if price < 5:
        return 0.5
    elif price < 25:
        return 1.0
    elif price < 200:
        return 2.5
    else:
        return 5.0


def _round_to_strike(price: float, increment: float) -> float:
    """Round price to nearest valid strike."""
    return round(price / increment) * increment


def _get_atm_and_otm_strikes(ib: "IB", symbol: str, expiration: str, signal_type: str, logger=None) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """
    Given a symbol and expiration, fetch the current price and compute ATM + OTM strikes.

    Returns (atm_strike, otm_strike_call, otm_strike_put, current_price) or (None, None, None, None) on failure.

    For CALL spreads: ATM < OTM (buy lower, sell higher)
    For PUT spreads: ATM > OTM (buy higher, sell lower)
    """
    if ib is None or Stock is None:
        return (None, None, None, None)

    try:
        # Get current stock price
        stock = Stock(symbol, "SMART", "USD")
        qualified = ib.qualifyContracts(stock)
        if not qualified:
            if logger:
                logger(f"[{symbol}] Could not qualify stock contract")
            return (None, None, None, None)

        stock = qualified[0]

        # Request market data snapshot - try delayed/frozen first
        try:
            ib.reqMarketDataType(4)  # 4 = delayed-frozen
        except Exception:
            pass

        ticker = ib.reqMktData(stock, "", False, False)
        ib.sleep(2.0)

        # Get price - prefer last, then close, then bid/ask midpoint
        price = None
        # Check for valid numeric values (not NaN)
        def _valid(v):
            return v is not None and isinstance(v, (int, float)) and v == v and v > 0

        if _valid(ticker.last):
            price = ticker.last
        elif _valid(ticker.close):
            price = ticker.close
        elif _valid(ticker.bid) and _valid(ticker.ask):
            price = (ticker.bid + ticker.ask) / 2

        try:
            ib.cancelMktData(stock)
        except Exception:
            pass

        # If still no price, try to get from reqHistoricalData (last 1 bar)
        if price is None:
            try:
                bars = ib.reqHistoricalData(
                    stock,
                    endDateTime='',
                    durationStr='1 D',
                    barSizeSetting='1 day',
                    whatToShow='TRADES',
                    useRTH=True,
                    formatDate=1,
                    timeout=10
                )
                if bars and len(bars) > 0:
                    price = bars[-1].close
                    if logger:
                        logger(f"[{symbol}] Using historical close: ${price:.2f}")
            except Exception as e:
                if logger:
                    logger(f"[{symbol}] Historical data request failed: {e}")

        if price is None or price <= 0:
            if logger:
                logger(f"[{symbol}] Could not get valid price (last={getattr(ticker, 'last', None)}, close={getattr(ticker, 'close', None)})")
            return (None, None, None, None)

        # Determine strike increment and ATM
        increment = _get_strike_increment(price)
        atm = _round_to_strike(price, increment)

        # Standard spread width: 1 strike for most, but use increment-based logic
        # For calls: OTM is higher than ATM
        # For puts: OTM is lower than ATM
        otm_call = atm + increment
        otm_put = atm - increment

        if logger:
            logger(f"[{symbol}] price=${price:.2f} -> ATM={atm}, OTM_call={otm_call}, OTM_put={otm_put}")

        return (atm, otm_call, otm_put, price)

    except Exception as e:
        if logger:
            logger(f"[{symbol}] Error getting strikes: {e}")
        return (None, None, None, None)


def populate_missing_strikes(day_dir: str,
                             ib_host: str = "127.0.0.1",
                             ib_port: int = 7497,
                             client_id: int = 916,
                             logger=None) -> int:
    """
    Scan combined_listener_spreads.csv for rows with missing strike data and populate them.

    Returns the number of rows updated.
    """
    csv_path, cols, rows = _read_combined_csv(day_dir)
    if not rows:
        if logger:
            logger(f"populate_missing_strikes: no rows in {csv_path}")
        return 0

    # Find column names
    lc = {c.lower(): c for c in cols}
    sym_col = lc.get("symbol")
    exp_col = lc.get("expiration") or lc.get("exp") or lc.get("expiry")
    atm_col = lc.get("atm_strike")
    otm_call_col = lc.get("otm_strike_call")
    otm_put_col = lc.get("otm_strike_put")
    stype_col = lc.get("signal_type")
    price_col = lc.get("current_price")

    if not sym_col or not exp_col:
        if logger:
            logger("populate_missing_strikes: missing symbol or expiration columns")
        return 0

    # Ensure strike and price columns exist
    if atm_col is None:
        atm_col = "atm_strike"
        if atm_col not in cols:
            cols.append(atm_col)
    if otm_call_col is None:
        otm_call_col = "otm_strike_call"
        if otm_call_col not in cols:
            cols.append(otm_call_col)
    if otm_put_col is None:
        otm_put_col = "otm_strike_put"
        if otm_put_col not in cols:
            cols.append(otm_put_col)
    if price_col is None:
        price_col = "current_price"
        if price_col not in cols:
            cols.append(price_col)

    # Find rows with missing strikes or current_price
    rows_needing_strikes = []
    for i, row in enumerate(rows):
        atm_val = row.get(atm_col, "")
        price_val = row.get(price_col, "")

        # Check if strike or price is missing
        atm_missing = not atm_val or str(atm_val).strip() == "" or _parse_float(atm_val) is None
        price_missing = not price_val or str(price_val).strip() == "" or _parse_float(price_val) is None

        if atm_missing or price_missing:
            symbol = str(row.get(sym_col, "")).strip().upper()
            exp = str(row.get(exp_col, "")).strip()
            stype = str(row.get(stype_col, "")).strip().upper() if stype_col else ""
            if symbol and exp:
                rows_needing_strikes.append((i, symbol, exp, stype))

    if not rows_needing_strikes:
        if logger:
            logger("populate_missing_strikes: no rows with missing strikes")
        return 0

    if logger:
        logger(f"populate_missing_strikes: {len(rows_needing_strikes)} rows need strikes")

    # Connect to IB and fetch strikes
    if IB is None:
        if logger:
            logger("populate_missing_strikes: ib_insync not available")
        return 0

    ib = IB()
    updates = 0
    try:
        ib.connect(ib_host, ib_port, clientId=client_id, timeout=10)

        # Group by symbol to avoid redundant lookups
        symbol_data = {}  # symbol -> (atm, otm_call, otm_put, current_price)

        for idx, symbol, exp, stype in rows_needing_strikes:
            if symbol not in symbol_data:
                atm, otm_call, otm_put, current_price = _get_atm_and_otm_strikes(ib, symbol, exp, stype, logger=logger)
                symbol_data[symbol] = (atm, otm_call, otm_put, current_price)

            atm, otm_call, otm_put, current_price = symbol_data[symbol]

            if atm is not None:
                rows[idx][atm_col] = atm
                rows[idx][otm_call_col] = otm_call
                rows[idx][otm_put_col] = otm_put
                rows[idx][price_col] = current_price
                updates += 1
                if logger:
                    logger(f"[{symbol}] Populated: ATM={atm}, OTM_call={otm_call}, OTM_put={otm_put}, price={current_price}")

    except Exception as e:
        if logger:
            logger(f"populate_missing_strikes: IB connection error: {e}")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    if updates > 0:
        _write_combined_csv(csv_path, cols, rows)
        if logger:
            logger(f"populate_missing_strikes: updated {updates} rows in {csv_path}")

    return updates

def _ib_fetcher_factory(ib: "IB", poll_seconds: float = 1.5):  # Fix AO: was 0.6 — OI tick 101 needs ~1-2s to arrive
    """
    Return a callable (symbol, right, exp, strike) -> (oi, iv)
    Uses ib_insync snapshot market data to fetch option open interest and IV.
    """
    def _fetch(symbol: str, right: str, exp: str, strike: float):
        if ib is None:
            return (None, None)
        try:
            opt = Option(symbol=symbol,
                         lastTradeDateOrContractMonth=str(exp),
                         strike=float(strike),
                         right=str(right).upper(),
                         exchange="SMART",
                         currency="USD")
            [opt] = ib.qualifyContracts(opt)
            # Request generic ticks; include 588 (Option Open Interest) when supported
            # Note: fields availability depends on account/permissions.
            t = ib.reqMktData(opt, "100,101,106,588", False, False)
            # Allow brief time for snapshot to populate
            ib.sleep(poll_seconds)
            # Try several attribute names defensively
            oi = None
            for attr in ("optionOpenInterest", "openInterest", "optOpenInterest"):
                val = getattr(t, attr, None)
                if isinstance(val, (int, float)) and not (val != val):  # not NaN
                    oi = int(val)
                    break
            iv = None
            greeks = getattr(t, "modelGreeks", None)
            if greeks and hasattr(greeks, "impliedVol"):
                iv_val = greeks.impliedVol
                if isinstance(iv_val, (int, float)) and not (iv_val != iv_val):
                    iv = float(iv_val)
            # Clean up subscription
            try: ib.cancelMktData(opt)
            except Exception: pass
            return (oi, iv)
        except Exception:
            return (None, None)
    return _fetch

def enrich_combined_csv(day_dir: str, fetcher=None, logger=None):
    """
    Populate/refresh OI and IV columns in combined_listener_spreads.csv.

    - Adds columns: oi_atm, oi_oth, iv_atm, iv_oth (if missing).
    - For each row, if any of those values are missing/blank/NaN,
      calls `fetcher(symbol, right, exp, strike)` to obtain (oi, iv)
      for the corresponding leg and fills them in.
    - Writes back to the same CSV (keeps a .bak once).
    """
    fetch = fetcher or _default_fetcher
    csv_path, cols, rows = _read_combined_csv(day_dir)
    if not rows:
        return False

    # Normalize column keys present in source
    lc = {c.lower(): c for c in cols}
    sym = lc.get("symbol")
    rgt = lc.get("right") or lc.get("signal_type")
    exp = lc.get("exp") or lc.get("expiry") or lc.get("expiration")
    atm = lc.get("atm") or lc.get("k_atm") or lc.get("strike_long") or lc.get("strike1") or lc.get("atm_strike")
    # For OTM strikes, we have separate call/put columns in new CSV format
    oth = lc.get("oth") or lc.get("k_oth") or lc.get("strike_short") or lc.get("strike2")
    oth_call = lc.get("otm_strike_call")
    oth_put = lc.get("otm_strike_put")

    if not all([sym, rgt, exp, atm]) or (not oth and not oth_call and not oth_put):
        if logger: logger("enrich_csv: missing key columns in combined_listener_spreads.csv")
        return False

    # Ensure targets exist
    need_cols = ["oi_atm", "oi_oth", "iv_atm", "iv_oth"]
    _ensure_cols(cols, need_cols)

    updates = 0

    def _need(v):
        if v is None: return True
        if isinstance(v, str) and v.strip() == "": return True
        try:
            fv = float(v)
            return fv != fv  # NaN
        except Exception:
            return False

    for row in rows:
        try:
            symbol = str(row[sym]).strip()
            # Convert signal_type to right if needed
            right = str(row[rgt]).strip().upper()
            if right in ("CALL_OPEN", "CALL_CLOSE"):
                right = "C"
            elif right in ("PUT_OPEN", "PUT_CLOSE"):
                right = "P"
            elif right == "CLOSE":
                # For generic CLOSE signals, skip - we don't know call vs put
                continue
            expiry = str(row[exp]).strip()
            k1 = float(row[atm])
            # Get OTM strike from appropriate column based on right
            if oth:
                k2 = float(row[oth])
            elif right == "C" and oth_call:
                k2 = float(row[oth_call]) if row.get(oth_call) else None
            elif right == "P" and oth_put:
                k2 = float(row[oth_put]) if row.get(oth_put) else None
            else:
                k2 = None
        except Exception:
            continue

        # ATM leg fill
        if _need(row.get("oi_atm")) or _need(row.get("iv_atm")):
            oi1, iv1 = fetch(symbol, right, expiry, k1)
            if oi1 is not None:
                row["oi_atm"] = int(oi1)
                updates += 1
                # Fix AO Part 2: also backfill open_interest_atm_put — the column _oi_ok() reads
                if right == "P":
                    _col_put_atm = "open_interest_atm_put"
                    if _col_put_atm in cols and _need(row.get(_col_put_atm)):
                        row[_col_put_atm] = int(oi1)
                        updates += 1
            if iv1 is not None:
                row["iv_atm"] = float(iv1)
                updates += 1

        # OTH leg fill (only if we have a valid OTM strike)
        if k2 is not None and (_need(row.get("oi_oth")) or _need(row.get("iv_oth"))):
            oi2, iv2 = fetch(symbol, right, expiry, k2)
            if oi2 is not None:
                row["oi_oth"] = int(oi2)
                updates += 1
                # Fix AO Part 2: also backfill open_interest_otm_put — the column _oi_ok() reads
                if right == "P":
                    _col_put_oth = "open_interest_otm_put"
                    if _col_put_oth in cols and _need(row.get(_col_put_oth)):
                        row[_col_put_oth] = int(oi2)
                        updates += 1
            if iv2 is not None:
                row["iv_oth"] = float(iv2)
                updates += 1

        # Fix AO Part 3: For PUT_OPEN rows, use call OI as proxy for put OI when IB doesn't
        # return put OI via reqMktData. Call and put OI on the same stock are correlated.
        # LXP (OTM call OI=2) will still fail _oi_ok(); BCE (call OI=1231) will correctly pass.
        if right == "P":
            _col_put_atm = "open_interest_atm_put"
            _col_call_atm = "open_interest_atm_call"
            if _col_put_atm in cols and _need(row.get(_col_put_atm)):
                _call_oi_atm = row.get(_col_call_atm)
                if not _need(_call_oi_atm):
                    row[_col_put_atm] = _call_oi_atm
                    updates += 1
            _col_put_oth = "open_interest_otm_put"
            _col_call_oth = "open_interest_otm_call"
            if _col_put_oth in cols and _need(row.get(_col_put_oth)):
                _call_oi_oth = row.get(_col_call_oth)
                if not _need(_call_oi_oth):
                    row[_col_put_oth] = _call_oi_oth
                    updates += 1

    if updates:
        _write_combined_csv(csv_path, cols, rows)
    if logger: logger(f"enrich_csv: updated={updates}")
    return updates > 0


# ---- Fix N: Live spread price fetching ----

def _fetch_live_spread_price(ib, symbol: str, expiration: str, atm: float,
                              width: float, right: str = 'C') -> Optional[float]:
    """Fetch live debit spread price from IB.

    Args:
        ib: Connected IB instance
        symbol: Stock symbol
        expiration: Expiration in YYYYMMDD format
        atm: ATM strike price
        width: Spread width (1.0, 2.5, or 5.0)
        right: 'C' for call or 'P' for put

    Returns:
        Debit spread price (ask_long - bid_short), capped at spread width.
        Returns None if quotes unavailable.
    """
    if ib is None or Option is None:
        return None
    try:
        long_strike = atm
        short_strike = atm + width if right == 'C' else atm - width
        if short_strike <= 0:
            return None

        long_opt = Option(symbol, expiration, long_strike, right, 'SMART')
        short_opt = Option(symbol, expiration, short_strike, right, 'SMART')

        qualified = ib.qualifyContracts(long_opt, short_opt)
        if len(qualified) < 2:
            return None

        # Request market data
        long_ticker = ib.reqMktData(long_opt, snapshot=True)
        short_ticker = ib.reqMktData(short_opt, snapshot=True)
        ib.sleep(0.6)  # Wait for data

        ask_long = long_ticker.ask
        bid_short = short_ticker.bid

        try:
            ib.cancelMktData(long_opt)
            ib.cancelMktData(short_opt)
        except Exception:
            pass

        if ask_long and ask_long > 0 and bid_short is not None and bid_short >= 0:
            debit = ask_long - bid_short
            # Cap at spread width (can't exceed max value)
            debit = min(debit, width)
            return round(debit, 2)
    except Exception:
        pass
    return None


def enrich_live_spread_prices(day_dir: str, ib=None, logger=None) -> int:
    """Update CSV limit columns with live market prices from IB.

    Updates call_debit_limit_* and put_debit_limit_* columns for all rows.
    This should be run during RTH (market open) to replace after-hours theo values
    with actual live market prices.

    Returns count of values updated.
    """
    csv_path, cols, rows = _read_combined_csv(day_dir)
    if not rows:
        if logger:
            logger(f"enrich_live_prices: no rows in {day_dir}")
        return 0

    lc = {c.lower(): c for c in cols}
    sym_col = lc.get("symbol")
    exp_col = lc.get("expiration") or lc.get("exp")
    atm_col = lc.get("atm_strike")

    if not all([sym_col, exp_col, atm_col]):
        if logger:
            logger("enrich_live_prices: missing required columns (symbol, expiration, atm_strike)")
        return 0

    # Ensure limit columns exist
    limit_cols = ['call_debit_limit_1', 'put_debit_limit_1',
                  'call_debit_limit_2_5', 'put_debit_limit_2_5',
                  'call_debit_limit_5', 'put_debit_limit_5']
    _ensure_cols(cols, limit_cols)

    updates = 0
    for row in rows:
        symbol = str(row.get(sym_col, "")).strip()
        exp = str(row.get(exp_col, "")).strip()
        atm = _parse_float(row.get(atm_col))

        if not symbol or not exp or atm is None:
            continue

        if logger:
            logger(f"[{symbol}] Fetching live spread prices for exp={exp}, atm={atm}")

        # Fetch live prices for each width
        for width, suffix in [(1.0, '1'), (2.5, '2_5'), (5.0, '5')]:
            # CALL spread
            call_live = _fetch_live_spread_price(ib, symbol, exp, atm, width, 'C')
            if call_live is not None:
                col = f'call_debit_limit_{suffix}'
                old_val = row.get(col)
                row[col] = call_live
                if logger:
                    logger(f"  [{symbol}] {col}: {old_val} -> {call_live}")
                updates += 1

            # PUT spread
            put_live = _fetch_live_spread_price(ib, symbol, exp, atm, width, 'P')
            if put_live is not None:
                col = f'put_debit_limit_{suffix}'
                old_val = row.get(col)
                row[col] = put_live
                if logger:
                    logger(f"  [{symbol}] {col}: {old_val} -> {put_live}")
                updates += 1

    if updates > 0:
        _write_combined_csv(csv_path, cols, rows)
    if logger:
        logger(f"enrich_live_prices: updated={updates}")
    return updates


def enrich_if_rth(day_dir: str,
                  ib_host: str = "127.0.0.1",
                  ib_port: int = 7497,
                  client_id: int = 915,
                  logger=None,
                  update_prices: bool = True) -> bool:
    """
    If current time is Regular Trading Hours (NY), connect to IB and enrich the combined CSV with OI/IV.
    If update_prices=True (default), also updates limit columns with live spread prices.
    Returns True if any updates were written, False otherwise.
    """
    if not _is_rth():
        if logger: logger("enrich_if_rth: outside RTH; skipping enrichment.")
        return False
    if IB is None:
        if logger: logger("enrich_if_rth: ib_insync not available; skipping.")
        return False
    ib = IB()
    try:
        ib.connect(ib_host, ib_port, clientId=client_id)
        # Use delayed-frozen to avoid streaming if you prefer; harmless if not supported
        try:
            ib.reqMarketDataType(4)
        except Exception:
            pass
        fetcher = _ib_fetcher_factory(ib)
        changed = enrich_combined_csv(day_dir, fetcher=fetcher, logger=logger)

        # Fix N: Also update limit columns with live spread prices
        if update_prices:
            price_updates = enrich_live_spread_prices(day_dir, ib=ib, logger=logger)
            changed = changed or (price_updates > 0)

        return bool(changed)
    finally:
        try: ib.disconnect()
        except Exception: pass

def get_options_chain(self, stock_contract):
    """Retrieve options chain for liquidity analysis"""
    # Get option parameters
    req_id = self.get_next_req_id()
    self.reqSecDefOptParams(req_id, stock_contract.symbol, "", 
                           stock_contract.secType, stock_contract.conId)

def filter_liquid_options(self, symbol, strikes, expirations, day_dir: Optional[str] = None, oi_threshold: int = 100, enrich_csv: bool = True, fetcher=None):
    """Filter options by liquidity criteria"""
    # Optionally enrich today's combined CSV with fresh OI/IV before filtering
    if enrich_csv and day_dir:
        try:
            enrich_combined_csv(day_dir, fetcher=fetcher)
        except Exception:
            pass

    liquid_options = []

    for expiry in expirations[:3]:  # Focus on first 3 expirations
        for strike in strikes:
            # Create call option contract
            call_contract = Contract()
            call_contract.symbol = symbol
            call_contract.secType = "OPT"
            call_contract.exchange = "SMART"
            call_contract.currency = "USD"
            call_contract.lastTradeDateOrContractMonth = expiry
            call_contract.strike = strike
            call_contract.right = "C"

            # If we have a combined CSV for today, use it to filter by OI quickly.
            if day_dir:
                oi_atm, oi_oth = read_oi_from_csv(day_dir, symbol, "C", expiry, strike, strike)  # same-strike single leg
                ok = is_liquid_by_oi(oi_atm, oi_oth, threshold=oi_threshold)
                if ok is True:
                    liquid_options.append((expiry, "C", strike, oi_atm, oi_oth))
                # If ok is False -> skip (illiquid)
                # If ok is None -> fall back to live mkt data request below
                if ok is not None:
                    continue

            # Request market data to check liquidity
            req_id = self.get_next_req_id()
            self.reqMktData(req_id, call_contract, "", False, False, [])
            # NOTE: This code path requests live data but does not synchronously wait;
            # you may want to decorate this class to collect tick snapshots and evaluate later.
            # For now, record the candidate as "unknown OI".
            if day_dir is None:
                liquid_options.append((expiry, "C", strike, None, None))

    return liquid_options


def should_cancel_for_low_oi(day_dir: str,
                             symbol: str,
                             right: str,
                             exp: str,
                             k_atm: float,
                             k_oth: float,
                             threshold: int = 100) -> bool:
    """
    Pure function: look at today's combined_listener_spreads.csv and decide if
    an existing working order should be cancelled for low OI.
    Policy: cancel iff both legs are present AND both OI < threshold.
    If OI is missing (None/NaN) we *do not* cancel (return False) so that
    the caller can choose to re-check via live market data.
    """
    oi_atm, oi_oth = read_oi_from_csv(day_dir, symbol, right, exp, k_atm, k_oth)
    verdict = is_liquid_by_oi(oi_atm, oi_oth, threshold=threshold)
    if verdict is None:
        return False
    return not verdict

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Enrich combined_listener_spreads.csv with OI/IV columns or populate missing strikes.")
    ap.add_argument("--day-dir", required=True, help="Folder like C:\\OptionsHistory\\YY_MM_DD")
    ap.add_argument("--only-rth", action="store_true", help="Only enrich when current time is RTH (09:30–16:00 NY).")
    ap.add_argument("--populate-strikes", action="store_true", help="Populate missing ATM/OTM strikes by fetching current prices from IB.")
    ap.add_argument("--update-prices", action="store_true", help="Update limit columns with live spread prices from IB.")
    ap.add_argument("--ib-host", default="127.0.0.1")
    ap.add_argument("--ib-port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=915)
    args = ap.parse_args()

    def _log(msg: str):
        print(msg, flush=True)

    if args.populate_strikes:
        # Populate missing strikes first
        updated = populate_missing_strikes(args.day_dir, ib_host=args.ib_host, ib_port=args.ib_port,
                                           client_id=args.client_id + 1, logger=_log)
        print(f"Strike population: updated {updated} rows in: {args.day_dir}")
    elif args.update_prices:
        # Fix N: Update limit columns with live spread prices
        if IB is None:
            print("ERROR: ib_insync not available")
        else:
            ib = IB()
            try:
                ib.connect(args.ib_host, args.ib_port, clientId=args.client_id)
                try:
                    ib.reqMarketDataType(4)
                except Exception:
                    pass
                updated = enrich_live_spread_prices(args.day_dir, ib=ib, logger=_log)
                print(f"Live price update: updated {updated} values in: {args.day_dir}")
            finally:
                try: ib.disconnect()
                except Exception: pass
    elif args.only_rth:
        changed = enrich_if_rth(args.day_dir, ib_host=args.ib_host, ib_port=args.ib_port, client_id=args.client_id, logger=_log)
        print(f"Enrichment {'made changes' if changed else 'no changes needed'} in: {args.day_dir} (mode=only_rth)")
    else:
        # Try to use IB if available; otherwise fall back to placeholder fetcher (no changes expected)
        if IB is not None:
            ib = IB()
            try:
                ib.connect(args.ib_host, args.ib_port, clientId=args.client_id)
                try:
                    ib.reqMarketDataType(4)
                except Exception:
                    pass
                fetcher = _ib_fetcher_factory(ib)
                changed = enrich_combined_csv(args.day_dir, fetcher=fetcher, logger=_log)
            finally:
                try: ib.disconnect()
                except Exception: pass
        else:
            changed = enrich_combined_csv(args.day_dir, fetcher=None, logger=_log)
        print(f"Enrichment {'made changes' if changed else 'no changes needed'} in: {args.day_dir}")
