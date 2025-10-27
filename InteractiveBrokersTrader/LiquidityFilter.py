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
    from ib_insync import IB, Option, util as _ibutil
except Exception:
    IB = None
    Option = Option if 'Option' in globals() else object
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
    ap = argparse.ArgumentParser(description="Enrich combined_listener_spreads.csv with OI/IV columns.")
    ap.add_argument("--day-dir", required=True, help="Folder like C:\\OptionsHistory\\YY_MM_DD")
    ap.add_argument("--only-rth", action="store_true", help="Only enrich when current time is RTH (09:30–16:00 NY).")
    ap.add_argument("--ib-host", default="127.0.0.1")
    ap.add_argument("--ib-port", type=int, default=7497)
    ap.add_argument("--client-id", type=int, default=915)
    args = ap.parse_args()

    def _log(msg: str): 
        print(msg, flush=True)

    if args.only_rth:
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
