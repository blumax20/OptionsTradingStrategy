from ib_insync import IB, Option, LimitOrder, MarketOrder
from ib_insync.contract import ComboLeg, Contract
import logging
from ib_insync import util as _ibutil
import pandas as pd
from zoneinfo import ZoneInfo
import json
import csv
import os
# --- Riskless-combo handling ---
# Epsilon used to "nudge" the net limit when IB rejects a combo as riskless.
# Can be overridden via env var RISKLESS_EPSILON (e.g., 0.01 or 0.02)
RISKLESS_EPSILON = float(os.getenv("RISKLESS_EPSILON", "0.01"))

def _was_riskless_reject(trade) -> bool:
    """
    Inspect a Trade object's logs/status for IB error 201 'Riskless combination orders are not allowed.'
    Returns True if such a rejection is detected.
    """
    try:
        # Check orderStatus and log messages for the riskless-combo text
        msg = ""
        try:
            st = getattr(trade, "orderStatus", None)
            if st and getattr(st, "status", "") in ("Cancelled", "Inactive"):
                msg = getattr(st, "whyHeld", "") or ""
        except Exception:
            pass
        # Trade.log contains TradeLogEntry with 'message'
        for le in getattr(trade, "log", []) or []:
            txt = (getattr(le, "message", "") or "")
            if "Riskless combination orders are not allowed" in txt:
                return True
        # also check msg (rarely present)
        return "Riskless combination orders are not allowed" in msg
    except Exception:
        return False

def _nudge_limit_for_riskless(limit_price: float | None, action: str, eps: float = None) -> float | None:
    """
    Return a nudged limit to avoid IB's 'riskless combination' classification.
    Convention: for SELL(CLOSE) we add +eps; for BUY(OPEN) we subtract eps (but never sub-0.01).
    """
    if limit_price is None:
        return None
    if eps is None:
        eps = RISKLESS_EPSILON
    try:
        lp = float(limit_price)
    except Exception:
        return None
    if action.upper() == "SELL":
        return round(max(lp + float(eps), 0.01), 2)
    else:
        # BUY
        return round(max(lp - float(eps), 0.01), 2)
# --- Normalize symbols (strip exchange prefixes/timeframes/trailing punctuation) ---
def _clean_symbol(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return raw
    s = raw.strip().upper()
    if ',' in s:
        s = s.split(',', 1)[0].strip()
    if ':' in s:
        s = s.split(':', 1)[1].strip()
    if ' ' in s:
        s = s.split()[0].strip()
    while s and s[-1] in '.:;,/':
        s = s[:-1]
    return s

# --- Parse listener NY timestamp ("YYYY-MM-DD HH:MM:SS") ---
def _parse_ts_ny(val):
    try:
        if isinstance(val, str) and val.strip():
            return datetime.strptime(val.strip(), "%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return None
import math
import argparse
import os
from pathlib import Path
from datetime import datetime, timedelta
 # --- Enforce weekly closures from CSV for the last N days (default 7) ---
def enforce_weekly_closures(ib: IB, df: pd.DataFrame, args, days: int = 7):
    if df is None or df.empty:
        return
    if "timestamp_ny" not in df.columns:
        return
    try:
        now_ny = datetime.now(ZoneInfo("America/New_York")) if ZoneInfo else datetime.now()
    except Exception:
        now_ny = datetime.now()
    cutoff = now_ny - timedelta(days=days)
    cutoff_naive = cutoff.replace(tzinfo=None) if getattr(cutoff, "tzinfo", None) is not None else cutoff

    # Filter CLOSE signals in the last N days
    mask_close = (df.get("signal_type", pd.Series(dtype=str)).astype(str).str.upper().isin(["CLOSE","CALL_CLOSE","PUT_CLOSE"]))
    df_close = df.loc[mask_close].copy()
    if df_close.empty:
        return
    df_close["_ts"] = df_close["timestamp_ny"].apply(_parse_ts_ny)
    df_close = df_close[df_close["_ts"].notna() & (df_close["_ts"] >= cutoff_naive)]
    if df_close.empty:
        return

    # Keep the most recent close row per symbol
    df_close = df_close.sort_values(["symbol","_ts"]).groupby(df_close["symbol"].str.upper(), as_index=False, group_keys=False).tail(1)

    for _, row in df_close.iterrows():
        try:
            symbol = _clean_symbol(str(row.get("symbol")))
            if not symbol:
                continue
            expiration = str(row.get("expiration"))
            atm = row.get("atm_strike")
            k_call = row.get("otm_strike_call")
            k_put  = row.get("otm_strike_put")
            # Choose reasonable close limits
            w_call = _spread_width_from_strikes(atm, k_call)
            w_put  = _spread_width_from_strikes(atm, k_put)
            call_close_limit = width_aligned_close_limit(row, 'C', w_call)
            put_close_limit  = width_aligned_close_limit(row, 'P', w_put)
            # Enforce min limit
            def _enforce_min(x):
                if x is None or (isinstance(x, float) and (math.isnan(x) or x in (float('inf'), float('-inf')))):
                    return None
                try:
                    v = float(x)
                except Exception:
                    return None
                if v < args.min_limit:
                    return args.min_limit if args.bump_to_min else None
                return v
            call_close_limit = _enforce_min(call_close_limit)
            put_close_limit  = _enforce_min(put_close_limit)

            # Try exact call close first
            closed_any = False
            if not pd.isna(k_call) and call_close_limit is not None and not pd.isna(atm):
                if close_spread_if_present(ib, symbol, expiration, 'C', float(atm), float(k_call), call_close_limit, max_qty=args.quantity):
                    logger.info(f"[{symbol}] Weekly-enforce CLOSE CALL {atm}/{k_call} exp {expiration} @ {call_close_limit}")
                    closed_any = True
            # Try exact put close
            if not pd.isna(k_put) and put_close_limit is not None and not pd.isna(atm):
                if close_spread_if_present(ib, symbol, expiration, 'P', float(atm), float(k_put), put_close_limit, max_qty=args.quantity):
                    logger.info(f"[{symbol}] Weekly-enforce CLOSE PUT {atm}/{k_put} exp {expiration} @ {put_close_limit}")
                    closed_any = True
            # Approximate if needed
            if not closed_any:
                if call_close_limit is not None:
                    a_atm, a_oth, qty = find_approx_spread_to_close(ib, symbol, expiration, 'C', float(atm) if not pd.isna(atm) else None, float(k_call) if not pd.isna(k_call) else None, tol=args.close_tol, max_qty=args.quantity)
                    if qty > 0:
                        tr = place_debit_spread(ib, symbol, expiration, a_atm, a_oth, 'C', call_close_limit, quantity=qty, action='SELL')
                        if tr is not None:
                            CLOSE_SEEN_KEYS.add(_close_key(symbol, 'C', expiration))
                            logger.info(f"[{symbol}] Weekly-enforce CLOSE CALL(approx) {a_atm}/{a_oth} exp {expiration} @ {call_close_limit}")
                            closed_any = True
                if not closed_any and put_close_limit is not None:
                    a_atm, a_oth, qty = find_approx_spread_to_close(ib, symbol, expiration, 'P', float(atm) if not pd.isna(atm) else None, float(k_put) if not pd.isna(k_put) else None, tol=args.close_tol, max_qty=args.quantity)
                    if qty > 0:
                        tr = place_debit_spread(ib, symbol, expiration, a_atm, a_oth, 'P', put_close_limit, quantity=qty, action='SELL')
                        if tr is not None:
                            CLOSE_SEEN_KEYS.add(_close_key(symbol, 'P', expiration))
                            logger.info(f"[{symbol}] Weekly-enforce CLOSE PUT(approx) {a_atm}/{a_oth} exp {expiration} @ {put_close_limit}")
            # Final market fallback if nothing closed (mandated closure within window)
            if not closed_any and not pd.isna(atm):
                if not pd.isna(k_call):
                    if close_spread_market_if_present(ib, symbol, expiration, 'C', float(atm), float(k_call), max_qty=args.quantity):
                        logger.info(f"[{symbol}] Weekly-enforce CLOSE CALL (MKT fallback) {atm}/{k_call} exp {expiration}")
                        closed_any = True
                if not closed_any and not pd.isna(k_put):
                    if close_spread_market_if_present(ib, symbol, expiration, 'P', float(atm), float(k_put), max_qty=args.quantity):
                        logger.info(f"[{symbol}] Weekly-enforce CLOSE PUT (MKT fallback) {atm}/{k_put} exp {expiration}")
                        closed_any = True
        except Exception as e:
            logger.warning(f"[weekly-close] {symbol if 'symbol' in locals() else ''}: {e}")

def _default_output_base() -> Path:
    env = os.getenv("OUTPUT_BASE")
    if env and env.strip():
        return Path(env).expanduser()
    if os.name == "nt":
        return Path(r"C:\OptionsHistory")
    return Path("/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Stock History and Backtesting Data")

OUTPUT_BASE = _default_output_base()

def _dated_dir(date: datetime | None = None) -> Path:
    d = (date or datetime.now()).strftime("%y_%m_%d")
    p = OUTPUT_BASE / d
    p.mkdir(parents=True, exist_ok=True)
    return p

def combined_csv_for(date: datetime | None = None) -> Path:
    return _dated_dir(date) / "combined_listener_spreads.csv"

# --- Attempts CSV paths/writers (day-rolled append) ---
def _attempts_dir(date_override: str | None = None) -> Path:
    return OUTPUT_BASE / today_folder_yy_mm_dd(date_override)

def _attempts_dayrolled_path(date_override: str | None = None) -> Path:
    d = _attempts_dir(date_override)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"attempts_{today_folder_yy_mm_dd(date_override)}.csv"

def _attempts_append(rows: list[dict], date_override: str | None = None) -> Path | None:
    """
    Append rows to the day-rolled attempts CSV. Creates file and header if missing.
    Returns the path written or None if nothing to write.
    """
    if not rows:
        return None
    path = _attempts_dayrolled_path(date_override)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Build a union header from the rows to preserve fields
    header_keys: set[str] = set()
    for r in rows:
        if isinstance(r, dict):
            header_keys.update(r.keys())
    header = sorted(list(header_keys)) if header_keys else sorted(list(rows[0].keys()))

    file_exists = path.exists() and path.stat().st_size > 0
    try:
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            for r in rows:
                writer.writerow(r)
    except Exception as e:
        logger.warning(f"Failed to append attempts to {path}: {e}")
        return None
    return path

def find_latest_combined_csv() -> Path | None:
    if not OUTPUT_BASE.exists():
        return None
    candidates = []
    for child in OUTPUT_BASE.iterdir():
        if child.is_dir() and child.name.count("_") == 2:
            f = child / "combined_listener_spreads.csv"
            if f.exists():
                candidates.append((f.stat().st_mtime, f))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]

# ---------- Logging ----------
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("PlaceAnOrder")

# ---- Structured health/decision logging (machine-readable) ----
ATTEMPTS: list[dict] = []

def _now_ny_iso() -> str:
    try:
        return datetime.now(ZoneInfo("America/New_York")).isoformat()
    except Exception:
        return datetime.now().isoformat()

def log_decision(evt: str, symbol: str | None, reason: str, **fields):
    """
    Emit a single-line JSON event (prefixed with HEALTH_EVT) to ib_cycle.log (stdout).
    Example: HEALTH_EVT {"evt":"skip_open","symbol":"PEP","reason":"working_order",...}
    """
    payload = {"ts": _now_ny_iso(), "evt": evt, "symbol": (symbol or ""), "reason": reason}
    if fields:
        payload.update(fields)
    try:
        logger.info("HEALTH_EVT " + json.dumps(payload, default=str))
    except Exception:
        logger.info(f"HEALTH_EVT {{'evt':'{evt}','symbol':'{symbol}','reason':'{reason}'}}")

def record_attempt(symbol: str, action: str, status: str, reason: str, **fields):
    """
    Accumulate a row for this run and also emit a HEALTH_EVT line.
    status: 'placed' | 'skipped' | 'error'
    action: 'open_call' | 'open_put' | 'close' | 'force_close' | etc.
    """
    row = {"ts": _now_ny_iso(), "symbol": symbol or "", "action": action, "status": status, "reason": reason}
    if fields:
        row.update(fields)
    ATTEMPTS.append(row)
    log_decision("attempt", symbol, reason, action=action, status=status, **fields)
    # Best-effort immediate append so mid-run actions survive early exits
    try:
        _attempts_append([row])
    except Exception:
        pass

# --- OI gating and RTH detection helpers ---
def _is_rth(now: datetime | None = None) -> bool:
    """Return True if now is Regular Trading Hours (Mon-Fri, 09:30–16:00 America/New_York)."""
    try:
        tz = ZoneInfo("America/New_York")
    except Exception:
        tz = None
    n = now or (datetime.now(tz) if tz else datetime.now())
    # Monday=0 ... Friday=5
    if n.weekday() > 4:
        return False
    hh, mm = n.hour, n.minute
    # 09:30 <= time < 16:00 (inclusive lower, exclusive upper)
    return (hh > 9 or (hh == 9 and mm >= 30)) and (hh < 16)

def _oi_ok(row: pd.Series, right: str, threshold: int) -> bool:
    """
    True if both legs' open interest meet threshold for the given right.
    Missing/NaN is treated as 0.
    """
    try:
        if right.upper() == 'C':
            oi1 = float(row.get("open_interest_atm_call") or 0.0)
            oi2 = float(row.get("open_interest_otm_call") or 0.0)
        else:
            oi1 = float(row.get("open_interest_atm_put") or 0.0)
            oi2 = float(row.get("open_interest_otm_put") or 0.0)
        return (oi1 >= float(threshold)) and (oi2 >= float(threshold))
    except Exception:
        return False

def vprint(enabled: bool, msg: str):
    if enabled:
        logger.info(msg)


def parse_args():
    p = argparse.ArgumentParser(description="Place debit spread orders from combined CSV")
    p.add_argument("--mode", choices=["from-signal","call","put","all","force-close"],
                   default="from-signal",
                   help="from-signal=use CSV signal_type; call/put/all=open new spreads; force-close=scan positions and submit close orders for selected symbols.")
    p.add_argument("--date", default=None,
                   help="Override date folder (YY_MM_DD). If omitted, uses America/New_York today.")
    p.add_argument("--quantity", type=int, default=1, help="Order quantity per spread.")
    p.add_argument("--symbols", default=None,
                   help="Comma-separated tickers to include (others skipped). Example: 'PEP,FOXA,BMY'")
    p.add_argument("--min-limit", type=float, default=0.05,
                   help="Minimum limit price; orders below this are skipped (or bumped if --bump-to-min).")
    p.add_argument("--bump-to-min", action="store_true",
                   help="If set, limits below --min-limit are bumped up to the minimum instead of skipping.")
    p.add_argument("--close-tol", type=float, default=25.0,
                   help="Strike tolerance for approximate close matching (e.g., 0.5 for $0.50).")
    p.add_argument("--close-tol-scale", type=float, default=None,
               help="Fraction of underlying spot used to derive tolerance (e.g. 0.015 for 1.5%%). Overrides --close-tol when set.")
    p.add_argument("--close-tol-min", type=float, default=2.0,
               help="Floor for computed tolerance (strike units).")
    p.add_argument("--close-tol-max", type=float, default=50.0,
               help="Ceiling for computed tolerance (strike units).")
    p.add_argument("--force-close-side", choices=["call","put","both"], default="both",
                   help="Which side(s) to close in --mode force-close.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print intended actions but do not place orders.")
    p.add_argument("--verbose", action="store_true",
                   help="Verbose logging per row and decision.")
    p.add_argument("--quiet", action="store_true",
                   help="Reduce ib_insync console noise (sets ib_insync logging to WARNING and disables console logging).")
    p.add_argument("--use-live-open", choices=["off","mid","join"], default="off",
                   help="If not 'off', price OPEN orders from live quotes: 'mid' = mid(long)-mid(short), 'join' = ask(long)-bid(short).")
    p.add_argument("--use-live-close", choices=["off","mid","join"], default="off",
                   help="If not 'off', price CLOSE orders from live quotes: 'mid' = mid(long)-mid(short), 'join' = bid(long)-ask(short).")
    p.add_argument("--oi-threshold", type=int, default=100,
                   help="Minimum OI required on BOTH legs to allow OPEN orders (applies per --oi-check).")
    p.add_argument("--oi-check", choices=["off","rth","always"], default="rth",
                   help="When to enforce the OI gate for OPEN orders: 'off' (never), 'rth' (only during 09:30–16:00 NY), or 'always'.")
    return p.parse_args()

def today_folder_yy_mm_dd(override: str | None = None) -> str:
    if override:
        return override
    try:
        today_ny = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        today_ny = datetime.now()
    return today_ny.strftime("%y_%m_%d")

def combined_csv_path_for_today(date_override: str | None = None) -> Path:
    return OUTPUT_BASE / today_folder_yy_mm_dd(date_override) / "combined_listener_spreads.csv"

def best_theoretical_limit(row: pd.Series, right: str) -> float | None:
    """
    Pick the best available theoretical debit for the requested right ('C' or 'P'):
    prefer 2.5-wide, then 1-wide, then 5-wide. Returns a float or None if unavailable.
    """
    keys = []
    if right.upper() == 'C':
        keys = ["call_debit_theo_2_5", "call_debit_theo_1", "call_debit_theo_5"]
    else:
        keys = ["put_debit_theo_2_5", "put_debit_theo_1", "put_debit_theo_5"]
    for k in keys:
        if k in row and row[k] is not None and not (isinstance(row[k], float) and (math.isnan(row[k]) or row[k] in (float('inf'), float('-inf')))):
            try:
                val = float(row[k])
                # Enforce minimum limit via CLI at call site; return raw here
                if val > 0:
                    return round(val, 2)
            except Exception:
                continue
    return None

def best_close_limit(row: pd.Series, right: str) -> float | None:
    """
    Choose a reasonable limit to **sell** (close) the spread, preferring quote-based debit limits,
    falling back to theoretical. Uses the same priority as open: 2.5 -> 1 -> 5.
    """
    if right.upper() == 'C':
        for k in ("call_debit_limit_2_5","call_debit_limit_1","call_debit_limit_5",
                  "call_debit_theo_2_5","call_debit_theo_1","call_debit_theo_5"):
            if k in row and row[k] is not None and not (isinstance(row[k], float) and (math.isnan(row[k]) or row[k] in (float('inf'), float('-inf')))):
                try:
                    v = float(row[k])
                    if v > 0:
                        return round(v, 2)
                except Exception:
                    pass
    else:
        for k in ("put_debit_limit_2_5","put_debit_limit_1","put_debit_limit_5",
                  "put_debit_theo_2_5","put_debit_theo_1","put_debit_theo_5"):
            if k in row and row[k] is not None and not (isinstance(row[k], float) and (math.isnan(row[k]) or row[k] in (float('inf'), float('-inf')))):
                try:
                    v = float(row[k])
                    if v > 0:
                        return round(v, 2)
                except Exception:
                    pass
    return None

def _spread_width_from_strikes(atm: float | None, oth: float | None) -> float | None:
    try:
        if atm is None or oth is None or pd.isna(atm) or pd.isna(oth):
            return None
        return float(abs(float(oth) - float(atm)))
    except Exception:
        return None

def _width_bucket(width: float | None) -> str | None:
    """
    Map a numeric width to the nearest known bucket label used by CSV columns:
    returns one of {"1","2_5","5"} or None if width is invalid.
    """
    if width is None:
        return None
    buckets = [("1", 1.0), ("2_5", 2.5), ("5", 5.0)]
    lab, _ = min(buckets, key=lambda t: abs(width - t[1]))
    return lab

def _limit_key_prefix(right: str, kind: str) -> str:
    """
    right: 'C' or 'P'
    kind : 'limit' or 'theo'
    Returns CSV column prefix, e.g., 'call_debit_limit' or 'put_debit_theo'
    """
    base = "call" if right.upper() == "C" else "put"
    mid  = "debit_limit" if kind == "limit" else "debit_theo"
    return f"{base}_{mid}"

def _width_aligned_value(row: pd.Series, right: str, kind: str, width_bucket: str) -> float | None:
    """
    Try width-aligned column first (e.g., call_debit_limit_2_5), then fall back
    to the other widths by proximity (nearest 1/2.5/5).
    """
    prefix = _limit_key_prefix(right, kind)
    order = ["1","2_5","5"]
    order.sort(key=lambda x: abs((1.0 if x=="1" else 2.5 if x=="2_5" else 5.0) -
                                 (1.0 if width_bucket=="1" else 2.5 if width_bucket=="2_5" else 5.0)))
    for lab in order:
        col = f"{prefix}_{lab}"
        if col in row and row[col] is not None:
            try:
                v = float(row[col])
                if isinstance(v, float) and not (math.isnan(v) or v in (float('inf'), float('-inf'))) and v > 0:
                    return round(v, 2)
            except Exception:
                continue
    return None

def width_aligned_close_limit(row: pd.Series, right: str, width: float | None) -> float | None:
    """Close (SELL) price: prefer width-aligned *limit* column; fall back to width-aligned *theo*."""
    wb = _width_bucket(width)
    if wb is None:
        return None
    v = _width_aligned_value(row, right, "limit", wb)
    if v is not None:
        return v
    return _width_aligned_value(row, right, "theo", wb)

def width_aligned_theoretical(row: pd.Series, right: str, width: float | None) -> float | None:
    """OPEN (BUY) theoretical debit for a given width bucket."""
    wb = _width_bucket(width)
    if wb is None:
        return None
    return _width_aligned_value(row, right, "theo", wb)

def qualify_option(ib: IB, symbol: str, expiration: str, strike: float, right: str) -> Option | None:
    try:
        c = Option(symbol=symbol, lastTradeDateOrContractMonth=expiration,
                   strike=float(strike), right=right.upper(), exchange='SMART', currency='USD')
        return ib.qualifyContracts(c)[0]
    except Exception:
        try:
            log_decision("error", symbol, "qualify_failed", exp=expiration, right=right, strike=strike)
        except Exception:
            pass
        return None
def nearest_valid_expiration(ib: IB, symbol: str, right: str, strike: float, desired_exp: str) -> str | None:
    """Pick desired expiry if valid, else the closest available by date for this symbol/right/strike."""
    try:
        probe = Option(symbol=symbol, lastTradeDateOrContractMonth='',
                       strike=float(strike), right=right.upper(),
                       exchange='SMART', currency='USD')
        cds = ib.reqContractDetails(probe)
        if not cds:
            return None
        exps = sorted({cd.contract.lastTradeDateOrContractMonth for cd in cds if cd and cd.contract})
        if desired_exp in exps:
            return desired_exp
        from datetime import datetime as _dt
        def _to_dt(x):
            try: return _dt.strptime(x, "%Y%m%d")
            except: return None
        target = _to_dt(desired_exp)
        if not target:
            return exps[0]
        exps_dt = [(abs((_to_dt(e) - target).days), e) for e in exps if _to_dt(e)]
        exps_dt.sort()
        return exps_dt[0][1] if exps_dt else exps[0]
    except Exception:
        return None

def live_debit_limit(ib: IB, symbol: str, exp: str, right: str, longK: float, shortK: float, timeout: float = 3.0) -> float | None:
    """Compute ask(long) - bid(short) within a short poll window; returns a rounded limit or None."""
    try:
        longC = qualify_option(ib, symbol, exp, longK, right)
        shortC = qualify_option(ib, symbol, exp, shortK, right)
        if not longC or not shortC:
            return None
        tL = ib.reqMktData(longC, '', False, False)
        tS = ib.reqMktData(shortC, '', False, False)
        waited = 0.0; step = 0.2
        askL = bidS = None
        import math, time
        while waited < timeout and (askL is None or bidS is None):
            try: ib.sleep(step)
            except: pass
            time.sleep(step); waited += step
            a = getattr(tL, 'ask', None); b = getattr(tS, 'bid', None)
            if isinstance(a, float) and not math.isnan(a): askL = a
            if isinstance(b, float) and not math.isnan(b): bidS = b
        try: ib.cancelMktData(longC)
        except: pass
        try: ib.cancelMktData(shortC)
        except: pass
        if askL is not None and bidS is not None and askL > 0 and bidS >= 0:
            return round(float(askL) - float(bidS), 2)
        return None
    except Exception:
        return None
def _ticker_mid(t):
    b = getattr(t, 'bid', None); a = getattr(t, 'ask', None)
    try:
        import math
        if isinstance(a,(int,float)) and isinstance(b,(int,float)) and not math.isnan(a) and not math.isnan(b) and a > 0 and b >= 0:
            return (float(a)+float(b))/2.0
    except Exception:
        pass
    lst = getattr(t, 'last', None)
    try:
        import math
        if isinstance(lst,(int,float)) and not math.isnan(lst):
            return float(lst)
    except Exception:
        pass
    return None


def live_spread_price(ib: IB, symbol: str, exp: str, right: str,
                      longK: float, shortK: float,
                      action: str,        # 'BUY' for OPEN, 'SELL' for CLOSE
                      scheme: str = 'join',
                      timeout: float = 3.0) -> float | None:
    """
    Compute a live net positive price for the combo using either:
      - scheme='join':  OPEN(BUY):  ask(long) - bid(short)
                        CLOSE(SELL): bid(long) - ask(short)
      - scheme='mid' :  OPEN/CLOSE: mid(long) - mid(short)
    Returns a rounded positive float, or None if unavailable.
    """
    longC = qualify_option(ib, symbol, exp, longK, right)
    shortC = qualify_option(ib, symbol, exp, shortK, right)
    if not longC or not shortC:
        return None
    tL = ib.reqMktData(longC, '', False, False)
    tS = ib.reqMktData(shortC, '', False, False)
    waited = 0.0; step = 0.2
    import time, math
    try:
        while waited < timeout:
            time.sleep(step); waited += step
            L_bid = getattr(tL, 'bid', None); L_ask = getattr(tL, 'ask', None)
            S_bid = getattr(tS, 'bid', None); S_ask = getattr(tS, 'ask', None)
            if scheme == 'join':
                if action.upper() == 'BUY':
                    if isinstance(L_ask,(int,float)) and isinstance(S_bid,(int,float)) and L_ask is not None and S_bid is not None and not math.isnan(L_ask) and not math.isnan(S_bid) and L_ask > 0 and S_bid >= 0:
                        val = float(L_ask) - float(S_bid)
                        if val > 0:
                            return round(val, 2)
                else:
                    if isinstance(L_bid,(int,float)) and isinstance(S_ask,(int,float)) and L_bid is not None and S_ask is not None and not math.isnan(L_bid) and not math.isnan(S_ask) and S_ask > 0 and L_bid >= 0:
                        val = float(L_bid) - float(S_ask)
                        if val > 0:
                            return round(val, 2)
            else:
                mL = _ticker_mid(tL); mS = _ticker_mid(tS)
                if mL is not None and mS is not None:
                    val = float(mL) - float(mS)
                    if val > 0:
                        return round(val, 2)
        return None
    finally:
        try: ib.cancelMktData(longC)
        except Exception: pass
        try: ib.cancelMktData(shortC)
        except Exception: pass
# --- De-duplication helpers for opens (idempotency per run) ---
OPEN_SEEN_KEYS: set[str] = set()
# Prevent duplicate CLOSE submissions per (symbol, exp, right) in a single run
CLOSE_SEEN_KEYS: set[str] = set()
# Prevent more than one OPEN per (symbol, side) per run
OPEN_PLACED_THIS_RUN: set[str] = set()

def _open_side_key(symbol: str, right: str) -> str:
    return f"{(symbol or '').upper()}|{right.upper()}"
def _close_key(symbol: str, right: str, exp: str) -> str:
    return f"{(symbol or '').upper()}|{right.upper()}|{exp}"
def _combo_key(symbol: str, right: str, exp: str, longK: float, shortK: float) -> str:
    lk = round(float(longK), 2)
    sk = round(float(shortK), 2)
    return f"{symbol.upper()}|{right.upper()}|{exp}|{lk:.2f}|{sk:.2f}"

def has_working_open_order(ib: IB, symbol: str, exp: str, right: str, longK: float, shortK: float) -> bool:
    """
    True if there is an existing BUY BAG order for the same two legs in a working state.
    """
    longC = qualify_option(ib, symbol, exp, longK, right)
    shortC = qualify_option(ib, symbol, exp, shortK, right)
    if not longC or not shortC:
        return False
    target_ids = {longC.conId, shortC.conId}
    try:
        ib.reqOpenOrders(); ib.sleep(0.25)
    except Exception:
        pass
    for tr in ib.trades():
        try:
            if getattr(tr.contract, 'secType', '') != 'BAG':
                continue
            if getattr(tr.contract, 'symbol', '').upper() != symbol.upper():
                continue
            if getattr(tr.order, 'action', '').upper() != 'BUY':
                continue
            st = getattr(tr.orderStatus, 'status', '')
            # common working/pre-working states
            if st not in ('PreSubmitted','Submitted','PendingSubmit','ApiPending','Inactive'):
                continue
            legs = getattr(tr.contract, 'comboLegs', []) or []
            ids = {getattr(leg, 'conId', None) for leg in legs}
            if target_ids == ids:
                return True
        except Exception:
            continue
    return False

def has_open_position_for_spread(ib: IB, symbol: str, exp: str, right: str, longK: float, shortK: float) -> bool:
    """
    True if account already holds +N on longK and -N on shortK for given symbol/right/expiry.
    """
    longC = qualify_option(ib, symbol, exp, longK, right)
    shortC = qualify_option(ib, symbol, exp, shortK, right)
    if not longC or not shortC:
        return False
    qty_long = qty_short = 0.0
    for p in ib.positions():
        c = getattr(p, 'contract', None)
        if not c:
            continue
        if getattr(c, 'conId', None) == longC.conId:
            qty_long += float(p.position)
        if getattr(c, 'conId', None) == shortC.conId:
            qty_short += float(p.position)
    return (qty_long > 0) and (qty_short < 0)

def place_debit_spread(ib: IB, symbol: str, expiration: str, long_strike: float, short_strike: float, right: str,limit_price: float | None, quantity: int = 1, action: str = 'BUY', order_type: str = 'LMT'):
    """
    Place a vertical debit spread (combo BAG). If order_type == 'MKT' or limit_price is None, a MarketOrder is used.
    action: 'BUY' to OPEN, 'SELL' to CLOSE.
    """
    # Define legs
    long_leg = Option(
        symbol=symbol,
        lastTradeDateOrContractMonth=expiration,
        strike=float(long_strike),
        right=right.upper(),
        exchange='SMART',
        currency='USD'
    )
    short_leg = Option(
        symbol=symbol,
        lastTradeDateOrContractMonth=expiration,
        strike=float(short_strike),
        right=right.upper(),
        exchange='SMART',
        currency='USD'
    )

    # Qualify legs
    try:
        long_leg = ib.qualifyContracts(long_leg)[0]
        short_leg = ib.qualifyContracts(short_leg)[0]
        logger.info(f"[{symbol}] {right}-legs qualified: long {long_leg.conId} @{long_strike}, short {short_leg.conId} @{short_strike}, exp {expiration}")
    except Exception as e:
        logger.error(f"[{symbol}] Failed to qualify contracts for {right} spread: {e}")
        return None

    # Build combo contract
    combo = Contract()
    combo.symbol = symbol
    combo.secType = 'BAG'
    combo.currency = 'USD'
    combo.exchange = 'SMART'

    leg_long = ComboLeg()
    leg_long.conId = long_leg.conId
    leg_long.ratio = 1
    leg_long.action = 'BUY'
    leg_long.exchange = long_leg.exchange

    leg_short = ComboLeg()
    leg_short.conId = short_leg.conId
    leg_short.ratio = 1
    leg_short.action = 'SELL'
    leg_short.exchange = short_leg.exchange

    combo.comboLegs = [leg_long, leg_short]

    # Build and place order
    try:
        if order_type.upper() == 'MKT' or limit_price is None:
            order = MarketOrder(action.upper(), quantity)
            trade = ib.placeOrder(combo, order)
            logger.info(f"[{symbol}] Placed {right} {'MKT' if action.upper()=='BUY' else 'MKT CLOSE'} {long_strike}/{short_strike} exp {expiration} (qty={quantity})")
            try:
                rec_action = ("close_call" if (action.upper()=="SELL" and right.upper()=="C") else
                              "close_put"  if (action.upper()=="SELL" and right.upper()=="P") else
                              "open_call"  if (action.upper()=="BUY"  and right.upper()=="C") else
                              "open_put")
                record_attempt(symbol, rec_action, "placed", "success",
                               exp=str(expiration), right=right.upper(),
                               longK=float(long_strike), shortK=float(short_strike),
                               order_type="MKT", limit=None,
                               qty=int(quantity), order_action=action.upper())
                # Note: a second record is logged if riskless-combo retry is triggered below
            except Exception:
                pass
            # If IB classifies the combo as riskless and cancels it instantly, switch to a small-limit with epsilon nudge
            try:
                ib.sleep(0.3)
            except Exception:
                pass
            if _was_riskless_reject(trade):
                # For SELL(CLOSE) nudge up; for BUY(OPEN) nudge down from a tiny anchor
                anchor = 0.05
                nudged = _nudge_limit_for_riskless(anchor, action)
                try:
                    # Create a new LMT order with the nudged limit
                    order2 = LimitOrder(action.upper(), quantity, float(nudged))
                    trade2 = ib.placeOrder(combo, order2)
                    logger.info(f"[{symbol}] Riskless-combo MKT rejected; resubmitting as LMT @{nudged:.2f} with epsilon nudge ({RISKLESS_EPSILON:.2f})")
                    try:
                        record_attempt(symbol,
                                       ("close_call" if (action.upper()=="SELL" and right.upper()=="C") else
                                        "close_put"  if (action.upper()=="SELL" and right.upper()=="P") else
                                        "open_call"  if (action.upper()=="BUY"  and right.upper()=="C") else
                                        "open_put"),
                                       "placed", "riskless_retry",
                                       exp=str(expiration), right=right.upper(),
                                       longK=float(long_strike), shortK=float(short_strike),
                                       order_type="LMT", prev_order_type="MKT",
                                       prev_limit=None, limit=float(nudged),
                                       epsilon=float(RISKLESS_EPSILON),
                                       qty=int(quantity), order_action=action.upper())
                    except Exception:
                        pass
                    return trade2
                except Exception as _re:
                    logger.warning(f"[{symbol}] Riskless-combo retry (MKT→LMT) failed: {_re}")
            return trade
        else:
            order = LimitOrder(action.upper(), quantity, float(limit_price))
            trade = ib.placeOrder(combo, order)
            logger.info(f"[{symbol}] Placed {right} {'LMT' if action.upper()=='BUY' else 'LMT CLOSE'} {long_strike}/{short_strike} exp {expiration} @ {float(limit_price):.2f} (qty={quantity})")
            try:
                rec_action = ("close_call" if (action.upper()=="SELL" and right.upper()=="C") else
                              "close_put"  if (action.upper()=="SELL" and right.upper()=="P") else
                              "open_call"  if (action.upper()=="BUY"  and right.upper()=="C") else
                              "open_put")
                record_attempt(symbol, rec_action, "placed", "success",
                               exp=str(expiration), right=right.upper(),
                               longK=float(long_strike), shortK=float(short_strike),
                               order_type="LMT", limit=float(limit_price) if limit_price is not None else None,
                               qty=int(quantity), order_action=action.upper())
                # Note: a second record is logged if riskless-combo retry is triggered below
            except Exception:
                pass
            # Briefly wait and check for IB riskless-combo cancellation; if detected, nudge and resubmit once
            try:
                ib.sleep(0.3)
            except Exception:
                pass
            if _was_riskless_reject(trade):
                nudged = _nudge_limit_for_riskless(float(limit_price), action)
                try:
                    order2 = LimitOrder(action.upper(), quantity, float(nudged))
                    trade2 = ib.placeOrder(combo, order2)
                    logger.info(f"[{symbol}] Riskless-combo LMT rejected @{float(limit_price):.2f}; resubmitting LMT @{nudged:.2f} (epsilon {RISKLESS_EPSILON:.2f})")
                    try:
                        record_attempt(symbol,
                                       ("close_call" if (action.upper()=="SELL" and right.upper()=="C") else
                                        "close_put"  if (action.upper()=="SELL" and right.upper()=="P") else
                                        "open_call"  if (action.upper()=="BUY"  and right.upper()=="C") else
                                        "open_put"),
                                       "placed", "riskless_retry",
                                       exp=str(expiration), right=right.upper(),
                                       longK=float(long_strike), shortK=float(short_strike),
                                       order_type="LMT", prev_order_type="LMT",
                                       prev_limit=float(limit_price), limit=float(nudged),
                                       epsilon=float(RISKLESS_EPSILON),
                                       qty=int(quantity), order_action=action.upper())
                    except Exception:
                        pass
                    return trade2
                except Exception as _re:
                    logger.warning(f"[{symbol}] Riskless-combo retry (LMT→LMT) failed: {_re}")
            return trade
    except Exception as e:
        logger.error(f"[{symbol}] Failed to place {right} spread order: {e}")
        try:
            record_attempt(symbol, ("close" if action.upper()=="SELL" else f"open_{right.lower()}"),
                           "error", "place_failed",
                           exp=expiration, right=right, longK=long_strike, shortK=short_strike,
                           limit=limit_price, err=str(e))
        except Exception:
            pass
        return None

# --- Market close helper ---
def close_spread_market_if_present(ib: IB, symbol: str, expiration: str, right: str, atm_strike: float, oth_strike: float, max_qty: int = 1):
    """
    Same as close_spread_if_present, but places a MARKET SELL combo when a matching position is found.
    Returns True if an order was sent.
    """
    longC = qualify_option(ib, symbol, expiration, atm_strike, right)
    shortC = qualify_option(ib, symbol, expiration, oth_strike, right)
    if not longC or not shortC:
        return False
    qty_long = qty_short = 0.0
    for p in ib.positions():
        if getattr(p.contract, 'conId', None) == longC.conId:
            qty_long += float(p.position)
        if getattr(p.contract, 'conId', None) == shortC.conId:
            qty_short += float(p.position)
    n = min(abs(int(qty_long)), abs(int(qty_short)), max_qty)
    if n <= 0:
        logger.info(f"[{symbol}] No matching spread quantity to close (long={qty_long}, short={qty_short}) for {right} {atm_strike}/{oth_strike} exp {expiration}")
        return False
    ckey = _close_key(symbol, right, expiration)
    if ckey in CLOSE_SEEN_KEYS:
        logger.info(f"[{symbol}] CLOSE already submitted for {right} exp {expiration}; skipping")
        return False

    trade = place_debit_spread(ib, symbol, expiration, atm_strike, oth_strike, right,
                               None, quantity=n, action='SELL', order_type='MKT')
    if trade is not None:
        CLOSE_SEEN_KEYS.add(ckey)
    return trade is not None

# --- Cancel any pending/working OPEN (BUY) combo orders for a symbol ---
def cancel_open_orders_for_symbol(ib: IB, symbol: str) -> int:
    """
    Cancel all pending/working BUY combo (BAG) orders for the given ticker.
    Returns number of orders cancelled.
    """
    try:
        # Refresh local view of open orders/trades
        ib.reqOpenOrders()
        ib.sleep(0.25)
    except Exception:
        pass
    n_cancel = 0
    for tr in ib.trades():
        try:
            c = getattr(tr.contract, 'secType', '')
            sym = getattr(tr.contract, 'symbol', '')
            if c != 'BAG' or sym.upper() != symbol.upper():
                continue
            ord_obj = tr.order
            stat = tr.orderStatus
            act = getattr(ord_obj, 'action', '').upper()
            status = getattr(stat, 'status', '')
            # Consider common "working" states
            if act == 'BUY' and status in ('PreSubmitted','Submitted','PendingSubmit','ApiPending','ApiCancelled','Inactive'):
                try:
                    ib.cancelOrder(ord_obj)
                    n_cancel += 1
                    logger.info(f"[{symbol}] Cancelled pending OPEN order (id={ord_obj.orderId}, status={status})")
                    try:
                        record_attempt(symbol, "cancel_open", "placed", "cancelled",
                                       exp=getattr(tr.contract, "comboLegsDescrip", ""),
                                       right="?", longK=None, shortK=None,
                                       order_id=getattr(ord_obj, "orderId", None),
                                       prev_status=status)
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"[{symbol}] Failed to cancel order {getattr(ord_obj,'orderId',None)}: {e}")
        except Exception:
            continue
    return n_cancel

# --- Cancel any pending/working CLOSE (SELL) combo orders for a symbol ---

def cancel_close_orders_for_symbol(ib: IB, symbol: str) -> int:
    """
    Cancel all pending/working SELL combo (BAG) orders for the given ticker.
    Returns number of orders cancelled.
    """
    try:
        # Refresh local view of open orders/trades
        ib.reqOpenOrders()
        ib.sleep(0.25)
    except Exception:
        pass
    n_cancel = 0
    for tr in ib.trades():
        try:
            c = getattr(tr.contract, 'secType', '')
            sym = getattr(tr.contract, 'symbol', '')
            if c != 'BAG' or sym.upper() != symbol.upper():
                continue
            ord_obj = tr.order
            stat = tr.orderStatus
            act = getattr(ord_obj, 'action', '').upper()
            status = getattr(stat, 'status', '')
            # Common working/pre-working states
            if act == 'SELL' and status in ('PreSubmitted','Submitted','PendingSubmit','ApiPending','ApiCancelled','Inactive'):
                try:
                    ib.cancelOrder(ord_obj)
                    n_cancel += 1
                    logger.info(f"[{symbol}] Cancelled pending CLOSE order (id={getattr(ord_obj,'orderId',None)}, status={status})")
                    try:
                        record_attempt(symbol, "cancel_close", "placed", "cancelled",
                                       exp=getattr(tr.contract, "comboLegsDescrip", ""),
                                       right="?", longK=None, shortK=None,
                                       order_id=getattr(ord_obj, "orderId", None),
                                       prev_status=status)
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning(f"[{symbol}] Failed to cancel order {getattr(ord_obj,'orderId',None)}: {e}")
        except Exception:
            continue
    return n_cancel

def close_spread_if_present(ib: IB, symbol: str, expiration: str, right: str, atm_strike: float, oth_strike: float, limit_price: float, max_qty: int = 1):
    """
    Attempt to close an existing long debit spread by SELLing the combo if we find +1 long @ ATM and -1 short @ OTM (for calls),
    or +1 long put @ ATM and -1 short put @ lower strike (for puts). Returns True if an order was sent.
    """
    if limit_price is None:
        return False
    longC = qualify_option(ib, symbol, expiration, atm_strike, right)
    shortC = qualify_option(ib, symbol, expiration, oth_strike, right)
    if not longC or not shortC:
        return False

    # Inspect current positions to ensure we actually hold the legs
    pos = ib.positions()
    logger.debug(f"[{symbol}] Inspecting positions for CLOSE {right}: long@{atm_strike} short@{oth_strike} exp {expiration}")
    qty_long = qty_short = 0.0
    for p in pos:
        if getattr(p.contract, 'conId', None) == longC.conId:
            qty_long += float(p.position)
        if getattr(p.contract, 'conId', None) == shortC.conId:
            qty_short += float(p.position)
        logger.debug(f"[{symbol}]   pos leg: conId={getattr(p.contract,'conId',None)} strike={getattr(p.contract,'strike',None)} right={getattr(p.contract,'right',None)} exp={getattr(p.contract,'lastTradeDateOrContractMonth',None)} qty={p.position}")

    # For a long debit spread we expect +N on long leg, -N on short leg
    n = min(abs(int(qty_long)), abs(int(qty_short)), max_qty)
    if n <= 0:
        logger.info(f"[{symbol}] No matching spread quantity to close (long={qty_long}, short={qty_short}) for {right} {atm_strike}/{oth_strike} exp {expiration}")
        return False
    ckey = _close_key(symbol, right, expiration)
    if ckey in CLOSE_SEEN_KEYS:
        logger.info(f"[{symbol}] CLOSE already submitted for {right} exp {expiration}; skipping")
        return False
    # SELL the combo to close
    trade = place_debit_spread(ib, symbol, expiration, atm_strike, oth_strike, right, limit_price, quantity=n, action='SELL')
    if trade is not None:
        CLOSE_SEEN_KEYS.add(ckey)
    return trade is not None


# --- Approximate spread finder for closing ---
def find_approx_spread_to_close(ib: IB, symbol: str, expiration: str, right: str,
                                atm_hint: float | None, oth_hint: float | None,
                                tol: float, max_qty: int = 1):
    """
    Find a pair of legs (+long, -short) for the given symbol/right/expiration within a strike tolerance.
    Returns (atm_strike, oth_strike, qty) or (None, None, 0) if not found.
    """
    pos = ib.positions()
    # Collect legs by strike sign
    longs = {}
    shorts = {}
    for p in pos:
        c = getattr(p, 'contract', None)
        if not c or getattr(c, 'symbol', '').upper() != symbol.upper():
            continue
        if getattr(c, 'lastTradeDateOrContractMonth', '') != expiration:
            continue
        if getattr(c, 'right', '').upper() != right.upper():
            continue
        strike = float(getattr(c, 'strike', 0.0))
        qty = float(getattr(p, 'position', 0.0))
        if qty > 0:
            longs[strike] = longs.get(strike, 0.0) + qty
        elif qty < 0:
            shorts[strike] = shorts.get(strike, 0.0) + abs(qty)

    # Find any matching pair; prefer closest to hints if provided
    best = (None, None, 0, 1e9)  # (atm, oth, qty, distance)
    for s_long, ql in longs.items():
        for s_short, qs in shorts.items():
            qty = int(min(ql, qs, max_qty))
            if qty <= 0:
                continue
            # distance metric to hints
            d = 0.0
            if atm_hint is not None:
                d += abs(s_long - float(atm_hint))
            if oth_hint is not None:
                d += abs(s_short - float(oth_hint))
            # check tolerance if hints present
            within = True
            if atm_hint is not None and abs(s_long - float(atm_hint)) > tol:
                within = False
            if oth_hint is not None and abs(s_short - float(oth_hint)) > tol:
                within = False
            # If no hints provided, accept any pair
            if (atm_hint is None and oth_hint is None) or within:
                if d < best[3]:
                    best = (s_long, s_short, qty, d)
    return (best[0], best[1], best[2]) if best[2] > 0 else (None, None, 0)


# --- Fallback: scan positions for any spread for this symbol and close via MARKET order ---
def close_any_spread_for_symbol(ib: IB, symbol: str, side: str | None = None, max_qty: int = 1) -> int:
    """
    Fallback: scan positions for this symbol and close any vertical debit spread(s) we can detect
    using MARKET SELL combo orders. This ignores CSV expiration/ATM hints and uses the actual
    expirations present in the account. Returns the number of market close orders submitted.
    If side is 'call' or 'put', restrict to that right; otherwise do both.
    """
    side_set = {side.lower()} if side else {"call", "put"}
    # Build per-expiration/right maps of long(+qty by strike) and short(+qty by strike)
    from collections import defaultdict
    placed = 0
    pos = ib.positions()
    # Group by (exp,right)
    buckets: dict[tuple[str,str], dict[str, dict[float, float]]] = {}
    for p in pos:
        c = getattr(p, 'contract', None)
        if not c or getattr(c, 'secType', '') != 'OPT':
            continue
        if getattr(c, 'symbol', '').upper() != symbol.upper():
            continue
        exp = getattr(c, 'lastTradeDateOrContractMonth', '')
        right = getattr(c, 'right', '').upper()
        if right not in ('C','P'):
            continue
        if (right == 'C' and 'call' not in side_set) or (right == 'P' and 'put' not in side_set):
            continue
        strike = float(getattr(c, 'strike', 0.0))
        qty = float(getattr(p, 'position', 0.0))
        key = (exp, right)
        if key not in buckets:
            buckets[key] = {'longs': defaultdict(float), 'shorts': defaultdict(float)}
        if qty > 0:
            buckets[key]['longs'][strike] += qty
        elif qty < 0:
            buckets[key]['shorts'][strike] += abs(qty)
    # For each (exp,right) try to pair long and short strikes into a vertical and SELL MKT
    for (exp, right), d in buckets.items():
        longs = sorted(d['longs'].items())  # list of (strike, qty)
        shorts = sorted(d['shorts'].items())
        if not longs or not shorts:
            continue
        # For calls, prefer shorts with higher strike than long; for puts, prefer lower
        for ls, lq in longs:
            # find a compatible short
            candidates = [(ss, sq) for ss, sq in shorts if (ss > ls if right == 'C' else ss < ls) and sq > 0]
            if not candidates:
                continue
            # choose closest in strike distance
            ss, sq = min(candidates, key=lambda t: abs(t[0] - ls))
            n = int(min(lq, sq, max_qty))
            if n <= 0:
                continue
            # Place MARKET SELL combo using the actual expiration from the position legs
            ckey = _close_key(symbol, right, exp)
            if ckey in CLOSE_SEEN_KEYS:
                logger.info(f"[{symbol}] CLOSE already submitted for {right} exp {exp}; skipping")
                continue
            tr = place_debit_spread(ib, symbol, exp, float(ls), float(ss), right, None, quantity=n, action='SELL', order_type='MKT')
            if tr is not None:
                placed += 1
                # decrement used qty
                d['longs'][ls] -= n
                d['shorts'][ss] -= n
            if tr is not None:
                CLOSE_SEEN_KEYS.add(ckey)      
        # cleanup any zeroed entries (not strictly necessary)
    return placed

def _iter_spread_pairs_from_positions(ib: IB, symbol: str, side: str | None = None, max_qty: int = 1):
    """
    Yield (exp, right, longK, shortK, qty) for each detectable vertical debit spread
    in current positions for `symbol`. If `side` is 'call' or 'put', restrict to that right.
    """
    from collections import defaultdict
    symbol_u = (symbol or "").upper()
    side_set = {"call", "put"} if not side else {side.lower()}
    buckets = defaultdict(lambda: {"longs": defaultdict(float), "shorts": defaultdict(float)})  # key=(exp,right)

    for p in ib.positions():
        c = getattr(p, "contract", None)
        if not c or getattr(c, "secType", "") != "OPT":
            continue
        if getattr(c, "symbol", "").upper() != symbol_u:
            continue
        exp = getattr(c, "lastTradeDateOrContractMonth", "")
        right = getattr(c, "right", "").upper()
        if right not in ("C", "P"):
            continue
        if (right == "C" and "call" not in side_set) or (right == "P" and "put" not in side_set):
            continue
        strike = float(getattr(c, "strike", 0.0))
        qty = float(getattr(p, "position", 0.0))
        key = (exp, right)
        if qty > 0:
            buckets[key]["longs"][strike] += qty
        elif qty < 0:
            buckets[key]["shorts"][strike] += abs(qty)

    # Pair by closest compatible strike: calls short>long, puts short<long
    for (exp, right), d in buckets.items():
        longs = sorted(d["longs"].items())
        shorts = sorted(d["shorts"].items())
        if not longs or not shorts:
            continue
        for ls, lq in longs:
            cands = [(ss, sq) for ss, sq in shorts if ((ss > ls) if right == "C" else (ss < ls)) and sq > 0]
            if not cands:
                continue
            ss, sq = min(cands, key=lambda t: abs(t[0] - ls))
            qty = int(min(lq, sq, max_qty))
            if qty > 0:
                yield (exp, right, float(ls), float(ss), int(qty))

def force_close_symbol_via_positions(ib: IB, symbol: str, args) -> int:
    """
    Close any detectable vertical debit spread(s) for `symbol` directly from positions,
    even if `symbol` isn't in today's CSV. Returns number of orders submitted.

    Pricing preference:
      1) If --use-live-close in {'join','mid'} => compute a limit via live_spread_price and place LMT.
      2) Else => place MARKET order.
    Respects --min-limit (and --bump-to-min) when a live price exists.
    Always records an attempts row so attempts CSV is created.
    """
    submitted = 0
    side_opt = None if getattr(args, "force_close_side", "both") == "both" else getattr(args, "force_close_side")

    any_pair = False
    for exp, right, longK, shortK, qty in _iter_spread_pairs_from_positions(ib, symbol, side=side_opt, max_qty=args.quantity):
        any_pair = True
        limit = None
        scheme = getattr(args, "use_live_close", "off")
        if scheme in ("join", "mid"):
            lim = live_spread_price(ib, symbol, exp, right, longK, shortK,
                                    action="SELL", scheme=scheme, timeout=3.0)
            if lim is not None:
                try:
                    v = float(lim)
                    limit = args.min_limit if (v < args.min_limit and getattr(args, "bump_to_min", False)) else (v if v >= args.min_limit else None)
                except Exception:
                    limit = None

        order_type = "LMT" if (limit is not None) else "MKT"
        ckey = _close_key(symbol, right, exp)
        if ckey in CLOSE_SEEN_KEYS:
            logger.info(f"[{symbol}] CLOSE already submitted for {right} exp {exp}; skipping")
            continue
        tr = place_debit_spread(ib, symbol, exp, longK, shortK, right, limit,
                                quantity=qty, action="SELL", order_type=order_type)
        if tr is not None:
            CLOSE_SEEN_KEYS.add(ckey)
            submitted += 1
            record_attempt(symbol, "force_close", "placed", "positions_fallback",
                           exp=str(exp), right=right, longK=float(longK), shortK=float(shortK),
                           order_type=order_type, limit=(float(limit) if limit is not None else None),
                           qty=int(qty), scheme=scheme)
        else:
            record_attempt(symbol, "force_close", "error", "place_failed_positions",
                           exp=str(exp), right=right, longK=float(longK), shortK=float(shortK),
                           order_type=order_type, limit=(float(limit) if limit is not None else None),
                           qty=int(qty))

    if not any_pair:
        # Ensure attempts CSV exists and tell you why nothing fired
        record_attempt(symbol, "force_close", "skipped", "no_spread_in_positions")

    return submitted

def run_from_csv():
    args = parse_args()
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    # Quiet console noise if requested
    if getattr(args, "quiet", False) and not getattr(args, "verbose", False):
        try:
            # Quiet our root logger
            logging.getLogger().setLevel(logging.WARNING)
            # Silence ib_insync chatter (positions/updatePortfolio are INFO on wrapper)
            logging.getLogger("ib_insync").setLevel(logging.ERROR)
            logging.getLogger("ib_insync.wrapper").setLevel(logging.ERROR)
            logging.getLogger("ib_insync.client").setLevel(logging.ERROR)
            # Also mute the 'ibapi' package if present
            logging.getLogger("ibapi").setLevel(logging.ERROR)
            # turn off ib_insync's console log mirroring
            try:
                _ibutil.logToConsole(False)
            except Exception:
                pass
        except Exception:
            pass
    only = None
    if args.symbols:
        only = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    csv_path = combined_csv_path_for_today(args.date)
    logger.info(f"Loading combined CSV: {csv_path} | mode={args.mode} | qty={args.quantity} | symbols={sorted(list(only)) if only else 'ALL'}")
    try:
        df = pd.read_csv(csv_path)
    except FileNotFoundError:
        logger.error(f"Combined CSV not found: {csv_path}")
        return
    except Exception as e:
        logger.error(f"Failed to read CSV: {e}")
        return
    # Normalize symbols and coerce numerics early
    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str).map(_clean_symbol)
    num_cols = [
        "atm_strike","otm_strike_call","otm_strike_put",
        "call_debit_limit_1","call_debit_limit_2_5","call_debit_limit_5",
        "put_debit_limit_1","put_debit_limit_2_5","put_debit_limit_5",
        "call_debit_theo_1","call_debit_theo_2_5","call_debit_theo_5",
        "put_debit_theo_1","put_debit_theo_2_5","put_debit_theo_5",
        "open_interest_atm_call","open_interest_otm_call",
        "open_interest_atm_put","open_interest_otm_put"
    ]
    for c in [c for c in num_cols if c in df.columns]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # Keep only the latest row per symbol (based on listener's timestamp_ny) to avoid multiple signals for same ticker
    if "timestamp_ny" in df.columns:
        try:
            df["_ts"] = df["timestamp_ny"].apply(_parse_ts_ny)
            df = df.sort_values(["symbol","_ts"]).groupby(df["symbol"].str.upper(), as_index=False, group_keys=False).tail(1)
        except Exception:
            # If parsing fails, fall back to keeping the CSV as-is
            pass

    # Validate minimal columns
    required_cols = ["symbol", "expiration", "atm_strike", "otm_strike_call", "otm_strike_put"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        logger.error(f"CSV missing required columns: {missing}")
        return

    if only:
        df = df[df["symbol"].str.upper().isin(only)]

    # Quick stats for CLOSE signals (for debugging)
    if "signal_type" in df.columns:
        close_mask = df["signal_type"].astype(str).str.upper().isin(["CLOSE","CALL_CLOSE","PUT_CLOSE"])
        logger.info(f"Found {close_mask.sum()} CLOSE rows in CSV (of {len(df)} total).")

    # Connect once
    ib = IB()
    try:
        ib.connect('127.0.0.1', 7497, clientId=101)  # Paper trading by default
        # Market data type not required for order placement; keep delayed-frozen
        ib.reqMarketDataType(4)
        ib.reqPositions()
        ib.sleep(0.5)
        try:
            ps = ib.positions()
            logger.info(f"Positions loaded: {len(ps)} entries")
        except Exception:
            pass
        # Reset per-run open de-dup set
        try:
            OPEN_SEEN_KEYS.clear()
        except Exception:
            pass
        try:
            CLOSE_SEEN_KEYS.clear()
        except Exception:
            pass
        try:
            OPEN_PLACED_THIS_RUN.clear()
        except Exception:
            pass
        # Enforce recent CLOSE signals only when running from-signal mode
        if args.mode == "from-signal":
            try:
                enforce_weekly_closures(ib, df.copy(), args, days=7)
            except Exception as _e:
                logger.warning(f"Weekly closure enforcement skipped due to error: {_e}")
    except Exception as e:
        logger.error(f"Failed to connect to IB: {e}")
        return

    # --- Positions-driven force-close for explicitly requested symbols (CSV-independent) ---
    # This runs even if today's CSV has ZERO rows for the requested tickers (e.g., ABR missing).
    if args.mode == "force-close" and args.symbols:
        _syms_req = {s.strip().upper() for s in str(args.symbols).split(",") if s.strip()}
        for _sym in sorted(_syms_req):
            if args.dry_run:
                # still write a diagnostic attempt so attempts CSV is created
                record_attempt(_sym, "force_close", "skipped", "dry_run_positions_only")
            else:
                n_fc = force_close_symbol_via_positions(ib, _sym, args)
                if n_fc > 0:
                    logger.info(f"[{_sym}] Force-closed {n_fc} spread(s) directly from positions (CSV-independent).")
        # Early finalize & return for force-close runs (prevents any opens/extra scanning)
        try:
            _attempts_append(ATTEMPTS)
        except Exception:
            pass
        ib.disconnect()
        return
    # If this batch contains CLOSE signals, proactively cancel any pending BUY combo orders for those tickers
    if 'close_set' in locals() or 'close_mask' in locals():
        # Defensive: try to find the set of tickers flagged for CLOSE
        if 'close_set' in locals():
            close_syms = close_set
        elif 'close_mask' in locals() and 'df' in locals():
            close_syms = set(df[close_mask]["symbol"].astype(str).map(_clean_symbol))
        else:
            close_syms = set()
    else:
        close_syms = set()
    if 'close_mask' in locals() and 'df' in locals():
        close_set = set(df[close_mask]["symbol"].astype(str).map(_clean_symbol))
    # Use close_set if defined
    if 'close_set' in locals():
        _close_set = close_set
    else:
        _close_set = close_syms
    if _close_set:
        total_cxl = 0
        for sym in sorted([s for s in _close_set if s]):
            total_cxl += cancel_open_orders_for_symbol(ib, sym)
        if total_cxl > 0:
            logger.info(f"Cancelled {total_cxl} pending OPEN combo order(s) across CLOSE-priority tickers")

    placed = 0
    for idx, row in df.iterrows():
        try:
            symbol = _clean_symbol(str(row.get("symbol")))
            expiration = str(row.get("expiration"))  # YYYYMMDD from CSV
            atm = row.get("atm_strike")
            k_call = row.get("otm_strike_call")
            k_put = row.get("otm_strike_put")

            # If put OTM is missing but call OTM exists, infer symmetric width as a fallback
            if (pd.isna(k_put) or k_put is None) and not pd.isna(k_call) and not pd.isna(atm):
                try:
                    width = abs(float(k_call) - float(atm))
                    if width > 0:
                        k_put = float(atm) - width
                        vprint(args.verbose, f"[{symbol}] Inferred put OTM strike = {k_put} from call width {width}")
                except Exception:
                    pass

            # Skip opens if essentials are missing; for CLOSE flows we will fall back to positions scan
            if symbol in (None, "nan", "NaN"):
                msg = f"[row {idx}] Skipping; missing symbol. sym={symbol}"
                if args.verbose: logger.info(msg)
                else: logger.warning(msg)
                record_attempt(symbol or "", "open", "skipped", "missing_symbol", row_index=int(idx))
                continue
            missing_exp_or_atm = (not expiration) or pd.isna(atm)

            # Helper to enforce min limit
            def enforce_min_limit(x: float | None) -> float | None:
                if x is None or (isinstance(x, float) and (math.isnan(x) or x in (float('inf'), float('-inf')))):
                    return None
                try:
                    v = float(x)
                except Exception:
                    return None
                if v < args.min_limit:
                    return args.min_limit if args.bump_to_min else None
                return v

            # Decide which legs to place
            allow_call = allow_put = False
            stype = str(row.get("signal_type") or "").upper()

            # Defensive per-run guard: if we've already opened this side for this symbol, skip any other open paths
            if allow_call and _open_side_key(symbol, 'C') in OPEN_PLACED_THIS_RUN:
                allow_call = False
            if allow_put and _open_side_key(symbol, 'P') in OPEN_PLACED_THIS_RUN:
                allow_put = False

            # Fallback: infer signal_type from strategy_position if stype missing
            if not stype:
                sp = row.get("strategy_position")
                try:
                    sp_i = int(sp) if sp is not None and str(sp).strip() != "" else None
                except Exception:
                    sp_i = None
                if sp_i is not None:
                    if sp_i > 0:
                        stype = "CALL_OPEN"
                    elif sp_i < 0:
                        stype = "PUT_OPEN"
                    else:
                        stype = "CLOSE"

            if args.mode == "from-signal":
                if stype == "CALL_OPEN":
                    # Latest-signal-wins: cancel any working CLOSE orders before we open
                    cxl_close = cancel_close_orders_for_symbol(ib, symbol)
                    if cxl_close > 0:
                        logger.info(f"[{symbol}] Cancelled {cxl_close} pending CLOSE combo order(s) prior to CALL_OPEN")
                    allow_call = True
                elif stype == "PUT_OPEN":
                    # Latest-signal-wins: cancel any working CLOSE orders before we open
                    cxl_close = cancel_close_orders_for_symbol(ib, symbol)
                    if cxl_close > 0:
                        logger.info(f"[{symbol}] Cancelled {cxl_close} pending CLOSE combo order(s) prior to PUT_OPEN")
                    allow_put = True
                elif stype in ("CLOSE","CALL_CLOSE","PUT_CLOSE"):
                    # Cancel any pending OPENs for this ticker before closing
                    cxl = cancel_open_orders_for_symbol(ib, symbol)
                    if cxl > 0:
                        logger.info(f"[{symbol}] Cancelled {cxl} pending OPEN combo order(s) prior to CLOSE")
                    # Attempt to close whichever spread we hold (call and/or put) by inspecting positions
                    closed_any = False
                    # Close call spread if present
                    live_close_limit_c = None
                    if getattr(args, "use_live_close", "off") in ("mid","join") and not pd.isna(atm) and not pd.isna(k_call):
                        live_close_limit_c = live_spread_price(ib, symbol, expiration, 'C', float(atm), float(k_call),
                                                               action='SELL', scheme=args.use_live_close, timeout=3.0)
                    w_call = _spread_width_from_strikes(atm, k_call)
                    call_close_raw = width_aligned_close_limit(row, 'C', w_call)
                    call_close_limit = (live_close_limit_c if (live_close_limit_c is not None)
                                        else enforce_min_limit(call_close_raw))
                    if not pd.isna(k_call) and call_close_limit is not None and not pd.isna(atm):
                        vprint(args.verbose, f"[{symbol}] Attempt CLOSE CALL exact {atm}/{k_call} exp {expiration} @ {call_close_limit}")
                        if args.dry_run:
                            vprint(args.verbose, f"[DRY-RUN] CLOSE CALL {symbol} {atm}/{k_call} exp {expiration} @ {call_close_limit}")
                            closed_any = True  # simulate success in dry-run
                            placed += 1
                        else:
                            if close_spread_if_present(ib, symbol, expiration, 'C', float(atm), float(k_call), call_close_limit, max_qty=args.quantity):
                                logger.info(f"[{symbol}] Submitted CLOSE for CALL spread {atm}/{k_call} exp {expiration} @ {call_close_limit}")
                                closed_any = True
                                placed += 1
                    # fallback to market close if position present
                    if call_close_limit is None and not pd.isna(k_call) and not pd.isna(atm):
                        if not args.dry_run and close_spread_market_if_present(ib, symbol, expiration, 'C', float(atm), float(k_call), max_qty=args.quantity):
                            logger.info(f"[{symbol}] Submitted CLOSE CALL (MKT fallback) {atm}/{k_call} exp {expiration}")
                            placed += 1
                    # Close put spread if present
                    live_close_limit_p = None
                    if getattr(args, "use_live_close", "off") in ("mid","join") and not pd.isna(atm) and not pd.isna(k_put):
                        live_close_limit_p = live_spread_price(ib, symbol, expiration, 'P', float(atm), float(k_put),
                                                               action='SELL', scheme=args.use_live_close, timeout=3.0)
                    w_put = _spread_width_from_strikes(atm, k_put)
                    put_close_raw = width_aligned_close_limit(row, 'P', w_put)
                    put_close_limit = (live_close_limit_p if (live_close_limit_p is not None)
                                    else enforce_min_limit(put_close_raw))
                    if not pd.isna(k_put) and put_close_limit is not None and not pd.isna(atm):
                        vprint(args.verbose, f"[{symbol}] Attempt CLOSE PUT exact {atm}/{k_put} exp {expiration} @ {put_close_limit}")
                        if args.dry_run:
                            vprint(args.verbose, f"[DRY-RUN] CLOSE PUT {symbol} {atm}/{k_put} exp {expiration} @ {put_close_limit}")
                            closed_any = True  # simulate success in dry-run
                            placed += 1
                        else:
                            if close_spread_if_present(ib, symbol, expiration, 'P', float(atm), float(k_put), put_close_limit, max_qty=args.quantity):
                                logger.info(f"[{symbol}] Submitted CLOSE for PUT spread {atm}/{k_put} exp {expiration} @ {put_close_limit}")
                                closed_any = True
                                placed += 1
                    # fallback to market close if position present
                    if put_close_limit is None and not pd.isna(k_put) and not pd.isna(atm):
                        if not args.dry_run and close_spread_market_if_present(ib, symbol, expiration, 'P', float(atm), float(k_put), max_qty=args.quantity):
                            logger.info(f"[{symbol}] Submitted CLOSE PUT (MKT fallback) {atm}/{k_put} exp {expiration}")
                            placed += 1
                    if not closed_any:
                        # try approximate match within tolerance
                        if not pd.isna(k_call) and not pd.isna(atm):
                            vprint(args.verbose, f"[{symbol}] Attempt CLOSE CALL(approx) around atm={atm}, other_hint={k_call} tol={args.close_tol} @ {call_close_limit}")
                            approx_atm, approx_oth, qty = find_approx_spread_to_close(ib, symbol, expiration, 'C',
                                                                                      float(atm), float(k_call), tol=args.close_tol, max_qty=args.quantity)
                            if qty > 0 and call_close_limit is not None:
                                if args.dry_run:
                                    vprint(args.verbose, f"[DRY-RUN] CLOSE CALL(approx) {symbol} {approx_atm}/{approx_oth} exp {expiration} @ {call_close_limit} x{qty}")
                                    closed_any = True
                                    placed += 1
                                else:
                                    if qty > 0:
                                        tr = place_debit_spread(ib, symbol, expiration, approx_atm, approx_oth, 'C',
                                                          call_close_limit, quantity=qty, action='SELL')
                                        if tr is not None:
                                            CLOSE_SEEN_KEYS.add(_close_key(symbol, 'C', expiration))
                                            logger.info(f"[{symbol}] Submitted CLOSE CALL(approx) {approx_atm}/{approx_oth} exp {expiration} @ {call_close_limit}")
                                            closed_any = True
                                            placed += 1
                        if not closed_any and not pd.isna(k_put) and not pd.isna(atm):
                            vprint(args.verbose, f"[{symbol}] Attempt CLOSE PUT(approx) around atm={atm}, other_hint={k_put} tol={args.close_tol} @ {put_close_limit}")
                            approx_atm, approx_oth, qty = find_approx_spread_to_close(ib, symbol, expiration, 'P',
                                                                                      float(atm), float(k_put), tol=args.close_tol, max_qty=args.quantity)
                            if qty > 0 and put_close_limit is not None:
                                if args.dry_run:
                                    vprint(args.verbose, f"[DRY-RUN] CLOSE PUT(approx) {symbol} {approx_atm}/{approx_oth} exp {expiration} @ {put_close_limit} x{qty}")
                                    closed_any = True
                                    placed += 1
                                else:
                                    if qty > 0:
                                        tr = place_debit_spread(ib, symbol, expiration, approx_atm, approx_oth, 'P',
                                                          put_close_limit, quantity=qty, action='SELL')
                                        if tr is not None:
                                            CLOSE_SEEN_KEYS.add(_close_key(symbol, 'P', expiration))
                                            logger.info(f"[{symbol}] Submitted CLOSE PUT(approx) {approx_atm}/{approx_oth} exp {expiration} @ {put_close_limit}")
                                            closed_any = True
                                            placed += 1
                    if not closed_any:
                        # Final fallback: close any detected spread(s) for this symbol via MARKET orders,
                        # even if expiration/ATM are missing or mismatched.
                        restrict = None
                        if stype == "CALL_CLOSE":
                            restrict = "call"
                        elif stype == "PUT_CLOSE":
                            restrict = "put"
                        n_closed = 0 if args.dry_run else close_any_spread_for_symbol(ib, symbol, side=restrict, max_qty=args.quantity)
                        if n_closed > 0:
                            logger.info(f"[{symbol}] Fallback MARKET close placed for {n_closed} spread(s) via positions scan")
                            placed += n_closed
                        else:
                            vprint(args.verbose, f"[{symbol}] No usable signal_type in CSV (stype='{stype}'); skipping in from-signal mode.")
                    continue
                else:
                    vprint(args.verbose, f"[{symbol}] No usable signal_type in CSV (stype='{stype}'); skipping in from-signal mode.")
                    continue
            elif args.mode == "call":
                allow_call = True
            elif args.mode == "put":
                allow_put = True
            elif args.mode == "all":
                allow_call = True
                allow_put = True
            # --- force-close mode ---
            if args.mode == "force-close":
                # Note: explicit --symbols were already handled above (CSV-independent).
                sides = ["call","put"] if args.force_close_side == "both" else [args.force_close_side]
                for side in sides:
                    if side == "call":
                        limit = None
                        if getattr(args, "use_live_close", "off") in ("mid","join") and not pd.isna(atm):
                            hint = k_call if side == 'call' else k_put
                            if not pd.isna(hint):
                                limit = live_spread_price(ib, symbol, expiration, ('C' if side=='call' else 'P'),
                                                          float(atm), float(hint),
                                                          action='SELL', scheme=args.use_live_close, timeout=3.0)
                        if limit is None:
                            w_call = _spread_width_from_strikes(atm, k_call)
                            limit = enforce_min_limit(width_aligned_close_limit(row, 'C', w_call))
                        if limit is None:
                            vprint(args.verbose, f"[{symbol}] FORCE-CLOSE CALL skipped; limit below min or missing")
                        else:
                            # exact then approx
                            done = False
                            if not pd.isna(k_call):
                                vprint(args.verbose, f"[{symbol}] Attempt CLOSE CALL exact {atm}/{k_call} exp {expiration} @ {limit}")
                                if args.dry_run:
                                    vprint(args.verbose, f"[DRY-RUN] CLOSE CALL {symbol} {atm}/{k_call} exp {expiration} @ {limit} x{args.quantity}")
                                    done = True
                                    placed += 1
                                else:
                                    done = close_spread_if_present(ib, symbol, expiration, 'C', float(atm), float(k_call), limit, max_qty=args.quantity)
                                    if done:
                                        logger.info(f"[{symbol}] Submitted FORCE-CLOSE CALL {atm}/{k_call} exp {expiration} @ {limit}")
                                        placed += 1
                            if not done:
                                vprint(args.verbose, f"[{symbol}] Attempt CLOSE CALL(approx) around atm={atm}, other_hint={k_call} tol={args.close_tol} @ {limit}")
                                a_atm, a_oth, qty = find_approx_spread_to_close(ib, symbol, expiration, 'C',
                                                                                float(atm) if not pd.isna(atm) else None,
                                                                                float(k_call) if not pd.isna(k_call) else None,
                                                                                tol=args.close_tol, max_qty=args.quantity)
                                if qty > 0:
                                    if args.dry_run:
                                        vprint(args.verbose, f"[DRY-RUN] CLOSE CALL(approx) {symbol} {a_atm}/{a_oth} exp {expiration} @ {limit} x{qty}")
                                        placed += 1
                                    else:
                                        place_debit_spread(ib, symbol, expiration, a_atm, a_oth, 'C', limit, quantity=qty, action='SELL')
                                        logger.info(f"[{symbol}] Submitted FORCE-CLOSE CALL(approx) {a_atm}/{a_oth} exp {expiration} @ {limit}")
                                        placed += 1
                    else:
                        limit = None
                        if getattr(args, "use_live_close", "off") in ("mid","join") and not pd.isna(atm):
                            hint = k_call if side == 'call' else k_put
                            if not pd.isna(hint):
                                limit = live_spread_price(ib, symbol, expiration, ('C' if side=='call' else 'P'),
                                                          float(atm), float(hint),
                                                          action='SELL', scheme=args.use_live_close, timeout=3.0)
                        if limit is None:
                            w_put = _spread_width_from_strikes(atm, k_put)
                            limit = enforce_min_limit(width_aligned_close_limit(row, 'P', w_put))
                        if limit is None:
                            vprint(args.verbose, f"[{symbol}] FORCE-CLOSE PUT skipped; limit below min or missing")
                        else:
                            done = False
                            if not pd.isna(k_put):
                                vprint(args.verbose, f"[{symbol}] Attempt CLOSE PUT exact {atm}/{k_put} exp {expiration} @ {limit}")
                                if args.dry_run:
                                    vprint(args.verbose, f"[DRY-RUN] CLOSE PUT {symbol} {atm}/{k_put} exp {expiration} @ {limit} x{args.quantity}")
                                    done = True
                                    placed += 1
                                else:
                                    done = close_spread_if_present(ib, symbol, expiration, 'P', float(atm), float(k_put), limit, max_qty=args.quantity)
                                    if done:
                                        logger.info(f"[{symbol}] Submitted FORCE-CLOSE PUT {atm}/{k_put} exp {expiration} @ {limit}")
                                        placed += 1
                            if not done:
                                vprint(args.verbose, f"[{symbol}] Attempt CLOSE PUT(approx) around atm={atm}, other_hint={k_put} tol={args.close_tol} @ {limit}")
                                a_atm, a_oth, qty = find_approx_spread_to_close(ib, symbol, expiration, 'P',
                                                                                float(atm) if not pd.isna(atm) else None,
                                                                                float(k_put) if not pd.isna(k_put) else None,
                                                                                tol=args.close_tol, max_qty=args.quantity)
                                if qty > 0:
                                    if args.dry_run:
                                        vprint(args.verbose, f"[DRY-RUN] CLOSE PUT(approx) {symbol} {a_atm}/{a_oth} exp {expiration} @ {limit} x{qty}")
                                        placed += 1
                                    else:
                                        place_debit_spread(ib, symbol, expiration, a_atm, a_oth, 'P', limit, quantity=qty, action='SELL')
                                        logger.info(f"[{symbol}] Submitted FORCE-CLOSE PUT(approx) {a_atm}/{a_oth} exp {expiration} @ {limit}")
                                        placed += 1
                # Force-close final fallback: MARKET close anything remaining for this symbol/side(s)
                if not args.dry_run:
                    sides = ["call","put"] if args.force_close_side == "both" else [args.force_close_side]
                    total_fallback = 0
                    for sside in sides:
                        total_fallback += close_any_spread_for_symbol(ib, symbol, side=sside, max_qty=args.quantity)
                    if total_fallback > 0:
                        logger.info(f"[{symbol}] FORCE-CLOSE fallback MARKET close submitted for {total_fallback} spread(s)")
                        placed += total_fallback
                continue

            # --- CALL debit spread (ATM long / OTM short) ---
            if allow_call and not pd.isna(k_call):
                # One-open-per-(symbol,side) guard for this run
                _side_key_c = _open_side_key(symbol, 'C')
                if _side_key_c in OPEN_PLACED_THIS_RUN:
                    record_attempt(symbol, "open_call", "skipped", "dup_symbol_side_in_run", exp=expiration)
                    continue
                # Early OI gate for CALL opens (applies to both theo/live and fallback paths)
                _need_oi = (args.oi_check == "always") or (args.oi_check == "rth" and _is_rth())
                if _need_oi and not _oi_ok(row, 'C', args.oi_threshold):
                    try:
                        oi1 = float(row.get("open_interest_atm_call") or 0.0)
                        oi2 = float(row.get("open_interest_otm_call") or 0.0)
                    except Exception:
                        oi1 = oi2 = 0.0
                    record_attempt(symbol, "open_call", "skipped", "oi_below_threshold",
                                   exp=str(expiration), atm=float(atm) if not pd.isna(atm) else None,
                                   oth=float(k_call) if not pd.isna(k_call) else None,
                                   oi_atm=oi1, oi_otm=oi2, threshold=int(args.oi_threshold), scope=args.oi_check)
                    # Skip any CALL open attempts for this row
                    continue
                attempted = False
                live_open_limit = None
                if getattr(args, "use_live_open", "off") in ("mid","join"):
                    live_open_limit = live_spread_price(ib, symbol, expiration, 'C', float(atm), float(k_call),
                                                        action='BUY', scheme=args.use_live_open, timeout=3.0)
                    if args.verbose and live_open_limit is not None:
                        vprint(args.verbose, f"[{symbol}] CALL OPEN live({args.use_live_open}) {atm}/{k_call} exp {expiration} @ {live_open_limit}")
                
                w_call_open = _spread_width_from_strikes(atm, k_call)
                w_put_open  = _spread_width_from_strikes(atm, k_put)
                _raw_theo_call = width_aligned_theoretical(row, 'C', w_call_open)
                _raw_theo_put  = width_aligned_theoretical(row, 'P', w_put_open)
                theo_call = enforce_min_limit(_raw_theo_call)
                theo_put  = enforce_min_limit(_raw_theo_put)
                keyC = _combo_key(symbol, 'C', expiration, float(atm), float(k_call))
                dup_run = (keyC in OPEN_SEEN_KEYS)
                wip     = has_working_open_order(ib, symbol, expiration, 'C', float(atm), float(k_call))
                held    = has_open_position_for_spread(ib, symbol, expiration, 'C', float(atm), float(k_call))
                skip = dup_run or wip or held
                if dup_run:
                    record_attempt(symbol, "open_call", "skipped", "dup_in_run",
                                   exp=expiration, right='C', atm=float(atm), oth=float(k_call))
                if wip:
                    record_attempt(symbol, "open_call", "skipped", "working_order",
                                   exp=expiration, right='C', atm=float(atm), oth=float(k_call))
                if held:
                    record_attempt(symbol, "open_call", "skipped", "already_held",
                                   exp=expiration, right='C', atm=float(atm), oth=float(k_call))
                if _raw_theo_call is not None and _raw_theo_call < args.min_limit and not args.bump_to_min:
                    record_attempt(symbol, "open_call", "skipped", "min_limit_reject",
                                   raw_theo=_raw_theo_call, min_limit=args.min_limit, bumped=False,
                                   exp=expiration, atm=float(atm), oth=float(k_call))
                # 1) Theo-first
                chosen_open_limit = None
                if not skip:
                    if live_open_limit is not None:
                        chosen_open_limit = enforce_min_limit(live_open_limit)
                    elif theo_call is not None:
                        chosen_open_limit = theo_call
                if chosen_open_limit is not None:
                    vprint(args.verbose, f"[{symbol}] CALL OPEN theo {atm}/{k_call} exp {expiration} @ {chosen_open_limit}")
                    if args.dry_run:
                        vprint(args.verbose, f"[DRY-RUN] CALL OPEN theo {symbol} {atm}/{k_call} exp {expiration} @ {chosen_open_limit} x{args.quantity}")
                        attempted = True; OPEN_SEEN_KEYS.add(keyC); placed += 1
                        record_attempt(symbol, "open_call", "placed", "success",
                                       exp=expiration, atm=float(atm), oth=float(k_call), limit=chosen_open_limit)
                        OPEN_PLACED_THIS_RUN.add(_open_side_key(symbol, 'C'))
                    else:
                        tr = place_debit_spread(ib, symbol, expiration, float(atm), float(k_call), 'C', chosen_open_limit, quantity=args.quantity)
                        if tr is not None:
                            OPEN_SEEN_KEYS.add(keyC); placed += 1; attempted = True
                            record_attempt(symbol, "open_call", "placed", "success",
                                           exp=expiration, atm=float(atm), oth=float(k_call), limit=chosen_open_limit)
                            OPEN_PLACED_THIS_RUN.add(_open_side_key(symbol, 'C'))
                        else:
                            # Retry once with refreshed expiration; then derive live limit
                            new_exp = nearest_valid_expiration(ib, symbol, 'C', float(atm), expiration)
                            if new_exp and new_exp != expiration:
                                vprint(args.verbose, f"[{symbol}] CALL OPEN retry with refreshed expiration {new_exp}")
                                tr = place_debit_spread(ib, symbol, new_exp, float(atm), float(k_call), 'C', chosen_open_limit, quantity=args.quantity)
                                if tr is not None:
                                    OPEN_SEEN_KEYS.add(_combo_key(symbol,'C',new_exp,float(atm),float(k_call))); placed += 1; attempted = True
                                    record_attempt(symbol, "open_call", "placed", "success",
                                                   exp=new_exp, atm=float(atm), oth=float(k_call), limit=chosen_open_limit)
                                    OPEN_PLACED_THIS_RUN.add(_open_side_key(symbol, 'C'))
                                else:
                                    live = live_debit_limit(ib, symbol, new_exp, 'C', float(atm), float(k_call), timeout=3.0)
                                    if live is not None:
                                        live_limit = enforce_min_limit(live)
                                        tr = place_debit_spread(ib, symbol, new_exp, float(atm), float(k_call), 'C', live_limit, quantity=args.quantity)
                                        if tr is not None:
                                            OPEN_SEEN_KEYS.add(_combo_key(symbol,'C',new_exp,float(atm),float(k_call))); placed += 1; attempted = True
                                            record_attempt(symbol, "open_call", "placed", "success",
                                                           exp=new_exp, atm=float(atm), oth=float(k_call), limit=live_limit)
                                            OPEN_PLACED_THIS_RUN.add(_open_side_key(symbol, 'C'))
                # 2) Fallback to debit_limit only when both legs have OI ≥ 100
                if not attempted:
                    oi_ok = False
                    try:
                        oi1 = float(row.get("open_interest_atm_call") or 0.0)
                        oi2 = float(row.get("open_interest_otm_call") or 0.0)
                        oi_ok = (oi1 >= 100) and (oi2 >= 100)
                    except Exception:
                        oi_ok = False
                    if not oi_ok:
                        record_attempt(symbol, "open_call", "skipped", "oi_below_threshold",
                                       oi_atm=oi1, oi_otm=oi2, threshold=100,
                                       exp=expiration, atm=float(atm) if not pd.isna(atm) else None,
                                       oth=float(k_call) if not pd.isna(k_call) else None)
                    if oi_ok:
                        wb = _width_bucket(w_call_open)
                        ordered = [f"call_debit_limit_{wb}"] if wb else []
                        for alt in ("1","2_5","5"):
                            col = f"call_debit_limit_{alt}"
                            if col not in ordered:
                                ordered.append(col)
                        for kk in ordered:
                            lv = enforce_min_limit(row.get(kk))
                            if lv is None:
                                continue
                            if args.dry_run:
                                vprint(args.verbose, f"[DRY-RUN] CALL OPEN fallback limit {symbol} {atm}/{k_call} exp {expiration} @ {lv} x{args.quantity}")
                                OPEN_SEEN_KEYS.add(keyC); placed += 1
                                record_attempt(symbol, "open_call", "placed", "success",
                                               exp=expiration, atm=float(atm), oth=float(k_call), limit=lv)
                                OPEN_PLACED_THIS_RUN.add(_open_side_key(symbol, 'C'))
                                break
                            tr = place_debit_spread(ib, symbol, expiration, float(atm), float(k_call), 'C', lv, quantity=args.quantity)
                            if tr is None:
                                new_exp = nearest_valid_expiration(ib, symbol, 'C', float(atm), expiration)
                                if new_exp:
                                    live = live_debit_limit(ib, symbol, new_exp, 'C', float(atm), float(k_call), timeout=3.0)
                                    lv2 = enforce_min_limit(live)
                                    if lv2 is not None:
                                        tr = place_debit_spread(ib, symbol, new_exp, float(atm), float(k_call), 'C', lv2, quantity=args.quantity)
                            if tr:
                                OPEN_SEEN_KEYS.add(keyC); placed += 1
                                used_exp = new_exp if 'new_exp' in locals() and new_exp else expiration
                                used_limit = lv2 if 'lv2' in locals() and lv2 is not None else lv
                                record_attempt(symbol, "open_call", "placed", "success",
                                               exp=(used_exp),
                                               atm=float(atm), oth=float(k_call), limit=used_limit)
                                OPEN_PLACED_THIS_RUN.add(_open_side_key(symbol, 'C'))
                                break
                if allow_call and not attempted:
                    record_attempt(symbol, "open_call", "skipped", "no_viable_limit_or_conditions",
                                   exp=expiration, atm=float(atm), oth=float(k_call))
            # --- PUT debit spread (ATM long / lower strike short) ---
            if allow_put and not pd.isna(k_put):
                # One-open-per-(symbol,side) guard for this run
                _side_key_p = _open_side_key(symbol, 'P')
                if _side_key_p in OPEN_PLACED_THIS_RUN:
                    record_attempt(symbol, "open_put", "skipped", "dup_symbol_side_in_run", exp=expiration)
                    continue
                # Early OI gate for PUT opens (applies to both theo/live and fallback paths)
                _need_oi = (args.oi_check == "always") or (args.oi_check == "rth" and _is_rth())
                if _need_oi and not _oi_ok(row, 'P', args.oi_threshold):
                    try:
                        oi1 = float(row.get("open_interest_atm_put") or 0.0)
                        oi2 = float(row.get("open_interest_otm_put") or 0.0)
                    except Exception:
                        oi1 = oi2 = 0.0
                    record_attempt(symbol, "open_put", "skipped", "oi_below_threshold",
                                   exp=str(expiration), atm=float(atm) if not pd.isna(atm) else None,
                                   oth=float(k_put) if not pd.isna(k_put) else None,
                                   oi_atm=oi1, oi_otm=oi2, threshold=int(args.oi_threshold), scope=args.oi_check)
                    # Skip any PUT open attempts for this row
                    continue
                attempted = False
                live_open_limit = None
                if getattr(args, "use_live_open", "off") in ("mid","join"):
                    live_open_limit = live_spread_price(ib, symbol, expiration, 'P', float(atm), float(k_put),
                                                        action='BUY', scheme=args.use_live_open, timeout=3.0)
                    if args.verbose and live_open_limit is not None:
                        vprint(args.verbose, f"[{symbol}] PUT OPEN live({args.use_live_open}) {atm}/{k_put} exp {expiration} @ {live_open_limit}")
                w_call_open = _spread_width_from_strikes(atm, k_call)
                w_put_open  = _spread_width_from_strikes(atm, k_put)
                _raw_theo_call = width_aligned_theoretical(row, 'C', w_call_open)
                _raw_theo_put  = width_aligned_theoretical(row, 'P', w_put_open)
                theo_call = enforce_min_limit(_raw_theo_call)
                theo_put  = enforce_min_limit(_raw_theo_put)
                keyP = _combo_key(symbol, 'P', expiration, float(atm), float(k_put))
                dup_run = (keyP in OPEN_SEEN_KEYS)
                wip     = has_working_open_order(ib, symbol, expiration, 'P', float(atm), float(k_put))
                held    = has_open_position_for_spread(ib, symbol, expiration, 'P', float(atm), float(k_put))
                skip = dup_run or wip or held
                if dup_run:
                    record_attempt(symbol, "open_put", "skipped", "dup_in_run",
                                   exp=expiration, right='P', atm=float(atm), oth=float(k_put))
                if wip:
                    record_attempt(symbol, "open_put", "skipped", "working_order",
                                   exp=expiration, right='P', atm=float(atm), oth=float(k_put))
                if held:
                    record_attempt(symbol, "open_put", "skipped", "already_held",
                                   exp=expiration, right='P', atm=float(atm), oth=float(k_put))
                if _raw_theo_put is not None and _raw_theo_put < args.min_limit and not args.bump_to_min:
                    record_attempt(symbol, "open_put", "skipped", "min_limit_reject",
                                   raw_theo=_raw_theo_put, min_limit=args.min_limit, bumped=False,
                                   exp=expiration, atm=float(atm), oth=float(k_put))
                # 1) Theo-first
                chosen_open_limit = None
                if not skip:
                    if live_open_limit is not None:
                        chosen_open_limit = enforce_min_limit(live_open_limit)
                    elif theo_put is not None:
                        chosen_open_limit = theo_put
                if chosen_open_limit is not None:
                    vprint(args.verbose, f"[{symbol}] PUT OPEN theo {atm}/{k_put} exp {expiration} @ {chosen_open_limit}")
                    if args.dry_run:
                        vprint(args.verbose, f"[DRY-RUN] PUT OPEN theo {symbol} {atm}/{k_put} exp {expiration} @ {chosen_open_limit} x{args.quantity}")
                        attempted = True; OPEN_SEEN_KEYS.add(keyP); placed += 1
                        record_attempt(symbol, "open_put", "placed", "success",
                                       exp=expiration, atm=float(atm), oth=float(k_put), limit=chosen_open_limit)
                        OPEN_PLACED_THIS_RUN.add(_open_side_key(symbol, 'P'))
                    else:
                        tr = place_debit_spread(ib, symbol, expiration, float(atm), float(k_put), 'P', chosen_open_limit, quantity=args.quantity)
                        if tr is not None:
                            OPEN_SEEN_KEYS.add(keyP); placed += 1; attempted = True
                            record_attempt(symbol, "open_put", "placed", "success",
                                           exp=expiration, atm=float(atm), oth=float(k_put), limit=chosen_open_limit)
                            OPEN_PLACED_THIS_RUN.add(_open_side_key(symbol, 'P'))
                        else:
                            new_exp = nearest_valid_expiration(ib, symbol, 'P', float(atm), expiration)
                            if new_exp and new_exp != expiration:
                                vprint(args.verbose, f"[{symbol}] PUT OPEN retry with refreshed expiration {new_exp}")
                                tr = place_debit_spread(ib, symbol, new_exp, float(atm), float(k_put), 'P', chosen_open_limit, quantity=args.quantity)
                                if tr is not None:
                                    OPEN_SEEN_KEYS.add(_combo_key(symbol,'P',new_exp,float(atm),float(k_put))); placed += 1; attempted = True
                                    record_attempt(symbol, "open_put", "placed", "success",
                                                   exp=new_exp, atm=float(atm), oth=float(k_put), limit=chosen_open_limit)
                                    OPEN_PLACED_THIS_RUN.add(_open_side_key(symbol, 'P'))
                                else:
                                    live = live_debit_limit(ib, symbol, new_exp, 'P', float(atm), float(k_put), timeout=3.0)
                                    if live is not None:
                                        live_limit = enforce_min_limit(live)
                                        tr = place_debit_spread(ib, symbol, new_exp, float(atm), float(k_put), 'P', live_limit, quantity=args.quantity)
                                        if tr is not None:
                                            OPEN_SEEN_KEYS.add(_combo_key(symbol, 'P', new_exp, float(atm), float(k_put)))
                                            placed += 1
                                            attempted = True
                                            used_exp = new_exp if new_exp else expiration
                                            record_attempt(
                                                symbol, "open_put", "placed", "success",
                                                exp=used_exp, atm=float(atm), oth=float(k_put), limit=live_limit
                                            )
                                            OPEN_PLACED_THIS_RUN.add(_open_side_key(symbol, 'P'))
                # 2) Fallback to debit_limit only when both legs have OI ≥ 100
                if not attempted:
                    oi_ok = False
                    try:
                        oi1 = float(row.get("open_interest_atm_put") or 0.0)
                        oi2 = float(row.get("open_interest_otm_put") or 0.0)
                        oi_ok = (oi1 >= 100) and (oi2 >= 100)
                    except Exception:
                        oi_ok = False
                    if not oi_ok:
                        record_attempt(symbol, "open_put", "skipped", "oi_below_threshold",
                                       oi_atm=oi1, oi_otm=oi2, threshold=100,
                                       exp=expiration, atm=float(atm) if not pd.isna(atm) else None,
                                       oth=float(k_put) if not pd.isna(k_put) else None)
                    if oi_ok:
                        wb = _width_bucket(w_put_open)
                        ordered = [f"put_debit_limit_{wb}"] if wb else []
                        for alt in ("1","2_5","5"):
                            col = f"put_debit_limit_{alt}"
                            if col not in ordered:
                                ordered.append(col)
                        for kk in ordered:
                            lv = enforce_min_limit(row.get(kk))
                            if lv is None:
                                continue
                            if args.dry_run:
                                vprint(args.verbose, f"[DRY-RUN] PUT OPEN fallback limit {symbol} {atm}/{k_put} exp {expiration} @ {lv} x{args.quantity}")
                                OPEN_SEEN_KEYS.add(keyP); placed += 1
                                record_attempt(symbol, "open_put", "placed", "success",
                                               exp=expiration, atm=float(atm), oth=float(k_put), limit=lv)
                                OPEN_PLACED_THIS_RUN.add(_open_side_key(symbol, 'P'))
                                break
                            tr = place_debit_spread(ib, symbol, expiration, float(atm), float(k_put), 'P', lv, quantity=args.quantity)
                            if tr is None:
                                new_exp = nearest_valid_expiration(ib, symbol, 'P', float(atm), expiration)
                                if new_exp:
                                    live = live_debit_limit(ib, symbol, new_exp, 'P', float(atm), float(k_put), timeout=3.0)
                                    lv2 = enforce_min_limit(live)
                                    if lv2 is not None:
                                        tr = place_debit_spread(ib, symbol, new_exp, float(atm), float(k_put), 'P', lv2, quantity=args.quantity)
                            if tr:
                                used_exp = new_exp if 'new_exp' in locals() and new_exp else expiration
                                used_limit = lv2 if 'lv2' in locals() and lv2 is not None else lv
                                OPEN_SEEN_KEYS.add(keyP)
                                placed += 1
                                record_attempt(
                                    symbol, "open_put", "placed", "success",
                                    exp=used_exp, atm=float(atm), oth=float(k_put), limit=used_limit
                                )
                                OPEN_PLACED_THIS_RUN.add(_open_side_key(symbol, 'P'))
                                break
                if allow_put and not attempted:
                    record_attempt(symbol, "open_put", "skipped", "no_viable_limit_or_conditions",
                                   exp=expiration, atm=float(atm), oth=float(k_put))
        except Exception as e:
            logger.error(f"[row {idx}] Unexpected error: {e}")

    logger.info(f"Completed. Orders {'simulated' if args.dry_run else 'attempted'}: {placed}")
    # Persist run attempts (sidecar CSV) for health diagnostics
    try:
        out_dir = Path(r"C:\OptionsHistory\logs")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_csv = out_dir / (f"attempts_{today_folder_yy_mm_dd(args.date)}_{datetime.now().strftime('%H%M%S')}.csv")
        if ATTEMPTS:
            cols = ["ts","symbol","action","status","reason","exp","right","atm","oth","limit","raw_theo","min_limit","bumped","oi_atm","oi_otm","threshold","longK","shortK","err","row_index"]
            with open(out_csv, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for r in ATTEMPTS:
                    w.writerow(r)
            logger.info(f"Wrote attempts summary: {out_csv}")
    except Exception as e:
        logger.warning(f"Could not write attempts CSV: {e}")
    ib.disconnect()
    # --- Final attempts flush to day-rolled file ---
    try:
        out_path = _attempts_append(ATTEMPTS)
        if out_path:
            logger.info(f"Wrote attempts (append) to day-rolled file: {out_path}")
    except Exception as _e:
        logger.warning(f"Final attempts append failed: {_e}")
if __name__ == "__main__":
    run_from_csv()
