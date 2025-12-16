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
        right_col = cols.get("right")
        exp_col = cols.get("exp") or cols.get("expiry") or cols.get("expiration")
        atm_col = cols.get("atm") or cols.get("k_atm") or cols.get("strike_long") or cols.get("strike1")
        oth_col = cols.get("oth") or cols.get("k_oth") or cols.get("strike_short") or cols.get("strike2")
        oi_atm_col = cols.get("oi_atm") or cols.get("open_interest_atm") or cols.get("oi1")
        oi_oth_col = cols.get("oi_oth") or cols.get("open_interest_oth") or cols.get("oi2")

        if not all([sym_col, right_col, exp_col, atm_col, oth_col]):
            return (None, None)

        # iterate and keep the last matching row (latest write wins)
        oi_atm = None
        oi_oth = None
        for row in reader:
            try:
                key = _combo_key(
                    str(row[sym_col]).strip(),
                    str(row[right_col]).strip(),
                    str(row[exp_col]).strip(),
                    float(row[atm_col]),
                    float(row[oth_col]),
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


def _get_atm_and_otm_strikes(ib: "IB", symbol: str, expiration: str, signal_type: str, logger=None) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Given a symbol and expiration, fetch the current price and compute ATM + OTM strikes.

    Returns (atm_strike, otm_strike_call, otm_strike_put) or (None, None, None) on failure.

    For CALL spreads: ATM < OTM (buy lower, sell higher)
    For PUT spreads: ATM > OTM (buy higher, sell lower)
    """
    if ib is None or Stock is None:
        return (None, None, None)

    try:
        # Get current stock price
        stock = Stock(symbol, "SMART", "USD")
        qualified = ib.qualifyContracts(stock)
        if not qualified:
            if logger:
                logger(f"[{symbol}] Could not qualify stock contract")
            return (None, None, None)

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
            return (None, None, None)

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

        return (atm, otm_call, otm_put)

    except Exception as e:
        if logger:
            logger(f"[{symbol}] Error getting strikes: {e}")
        return (None, None, None)


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

    if not sym_col or not exp_col:
        if logger:
            logger("populate_missing_strikes: missing symbol or expiration columns")
        return 0

    # Ensure strike columns exist
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

    # Find rows with missing strikes
    rows_needing_strikes = []
    for i, row in enumerate(rows):
        atm_val = row.get(atm_col, "")
        otm_call_val = row.get(otm_call_col, "")
        otm_put_val = row.get(otm_put_col, "")

        # Check if any strike is missing
        atm_missing = not atm_val or atm_val.strip() == "" or _parse_float(atm_val) is None

        if atm_missing:
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
        symbol_strikes = {}  # symbol -> (atm, otm_call, otm_put)

        for idx, symbol, exp, stype in rows_needing_strikes:
            if symbol not in symbol_strikes:
                atm, otm_call, otm_put = _get_atm_and_otm_strikes(ib, symbol, exp, stype, logger=logger)
                symbol_strikes[symbol] = (atm, otm_call, otm_put)

            atm, otm_call, otm_put = symbol_strikes[symbol]

            if atm is not None:
                rows[idx][atm_col] = atm
                rows[idx][otm_call_col] = otm_call
                rows[idx][otm_put_col] = otm_put
                updates += 1
                if logger:
                    logger(f"[{symbol}] Populated strikes: ATM={atm}, OTM_call={otm_call}, OTM_put={otm_put}")

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

def _ib_fetcher_factory(ib: "IB", poll_seconds: float = 0.6):
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
    rgt = lc.get("right")
    exp = lc.get("exp") or lc.get("expiry") or lc.get("expiration")
    atm = lc.get("atm") or lc.get("k_atm") or lc.get("strike_long") or lc.get("strike1")
    oth = lc.get("oth") or lc.get("k_oth") or lc.get("strike_short") or lc.get("strike2")

    if not all([sym, rgt, exp, atm, oth]):
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
            right  = str(row[rgt]).strip().upper()
            expiry = str(row[exp]).strip()
            k1 = float(row[atm])
            k2 = float(row[oth])
        except Exception:
            continue

        # ATM leg fill
        if _need(row.get("oi_atm")) or _need(row.get("iv_atm")):
            oi1, iv1 = fetch(symbol, right, expiry, k1)
            if oi1 is not None:
                row["oi_atm"] = int(oi1)
                updates += 1
            if iv1 is not None:
                row["iv_atm"] = float(iv1)
                updates += 1

        # OTH leg fill
        if _need(row.get("oi_oth")) or _need(row.get("iv_oth")):
            oi2, iv2 = fetch(symbol, right, expiry, k2)
            if oi2 is not None:
                row["oi_oth"] = int(oi2)
                updates += 1
            if iv2 is not None:
                row["iv_oth"] = float(iv2)
                updates += 1

    if updates:
        _write_combined_csv(csv_path, cols, rows)
    if logger: logger(f"enrich_csv: updated={updates}")
    return updates > 0

def enrich_if_rth(day_dir: str,
                  ib_host: str = "127.0.0.1",
                  ib_port: int = 7497,
                  client_id: int = 915,
                  logger=None) -> bool:
    """
    If current time is Regular Trading Hours (NY), connect to IB and enrich the combined CSV with OI/IV.
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
