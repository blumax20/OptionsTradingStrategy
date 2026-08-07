import os
import csv
from typing import Optional, Tuple
from math import isnan
from ib_config import IB_HOST, IB_PORT
from theo_pricing import _theo_spread_debits  # Fix FI: recompute theo with real per-leg IV
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

def _ba_pct(bid, ask):
    """
    Fix FC: bid-ask spread as a percent of mid (mirrors listener._ba_pct).
    Returns None when there is no usable ask. A missing/NaN/negative bid
    (IB's -1 'no-bid' sentinel) with a valid ask is treated as 0 -> wide.
    """
    try:
        a = float(ask) if ask is not None else None
    except Exception:
        a = None
    if a is None or a != a or a <= 0:
        return None
    try:
        b = float(bid) if bid is not None else 0.0
    except Exception:
        b = 0.0
    if b != b or b < 0:
        b = 0.0
    mid = (a + b) / 2.0
    if mid <= 0:
        return None
    return round((a - b) / mid * 100.0, 1)


def _default_fetcher(symbol: str, right: str, exp: str, strike: float):
    """
    Placeholder fetcher; returns (oi, iv, ba) as (None, None, None).
    Caller should provide a real fetcher that queries IB for
    open interest, IV, and bid-ask % on the specific option contract.
    """
    return None, None, None


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


def _qualified_strikes_for_expiry(ib: "IB", stock, symbol: str, expiration: str,
                                  price: float, logger=None,
                                  max_probe: int = 8):
    """
    Fix FE: pick strikes that actually exist for THIS expiration.

    The price-based increment heuristic (_get_strike_increment) doesn't know an
    expiry's real spacing — e.g. HSBC Sept is 5-wide (105/110) but a $106 stock
    maps to a 2.5 increment -> 107.5, which does not exist. Fetch the listed
    strikes and qualifyContracts() against the specific expiration: nearest
    qualifying strike to price = ATM, then the adjacent qualifying strikes
    (natural spacing, matching the listener) for the OTM legs.

    Returns (atm, otm_call, otm_put) or None if secdef/qualify is unavailable.
    Only qualifyContracts is used (no market data), so it works after hours.
    """
    if ib is None or Option is None:
        return None
    try:
        params = ib.reqSecDefOptParams(symbol, "", "STK", stock.conId)
        strikes = sorted({float(s) for p in (params or []) for s in p.strikes})
        if not strikes:
            return None

        def _qual(k) -> bool:
            try:
                o = Option(symbol, expiration, float(k), 'C', "SMART", "100", "USD")
                return bool(ib.qualifyContracts(o))
            except Exception:
                return False

        atm = None
        for k in sorted(strikes, key=lambda s: abs(s - price))[:max_probe]:
            if _qual(k):
                atm = k
                break
        if atm is None:
            return None
        otm_call = next((s for s in strikes if s > atm and _qual(s)), None)
        otm_put = next((s for s in reversed(strikes) if s < atm and _qual(s)), None)
        if otm_call is None and otm_put is None:
            return None
        if logger:
            logger(f"[{symbol}] Fix FE per-expiry strikes ({expiration}): "
                   f"ATM={atm} OTM_call={otm_call} OTM_put={otm_put}")
        return (atm, otm_call, otm_put)
    except Exception as e:
        if logger:
            logger(f"[{symbol}] Fix FE per-expiry qualify failed: {e}")
        return None


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

        # Fix FE: prefer strikes that actually qualify for THIS expiration (handles
        # per-expiry spacing the price heuristic can't know, e.g. HSBC Sept 5-wide ->
        # 105/110 instead of 107.5). Fall back to the increment heuristic if secdef
        # or qualify is unavailable.
        _real = _qualified_strikes_for_expiry(ib, stock, symbol, expiration, price, logger)
        if _real is not None:
            atm, otm_call, otm_put = _real
            return (atm, otm_call, otm_put, price)

        # Determine strike increment and ATM
        increment = _get_strike_increment(price)
        atm = _round_to_strike(price, increment)

        # Standard spread width: 1 strike for most, but use increment-based logic
        # For calls: OTM is higher than ATM
        # For puts: OTM is lower than ATM
        otm_call = atm + increment
        otm_put = atm - increment

        if logger:
            logger(f"[{symbol}] price=${price:.2f} -> ATM={atm}, OTM_call={otm_call}, OTM_put={otm_put} (heuristic)")

        return (atm, otm_call, otm_put, price)

    except Exception as e:
        if logger:
            logger(f"[{symbol}] Error getting strikes: {e}")
        return (None, None, None, None)


def populate_missing_strikes(day_dir: str,
                             ib_host: str = IB_HOST,
                             ib_port: int = IB_PORT,
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
    needing_idx = set()
    # Fix FG: open rows whose signal-relevant OTM strike is PRESENT but may be invalid for the
    # chosen expiry (e.g. WBD 25.5P written from the pooled cross-expiry strike list). Validated
    # against IB after connect; invalid ones are added to rows_needing_strikes for re-snapping.
    otm_check_candidates = []
    for i, row in enumerate(rows):
        atm_val = row.get(atm_col, "")
        price_val = row.get(price_col, "")

        # Check if strike or price is missing
        atm_missing = not atm_val or str(atm_val).strip() == "" or _parse_float(atm_val) is None
        price_missing = not price_val or str(price_val).strip() == "" or _parse_float(price_val) is None

        # Fix CP-A: also trigger when both OTM strikes are empty — ATM may be present but
        # invalid (rounded non-IB strike). _get_atm_and_otm_strikes() re-snaps to real IB grid.
        otm_call_val = row.get(otm_call_col, "")
        otm_put_val  = row.get(otm_put_col,  "")
        otm_call_missing = not otm_call_val or str(otm_call_val).strip() == "" or _parse_float(otm_call_val) is None
        otm_put_missing  = not otm_put_val  or str(otm_put_val).strip()  == "" or _parse_float(otm_put_val)  is None
        otm_missing = otm_call_missing and otm_put_missing

        symbol = str(row.get(sym_col, "")).strip().upper()
        exp = str(row.get(exp_col, "")).strip()
        stype = str(row.get(stype_col, "")).strip().upper() if stype_col else ""

        if atm_missing or price_missing or otm_missing:
            if symbol and exp:
                rows_needing_strikes.append((i, symbol, exp, stype))
                needing_idx.add(i)
            continue

        # Fix FG: present-but-possibly-invalid OTM on an OPEN row → queue for IB validation.
        if symbol and exp and stype in ("CALL_OPEN", "PUT_OPEN"):
            if stype == "CALL_OPEN":
                otm_v, right = _parse_float(otm_call_val), "C"
            else:
                otm_v, right = _parse_float(otm_put_val), "P"
            if otm_v is not None:
                otm_check_candidates.append((i, symbol, exp, stype, otm_v, right))

    if not rows_needing_strikes and not otm_check_candidates:
        if logger:
            logger("populate_missing_strikes: no rows with missing strikes")
        return 0

    if logger:
        logger(f"populate_missing_strikes: {len(rows_needing_strikes)} rows need strikes; "
               f"{len(otm_check_candidates)} open row(s) to validate (Fix FG)")

    # Connect to IB and fetch strikes
    if IB is None:
        if logger:
            logger("populate_missing_strikes: ib_insync not available")
        return 0

    ib = IB()
    updates = 0
    try:
        ib.connect(ib_host, ib_port, clientId=client_id, timeout=10)

        # Fix FG: validate present OTM strikes on open rows against their expiry. A strike that
        # doesn't qualify (e.g. WBD 25.5P for the Sep monthly, sourced from the pooled
        # cross-expiry strike list) is queued for re-snapping by _get_atm_and_otm_strikes().
        for idx, symbol, exp, stype, otm_v, right in otm_check_candidates:
            if idx in needing_idx:
                continue
            try:
                _o = Option(symbol, exp, float(otm_v), right, "SMART", "100", "USD")
                _ok = bool(ib.qualifyContracts(_o))
            except Exception:
                _ok = False
            if not _ok:
                rows_needing_strikes.append((idx, symbol, exp, stype))
                needing_idx.add(idx)
                if logger:
                    logger(f"[{symbol}] Fix FG: OTM {right} {otm_v} not valid for {exp} — re-snapping")

        if not rows_needing_strikes:
            if logger:
                logger("populate_missing_strikes: no rows needed strikes after Fix FG validation")
            return 0

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

def _ib_fetcher_factory(ib: "IB", poll_seconds: float = 3.0):  # Fix BL: was 1.5 (Fix AO: was 0.6) — OTM OI often needs >1.5s
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
            # Try several attribute names defensively.
            # Fix BL: ib_insync Ticker uses callOpenInterest/putOpenInterest (confirmed by listener.py).
            # The generic names (optionOpenInterest etc.) don't exist on the Ticker object and always
            # returned None, leaving oi_atm/oi_oth as NaN despite enrichment running.
            oi = None
            _primary = "callOpenInterest" if str(right).upper() == "C" else "putOpenInterest"
            for attr in (_primary, "optionOpenInterest", "openInterest", "optOpenInterest"):
                val = getattr(t, attr, None)
                # Fix EM-2: reject 0/negative — IB uses 0 as "no data" sentinel on stall/closed market
                if isinstance(val, (int, float)) and not (val != val) and val > 0:
                    oi = int(val)
                    break
            def _clean_iv(_g):
                # Fix EM-2: reject 0/negative IV — IV of 0 is unphysical.
                # Fix FI: also reject implausible IV (band 0.03..2.0) so an illiquid
                # garbage tick can't feed the theo recompute.
                if _g and hasattr(_g, "impliedVol"):
                    _v = _g.impliedVol
                    if isinstance(_v, (int, float)) and not (_v != _v) and 0.03 <= _v <= 2.0:
                        return float(_v)
                return None
            iv = _clean_iv(getattr(t, "modelGreeks", None))
            # Fix FC: compute live bid-ask % from the same ticker (top-of-book always delivered)
            ba = _ba_pct(getattr(t, "bid", None), getattr(t, "ask", None))
            # Clean up primary subscription
            try: ib.cancelMktData(opt)
            except Exception: pass
            # Fix FJ: OI has no fallback like ba/iv. Tick 101 (Option Open Interest) is a
            # one-shot daily-snapshot tick on a separate data path from the streaming greeks
            # (106) and quotes; under socket contention it can be dropped while iv/ba arrive
            # (SCHW 2026-08-07 at the contended RTH open: iv=0.234 + ba=17.4 present, OI nan).
            # Re-snapshot OI once on type-4 — delayed-frozen carries the OI daily snapshot;
            # frozen type-2 does not — so this stays on the primary market-data type (no switch).
            if oi is None:
                try:
                    t2 = ib.reqMktData(opt, "100,101,106,588", False, False)
                    ib.sleep(poll_seconds)
                    for attr in (_primary, "optionOpenInterest", "openInterest", "optOpenInterest"):
                        val = getattr(t2, attr, None)
                        if isinstance(val, (int, float)) and not (val != val) and val > 0:
                            oi = int(val)
                            break
                    try: ib.cancelMktData(opt)
                    except Exception: pass
                except Exception:
                    pass
            # Fix FE/FI: after hours the primary type (live/delayed-frozen) returns bid/ask=-1
            # and often no modelGreeks, so ba and iv are None even though OI populates. Frozen
            # (type 2) serves the last regular-session quote/greeks (the true pre-close values).
            # Re-fetch bid/ask + IV when either is missing, then restore delayed-frozen.
            if ba is None or iv is None:
                try:
                    ib.reqMarketDataType(2)
                    tf = ib.reqMktData(opt, "", False, False)
                    ib.sleep(poll_seconds)
                    if ba is None:
                        ba = _ba_pct(getattr(tf, "bid", None), getattr(tf, "ask", None))
                    if iv is None:  # Fix FI: frozen greeks = pre-close IV
                        iv = _clean_iv(getattr(tf, "modelGreeks", None))
                    try: ib.cancelMktData(opt)
                    except Exception: pass
                except Exception:
                    pass
                finally:
                    try: ib.reqMarketDataType(4)
                    except Exception: pass
            return (oi, iv, ba)
        except Exception:
            return (None, None, None)
    return _fetch

def _held_option_sides(ib):
    """Fix FI: {SYMBOL: set('C'/'P')} for currently held (non-zero) OPT positions.
    Used to resolve which side a generic CLOSE row is closing (the position is still
    held until the 17:00 close, so ib.positions() at the 16:45 enrichment is correct)."""
    out: dict = {}
    if ib is None:
        return out
    try:
        for p in ib.positions():
            c = getattr(p, "contract", None)
            if not c or getattr(c, "secType", "") != "OPT":
                continue
            if not getattr(p, "position", 0):
                continue
            s = (getattr(c, "symbol", "") or "").upper()
            r = (getattr(c, "right", "") or "").upper()[:1]
            if s and r in ("C", "P"):
                out.setdefault(s, set()).add(r)
    except Exception:
        pass
    return out


def enrich_combined_csv(day_dir: str, fetcher=None, logger=None, ib=None):
    """
    Populate/refresh the canonical OI/IV columns in combined_listener_spreads.csv and
    recompute the theo debit prices with the fetched per-leg IV (Fix FI).

    Fix FI consolidates the OI/IV columns onto the single canonical set the readers use:
      OI  -> open_interest_{atm,otm}_{call,put}   (what _oi_ok / _find_csv_oi read)
      IV  -> iv_atm (long leg), iv_otm (short leg)
    The old parallel dead columns (oi_atm, oi_oth, iv_oth) are no longer written.

    For each row, if a canonical value is missing/blank/NaN/0, calls
    `fetcher(symbol, right, exp, strike)` -> (oi, iv, ba) for the leg and fills it in;
    then, when >=1 real IV was captured, overwrites the row's *_debit_theo_* columns
    (the side matching `right`) using Black-Scholes with those IVs. Writes back to the
    same CSV (keeps a .bak once).
    """
    fetch = fetcher or _default_fetcher
    csv_path, cols, rows = _read_combined_csv(day_dir)
    if not rows:
        return False

    # Fix FI: retire the dead parallel columns. Removing them from `cols` drops them on the
    # next write (DictWriter extrasaction="ignore"), so old CSVs get cleaned automatically.
    _stripped = False
    for _dead in ("oi_atm", "oi_oth", "iv_oth"):
        if _dead in cols:
            cols.remove(_dead)
            _stripped = True

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
    cur = lc.get("current_price")
    dte = lc.get("days_to_exp")

    if not all([sym, rgt, exp, atm]) or (not oth and not oth_call and not oth_put):
        if logger: logger("enrich_csv: missing key columns in combined_listener_spreads.csv")
        return False

    # Fix FI: ensure only the canonical OI/IV targets exist (no more oi_atm/oi_oth/iv_oth).
    # Fix FC: the 4 ba% columns so pre-Fix-FC prev-day CSVs get them on enrichment.
    need_cols = ["iv_atm", "iv_otm",
                 "open_interest_atm_call", "open_interest_otm_call",
                 "open_interest_atm_put", "open_interest_otm_put",
                 "ba_pct_atm_call", "ba_pct_otm_call", "ba_pct_atm_put", "ba_pct_otm_put"]
    _ensure_cols(cols, need_cols)

    updates = 0
    # Fix FI: resolve the held side for generic CLOSE rows (lazily, one positions() call).
    held_right = None  # None => not fetched yet

    def _need(v):
        if v is None: return True
        if isinstance(v, str) and v.strip() == "": return True
        try:
            fv = float(v)
            # Fix EM-3: NaN or 0 = "no data" -> refetch. Prevents stuck-0 corruption
            # from freezing enrichment (0 is IB's "no data" sentinel for OI/IV).
            return fv != fv or fv == 0
        except Exception:
            return False

    def _fnum(v):
        try:
            fv = float(v)
            return None if fv != fv else fv
        except Exception:
            return None

    for row in rows:
        try:
            symbol = str(row[sym]).strip()
            # Convert signal_type to right if needed
            rraw = str(row[rgt]).strip().upper()
            if rraw in ("CALL_OPEN", "CALL_CLOSE"):
                sides = ["C"]
            elif rraw in ("PUT_OPEN", "PUT_CLOSE"):
                sides = ["P"]
            elif rraw == "CLOSE":
                # Fix FI: generic CLOSE — resolve the held side(s) from live positions.
                if held_right is None:
                    held_right = _held_option_sides(ib)
                _hs = held_right.get(symbol.upper())
                if not _hs:
                    # No position (already flat) or offline -> keep prior behavior (skip).
                    continue
                sides = sorted(_hs)  # one side normally; both only on a rare roll
            else:
                continue
            expiry = str(row[exp]).strip()
            k1 = float(row[atm])
        except Exception:
            continue

        for right in sides:
            try:
                if oth:
                    k2 = float(row[oth])
                elif right == "C" and oth_call:
                    k2 = float(row[oth_call]) if row.get(oth_call) else None
                elif right == "P" and oth_put:
                    k2 = float(row[oth_put]) if row.get(oth_put) else None
                else:
                    k2 = None
            except Exception:
                k2 = None

            _oi_atm_col = "open_interest_atm_call" if right == "C" else "open_interest_atm_put"
            _oi_oth_col = "open_interest_otm_call" if right == "C" else "open_interest_otm_put"

            # ATM leg fill -> canonical open_interest_atm_{call|put} + iv_atm
            if _need(row.get(_oi_atm_col)) or _need(row.get("iv_atm")):
                oi1, iv1, ba1 = fetch(symbol, right, expiry, k1)
                # Fix FC: overwrite the ATM ba% with the fresh RTH value. Not _need-gated.
                if ba1 is not None:
                    _ba_col_atm = "ba_pct_atm_call" if right == "C" else "ba_pct_atm_put"
                    if _ba_col_atm in cols:
                        row[_ba_col_atm] = ba1
                        updates += 1
                # Fix EM-2: skip write when fetch returns 0
                if oi1 is not None and oi1 > 0 and _oi_atm_col in cols:
                    row[_oi_atm_col] = int(oi1)
                    updates += 1
                if iv1 is not None and iv1 > 0:  # Fix EM-2
                    row["iv_atm"] = float(iv1)
                    updates += 1

            # OTH leg fill (only if we have a valid OTM strike) -> canonical + iv_otm
            if k2 is not None and (_need(row.get(_oi_oth_col)) or _need(row.get("iv_otm"))):
                oi2, iv2, ba2 = fetch(symbol, right, expiry, k2)
                # Fix FC: overwrite the OTM ba% with the fresh RTH value. Not _need-gated.
                if ba2 is not None:
                    _ba_col_oth = "ba_pct_otm_call" if right == "C" else "ba_pct_otm_put"
                    if _ba_col_oth in cols:
                        row[_ba_col_oth] = ba2
                        updates += 1
                # Fix EM-2: skip write when fetch returns 0
                if oi2 is not None and oi2 > 0 and _oi_oth_col in cols:
                    row[_oi_oth_col] = int(oi2)
                    updates += 1
                if iv2 is not None and iv2 > 0:  # Fix EM-2 -> iv_otm (short leg), Fix FI
                    row["iv_otm"] = float(iv2)
                    updates += 1

            # Fix AO Part 3: For PUT rows, use call OI as proxy for put OI when IB doesn't
            # return put OI via reqMktData. Call and put OI on the same stock are correlated.
            if right == "P":
                if _need(row.get("open_interest_atm_put")):
                    _call_oi_atm = row.get("open_interest_atm_call")
                    if not _need(_call_oi_atm):
                        row["open_interest_atm_put"] = _call_oi_atm
                        updates += 1
                if _need(row.get("open_interest_otm_put")):
                    _call_oi_oth = row.get("open_interest_otm_call")
                    if not _need(_call_oi_oth):
                        row["open_interest_otm_put"] = _call_oi_oth
                        updates += 1

            # Fix FI: recompute the side's theo debits with the real per-leg IV.
            _iv_a = _fnum(row.get("iv_atm"))
            _iv_o = _fnum(row.get("iv_otm"))
            if (_iv_a and _iv_a > 0) or (_iv_o and _iv_o > 0):
                _S = _fnum(row.get(cur)) if cur else None
                _atm_v = _fnum(row.get(atm))
                _T = None
                _d = _fnum(row.get(dte)) if dte else None
                if _d is not None and _d > 0:
                    _T = _d / 365.0
                if _S and _S > 0 and _atm_v and _atm_v > 0 and _T:
                    sigma_atm = _iv_a if (_iv_a and _iv_a > 0) else _iv_o
                    sigma_otm = _iv_o if (_iv_o and _iv_o > 0) else _iv_a
                    try:
                        theo = _theo_spread_debits(_S, _atm_v, _T, sigma_atm, sigma_otm=sigma_otm)
                        _pref = "call" if right == "C" else "put"
                        for _w in ("1", "2_5", "5"):
                            _col = f"{_pref}_debit_theo_{_w}"
                            if _col in cols:
                                row[_col] = theo.get(f"{_pref}_debit_theo_{_w}")
                                updates += 1
                    except Exception as _te:
                        if logger: logger(f"enrich_csv: theo recompute error {symbol} {right}: {_te}")

    if updates or _stripped:
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
                  ib_host: str = IB_HOST,
                  ib_port: int = IB_PORT,
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
        changed = enrich_combined_csv(day_dir, fetcher=fetcher, logger=logger, ib=ib)  # Fix FI: ib for CLOSE held-side

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
    ap.add_argument("--ib-host", default=IB_HOST)
    ap.add_argument("--ib-port", type=int, default=IB_PORT)
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
                changed = enrich_combined_csv(args.day_dir, fetcher=fetcher, logger=_log, ib=ib)  # Fix FI: ib for CLOSE held-side
            finally:
                try: ib.disconnect()
                except Exception: pass
        else:
            changed = enrich_combined_csv(args.day_dir, fetcher=None, logger=_log)
        print(f"Enrichment {'made changes' if changed else 'no changes needed'} in: {args.day_dir}")
