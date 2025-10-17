from ib_insync import IB, Option, LimitOrder, MarketOrder
from ib_insync.contract import ComboLeg, Contract
import logging
import pandas as pd
from zoneinfo import ZoneInfo
from datetime import datetime

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
            call_close_limit = best_close_limit(row, 'C')
            put_close_limit  = best_close_limit(row, 'P')
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
                        place_debit_spread(ib, symbol, expiration, a_atm, a_oth, 'C', call_close_limit, quantity=qty, action='SELL')
                        logger.info(f"[{symbol}] Weekly-enforce CLOSE CALL(approx) {a_atm}/{a_oth} exp {expiration} @ {call_close_limit}")
                        closed_any = True
                if not closed_any and put_close_limit is not None:
                    a_atm, a_oth, qty = find_approx_spread_to_close(ib, symbol, expiration, 'P', float(atm) if not pd.isna(atm) else None, float(k_put) if not pd.isna(k_put) else None, tol=args.close_tol, max_qty=args.quantity)
                    if qty > 0:
                        place_debit_spread(ib, symbol, expiration, a_atm, a_oth, 'P', put_close_limit, quantity=qty, action='SELL')
                        logger.info(f"[{symbol}] Weekly-enforce CLOSE PUT(approx) {a_atm}/{a_oth} exp {expiration} @ {put_close_limit}")
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
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("PlaceAnOrder")

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
    p.add_argument("--close-tol", type=float, default=0.5,
                   help="Strike tolerance for approximate close matching (e.g., 0.5 for $0.50).")
    p.add_argument("--force-close-side", choices=["call","put","both"], default="both",
                   help="Which side(s) to close in --mode force-close.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print intended actions but do not place orders.")
    p.add_argument("--verbose", action="store_true",
                   help="Verbose logging per row and decision.")
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


def qualify_option(ib: IB, symbol: str, expiration: str, strike: float, right: str) -> Option | None:
    try:
        c = Option(symbol=symbol, lastTradeDateOrContractMonth=expiration,
                   strike=float(strike), right=right.upper(), exchange='SMART', currency='USD')
        return ib.qualifyContracts(c)[0]
    except Exception:
        return None

# --- De-duplication helpers for opens (idempotency per run) ---
OPEN_SEEN_KEYS: set[str] = set()

def _combo_key(symbol: str, right: str, exp: str, longK: float, shortK: float) -> str:
    return f"{symbol.upper()}|{right.upper()}|{exp}|{float(longK):.4f}|{float(shortK):.4f}"

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

def place_debit_spread(ib: IB, symbol: str, expiration: str, long_strike: float, short_strike: float, right: str, limit_price: float, quantity: int = 1, action: str = 'BUY'):
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
        # Support both limit and market orders
        if 'order_type' in place_debit_spread.__code__.co_varnames:
            pass  # for type checkers
        # The function signature is updated, so we use order_type param
    except Exception:
        pass
    try:
        # Accept order_type and limit_price
        import inspect
        argspec = inspect.getfullargspec(place_debit_spread)
        order_type = locals().get('order_type', 'LMT')
    except Exception:
        order_type = 'LMT'
    if not isinstance(order_type, str):
        order_type = 'LMT'
    try:
        if order_type.upper() == 'MKT' or limit_price is None:
            order = MarketOrder(action.upper(), quantity)
            trade = ib.placeOrder(combo, order)
            logger.info(f"[{symbol}] Placed {right} {'MKT' if action.upper()=='BUY' else 'MKT CLOSE'} {long_strike}/{short_strike} exp {expiration} (qty={quantity})")
        else:
            order = LimitOrder(action.upper(), quantity, float(limit_price))
            trade = ib.placeOrder(combo, order)
            logger.info(f"[{symbol}] Placed {right} {'LMT' if action.upper()=='BUY' else 'LMT CLOSE'} {long_strike}/{short_strike} exp {expiration} @ {float(limit_price):.2f} (qty={quantity})")
        return trade
    except Exception as e:
        logger.error(f"[{symbol}] Failed to place {right} spread order: {e}")
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
    trade = place_debit_spread(ib, symbol, expiration, atm_strike, oth_strike, right, None, quantity=n, action='SELL', order_type='MKT')
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

    # SELL the combo to close
    trade = place_debit_spread(ib, symbol, expiration, atm_strike, oth_strike, right, limit_price, quantity=n, action='SELL')
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

def run_from_csv():
    args = parse_args()
    if args.verbose:
        logger.setLevel(logging.DEBUG)
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
        # Enforce that recent CLOSE signals (last 7 days) have been acted on before opening new risk
        try:
            enforce_weekly_closures(ib, df.copy(), args, days=7)
        except Exception as _e:
            logger.warning(f"Weekly closure enforcement skipped due to error: {_e}")
    except Exception as e:
        logger.error(f"Failed to connect to IB: {e}")
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

            # Skip if any essential piece is missing
            if symbol in (None, "nan", "NaN") or not expiration or pd.isna(atm):
                msg = f"[row {idx}] Skipping; missing fields symbol/expiration/atm_strike. sym={symbol}, exp={expiration}, atm={atm}"
                if args.verbose: logger.info(msg)
                else: logger.warning(msg)
                continue

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
                    allow_call = True
                elif stype == "PUT_OPEN":
                    allow_put = True
                elif stype in ("CLOSE","CALL_CLOSE","PUT_CLOSE"):
                    # Cancel any pending OPENs for this ticker before closing
                    cxl = cancel_open_orders_for_symbol(ib, symbol)
                    if cxl > 0:
                        logger.info(f"[{symbol}] Cancelled {cxl} pending OPEN combo order(s) prior to CLOSE")
                    # Attempt to close whichever spread we hold (call and/or put) by inspecting positions
                    closed_any = False
                    # Close call spread if present
                    call_close_limit = enforce_min_limit(best_close_limit(row, 'C'))
                    if not pd.isna(k_call) and call_close_limit is not None:
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
                    if call_close_limit is None and not pd.isna(k_call):
                        if not args.dry_run and close_spread_market_if_present(ib, symbol, expiration, 'C', float(atm), float(k_call), max_qty=args.quantity):
                            logger.info(f"[{symbol}] Submitted CLOSE CALL (MKT fallback) {atm}/{k_call} exp {expiration}")
                            placed += 1
                    # Close put spread if present
                    put_close_limit = enforce_min_limit(best_close_limit(row, 'P'))
                    if not pd.isna(k_put) and put_close_limit is not None:
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
                    if put_close_limit is None and not pd.isna(k_put):
                        if not args.dry_run and close_spread_market_if_present(ib, symbol, expiration, 'P', float(atm), float(k_put), max_qty=args.quantity):
                            logger.info(f"[{symbol}] Submitted CLOSE PUT (MKT fallback) {atm}/{k_put} exp {expiration}")
                            placed += 1
                    if not closed_any:
                        # try approximate match within tolerance
                        if not pd.isna(k_call):
                            vprint(args.verbose, f"[{symbol}] Attempt CLOSE CALL(approx) around atm={atm}, other_hint={k_call} tol={args.close_tol} @ {call_close_limit}")
                            approx_atm, approx_oth, qty = find_approx_spread_to_close(ib, symbol, expiration, 'C',
                                                                                      float(atm), float(k_call), tol=args.close_tol, max_qty=args.quantity)
                            if qty > 0 and call_close_limit is not None:
                                if args.dry_run:
                                    vprint(args.verbose, f"[DRY-RUN] CLOSE CALL(approx) {symbol} {approx_atm}/{approx_oth} exp {expiration} @ {call_close_limit} x{qty}")
                                    closed_any = True
                                    placed += 1
                                else:
                                    if place_debit_spread(ib, symbol, expiration, approx_atm, approx_oth, 'C',
                                                          call_close_limit, quantity=qty, action='SELL'):
                                        logger.info(f"[{symbol}] Submitted CLOSE CALL(approx) {approx_atm}/{approx_oth} exp {expiration} @ {call_close_limit}")
                                        closed_any = True
                                        placed += 1
                        if not closed_any and not pd.isna(k_put):
                            vprint(args.verbose, f"[{symbol}] Attempt CLOSE PUT(approx) around atm={atm}, other_hint={k_put} tol={args.close_tol} @ {put_close_limit}")
                            approx_atm, approx_oth, qty = find_approx_spread_to_close(ib, symbol, expiration, 'P',
                                                                                      float(atm), float(k_put), tol=args.close_tol, max_qty=args.quantity)
                            if qty > 0 and put_close_limit is not None:
                                if args.dry_run:
                                    vprint(args.verbose, f"[DRY-RUN] CLOSE PUT(approx) {symbol} {approx_atm}/{approx_oth} exp {expiration} @ {put_close_limit} x{qty}")
                                    closed_any = True
                                    placed += 1
                                else:
                                    if place_debit_spread(ib, symbol, expiration, approx_atm, approx_oth, 'P',
                                                          put_close_limit, quantity=qty, action='SELL'):
                                        logger.info(f"[{symbol}] Submitted CLOSE PUT(approx) {approx_atm}/{approx_oth} exp {expiration} @ {put_close_limit}")
                                        closed_any = True
                                        placed += 1
                    if not closed_any:
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
                sides = ["call","put"] if args.force_close_side == "both" else [args.force_close_side]
                for side in sides:
                    if side == "call":
                        limit = enforce_min_limit(best_close_limit(row, 'C'))
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
                                else:
                                    done = close_spread_if_present(ib, symbol, expiration, 'C', float(atm), float(k_call), limit, max_qty=args.quantity)
                                    if done:
                                        logger.info(f"[{symbol}] Submitted FORCE-CLOSE CALL {atm}/{k_call} exp {expiration} @ {limit}")
                            if not done:
                                vprint(args.verbose, f"[{symbol}] Attempt CLOSE CALL(approx) around atm={atm}, other_hint={k_call} tol={args.close_tol} @ {limit}")
                                a_atm, a_oth, qty = find_approx_spread_to_close(ib, symbol, expiration, 'C',
                                                                                float(atm) if not pd.isna(atm) else None,
                                                                                float(k_call) if not pd.isna(k_call) else None,
                                                                                tol=args.close_tol, max_qty=args.quantity)
                                if qty > 0:
                                    if args.dry_run:
                                        vprint(args.verbose, f"[DRY-RUN] CLOSE CALL(approx) {symbol} {a_atm}/{a_oth} exp {expiration} @ {limit} x{qty}")
                                    else:
                                        place_debit_spread(ib, symbol, expiration, a_atm, a_oth, 'C', limit, quantity=qty, action='SELL')
                                        logger.info(f"[{symbol}] Submitted FORCE-CLOSE CALL(approx) {a_atm}/{a_oth} exp {expiration} @ {limit}")
                    else:
                        limit = enforce_min_limit(best_close_limit(row, 'P'))
                        if limit is None:
                            vprint(args.verbose, f"[{symbol}] FORCE-CLOSE PUT skipped; limit below min or missing")
                        else:
                            done = False
                            if not pd.isna(k_put):
                                vprint(args.verbose, f"[{symbol}] Attempt CLOSE PUT exact {atm}/{k_put} exp {expiration} @ {limit}")
                                if args.dry_run:
                                    vprint(args.verbose, f"[DRY-RUN] CLOSE PUT {symbol} {atm}/{k_put} exp {expiration} @ {limit} x{args.quantity}")
                                    done = True
                                else:
                                    done = close_spread_if_present(ib, symbol, expiration, 'P', float(atm), float(k_put), limit, max_qty=args.quantity)
                                    if done:
                                        logger.info(f"[{symbol}] Submitted FORCE-CLOSE PUT {atm}/{k_put} exp {expiration} @ {limit}")
                            if not done:
                                vprint(args.verbose, f"[{symbol}] Attempt CLOSE PUT(approx) around atm={atm}, other_hint={k_put} tol={args.close_tol} @ {limit}")
                                a_atm, a_oth, qty = find_approx_spread_to_close(ib, symbol, expiration, 'P',
                                                                                float(atm) if not pd.isna(atm) else None,
                                                                                float(k_put) if not pd.isna(k_put) else None,
                                                                                tol=args.close_tol, max_qty=args.quantity)
                                if qty > 0:
                                    if args.dry_run:
                                        vprint(args.verbose, f"[DRY-RUN] CLOSE PUT(approx) {symbol} {a_atm}/{a_oth} exp {expiration} @ {limit} x{qty}")
                                    else:
                                        place_debit_spread(ib, symbol, expiration, a_atm, a_oth, 'P', limit, quantity=qty, action='SELL')
                                        logger.info(f"[{symbol}] Submitted FORCE-CLOSE PUT(approx) {a_atm}/{a_oth} exp {expiration} @ {limit}")
                continue

            # --- CALL debit spread (ATM long / OTM short) ---
            if allow_call:
                raw_call_limit = best_theoretical_limit(row, 'C')
                call_limit = enforce_min_limit(raw_call_limit)
                if call_limit is not None and not pd.isna(k_call):
                    key = _combo_key(symbol, 'C', expiration, float(atm), float(k_call))
                    if key in OPEN_SEEN_KEYS:
                        vprint(args.verbose, f"[{symbol}] Skipping duplicate CALL OPEN (already attempted this run) {atm}/{k_call} exp {expiration}")
                    elif has_working_open_order(ib, symbol, expiration, 'C', float(atm), float(k_call)):
                        vprint(args.verbose, f"[{symbol}] Skipping CALL OPEN; working BUY order exists for {atm}/{k_call} exp {expiration}")
                    elif has_open_position_for_spread(ib, symbol, expiration, 'C', float(atm), float(k_call)):
                        vprint(args.verbose, f"[{symbol}] Skipping CALL OPEN; position already held for {atm}/{k_call} exp {expiration}")
                    else:
                        vprint(args.verbose, f"[{symbol}] Attempt CALL OPEN {atm}/{k_call} exp {expiration} @ {call_limit}")
                        if args.dry_run:
                            vprint(args.verbose, f"[DRY-RUN] CALL OPEN {symbol} {atm}/{k_call} exp {expiration} @ {call_limit} x{args.quantity}")
                        else:
                            place_debit_spread(ib, symbol, expiration, float(atm), float(k_call), 'C', call_limit, quantity=args.quantity)
                            OPEN_SEEN_KEYS.add(key)
                            placed += 1
                elif call_limit is None and not pd.isna(k_call):
                    key = _combo_key(symbol, 'C', expiration, float(atm), float(k_call))
                    if key not in OPEN_SEEN_KEYS and not has_working_open_order(ib, symbol, expiration, 'C', float(atm), float(k_call)) and not has_open_position_for_spread(ib, symbol, expiration, 'C', float(atm), float(k_call)):
                        if args.dry_run:
                            vprint(args.verbose, f"[DRY-RUN] CALL OPEN MKT {symbol} {atm}/{k_call} exp {expiration} x{args.quantity}")
                        else:
                            place_debit_spread(ib, symbol, expiration, float(atm), float(k_call), 'C', None, quantity=args.quantity, action='BUY', order_type='MKT')
                            OPEN_SEEN_KEYS.add(key); placed += 1
                else:
                    vprint(args.verbose, f"[{symbol}] Call skipped; limit={call_limit}, k_call={k_call}")
            # --- PUT debit spread (ATM long / lower strike short) ---
            if allow_put:
                raw_put_limit = best_theoretical_limit(row, 'P')
                put_limit = enforce_min_limit(raw_put_limit)
                if put_limit is not None and not pd.isna(k_put):
                    key = _combo_key(symbol, 'P', expiration, float(atm), float(k_put))
                    if key in OPEN_SEEN_KEYS:
                        vprint(args.verbose, f"[{symbol}] Skipping duplicate PUT OPEN (already attempted this run) {atm}/{k_put} exp {expiration}")
                    elif has_working_open_order(ib, symbol, expiration, 'P', float(atm), float(k_put)):
                        vprint(args.verbose, f"[{symbol}] Skipping PUT OPEN; working BUY order exists for {atm}/{k_put} exp {expiration}")
                    elif has_open_position_for_spread(ib, symbol, expiration, 'P', float(atm), float(k_put)):
                        vprint(args.verbose, f"[{symbol}] Skipping PUT OPEN; position already held for {atm}/{k_put} exp {expiration}")
                    else:
                        vprint(args.verbose, f"[{symbol}] Attempt PUT OPEN {atm}/{k_put} exp {expiration} @ {put_limit}")
                        if args.dry_run:
                            vprint(args.verbose, f"[DRY-RUN] PUT OPEN {symbol} {atm}/{k_put} exp {expiration} @ {put_limit} x{args.quantity}")
                        else:
                            place_debit_spread(ib, symbol, expiration, float(atm), float(k_put), 'P', put_limit, quantity=args.quantity)
                            OPEN_SEEN_KEYS.add(key)
                            placed += 1
                elif put_limit is None and not pd.isna(k_put):
                    key = _combo_key(symbol, 'P', expiration, float(atm), float(k_put))
                    if key not in OPEN_SEEN_KEYS and not has_working_open_order(ib, symbol, expiration, 'P', float(atm), float(k_put)) and not has_open_position_for_spread(ib, symbol, expiration, 'P', float(atm), float(k_put)):
                        if args.dry_run:
                            vprint(args.verbose, f"[DRY-RUN] PUT OPEN MKT {symbol} {atm}/{k_put} exp {expiration} x{args.quantity}")
                        else:
                            place_debit_spread(ib, symbol, expiration, float(atm), float(k_put), 'P', None, quantity=args.quantity, action='BUY', order_type='MKT')
                            OPEN_SEEN_KEYS.add(key); placed += 1
                else:
                    vprint(args.verbose, f"[{symbol}] Put skipped; limit={put_limit}, k_put={k_put}")

        except Exception as e:
            logger.error(f"[row {idx}] Unexpected error: {e}")

    logger.info(f"Completed. Orders {'simulated' if args.dry_run else 'attempted'}: {placed}")
    ib.disconnect()

if __name__ == "__main__":
    run_from_csv()
