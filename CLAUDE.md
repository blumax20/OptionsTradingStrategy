# Options Trading System - Architecture & Bug Fixes

## Overview

This document summarizes the architecture of the Interactive Brokers options trading system and the bug fixes implemented to prevent unwanted market orders.

**Last Updated:** February 12, 2026

---

## System Architecture

### Core Components

1. **DailyCycleManagement.py**
   - Orchestrates daily trading cycles (after-hours, preclose, RTH risk exits)
   - Delegates to PlaceAnOrder.py for actual order placement
   - Manages multi-stage close logic with pricing fallbacks

2. **PlaceAnOrder.py**
   - Core order placement logic
   - Handles from-signal mode (CSV-driven) and force-close mode (position-driven)
   - Implements pricing fallback chains for limit orders
   - Contains CALL_OPEN, PUT_OPEN, and CLOSE handlers

3. **ib_close_guard.py**
   - Prevents duplicate order placement
   - Checks for existing working close orders via ib_insync

### Trading Cycles

#### After-Hours Cycle (5:00 PM)
- Triggered via `_after_hours_batch_placement()`
- Opens new positions from CSV signals (CALL_OPEN, PUT_OPEN)
- Closes positions via multi-stage delegation

#### Preclose Cycle (3:00 PM)
- Triggered via `_pre_close_market_conversion()`
- Converts stubborn unfilled limit orders to market orders (during market hours)
- Cancels existing limit orders and re-places with live pricing

#### RTH Risk Exits (9:30 AM)
- Triggered at market open
- Force-closes high-risk positions
- Uses live pricing (market is open)

---

## Multi-Stage Close Architecture

**Location:** `DailyCycleManagement._delegate_close_from_csvs_within()`

### Stage 1: from-signal CLOSE (CSV-driven)
- Uses exact expiration + strike matching
- Falls back to approximate matching (±7 days)
- **FIX F APPLIED:** Skips instead of placing MARKET when no match found

### Stage 1.5: force-close with live pricing
- Triggered after Stage 1 completes
- Uses `force_close_symbol_via_positions()` with `--use-live-close mid`
- Scans positions by symbol (ignores expiration mismatch)
- After-hours: Fails (market closed, no live quotes)
- During market hours (preclose): Works correctly

### Stage 2: force-close with CSV pricing
- Triggered after Stage 1.5 completes
- Uses `force_close_symbol_via_positions()` with `--use-live-close off`
- Pricing fallback chain:
  1. Live quotes (fails after-hours)
  2. **FIX O APPLIED:** Previous trading day's CSV (has position strikes + live prices from 9:35 AM)
  3. **FIX C APPLIED:** Skips if all fail (no MARKET fallback)
  4. **FIX J APPLIED:** At 3pm preclose, MARKET order if `--allow-market-fallback` set

---

## Pricing Fallback Logic

### CLOSE Orders

**Function:** `force_close_symbol_via_positions()` at PlaceAnOrder.py:1812-1930

**Fallback Chain (Updated Feb 7):**
1. **Live Quotes** (lines 1834-1856)
   - Uses `live_spread_price()` with `--use-live-close` scheme (mid/join/off)
   - Fails after-hours (market closed)

2. **Previous Trading Day's CSV** (lines 1893-1922) - **FIX O**
   - Uses `_get_csv_paths_for_close()` to get previous day's CSV
   - Symbol-only lookup (no expiration filter)
   - Uses `width_aligned_close_limit()` function
   - Previous day's CSV has position strikes (Fix I) + live prices (Fix N at 9:35 AM)
   - Applies 10% buffer: `limit * 0.90`
   - Checks min_limit (0.05 or 0.01 for preclose)

3. **Skip or MARKET** (lines 1925-1945)
   - If all pricing fails and `--allow-market-fallback` NOT set: skip order (Fix C)
   - If `--allow-market-fallback` IS set (3pm preclose only): place MARKET order (Fix J)
   - Records reason: `no_viable_limit_all_fallbacks_failed` or `market_fallback_preclose`

**Note:** Stale position market prices fallback was removed - previous day's CSV with live prices is more reliable.

### OPEN Orders

**Function:** CALL_OPEN handler at PlaceAnOrder.py:2639-3184, PUT_OPEN at 2667-3342

**Pricing Logic:**
- Tries limit columns first: `call_debit_limit_1`, `call_debit_limit_2_5`, `call_debit_limit_5`
- **FIX D APPLIED:** Falls back to theo columns: `call_debit_theo_1`, `call_debit_theo_2_5`, `call_debit_theo_5`
- Uses `enforce_min_limit()` to validate pricing
- Skips with `no_viable_limit_or_conditions` if no valid pricing

---

## Bug Fixes Implemented

### Fix A: Consistent Row Selection (Jan 26)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py:1872

**Issue:** Inconsistent CSV row selection when duplicates exist
- DailyCycleManagement used `.iloc[-1]` (last row)
- PlaceAnOrder used `.iloc[0]` (first row)

**Fix:** Changed PlaceAnOrder to use `.iloc[-1]` for consistency

**Impact:** Uses latest CSV data when symbol appears multiple times

---

### Fix B: Enhanced Logging (Jan 26)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py:598-601 (width_aligned_close_limit function)

**Issue:** Insufficient visibility into theo fallback behavior

**Fix:** Added logging:
- `Using theo fallback for {right} width={width}: {theo_v}` when theo used
- `Both limit and theo are None for {right} width={width}` when both fail

**Impact:** Better diagnostics for pricing failures

---

### Fix C: Skip When All Pricing Fails (Jan 27)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py:1965-1976 (force_close_symbol_via_positions)

**Issue:** Placed MARKET orders when all limit pricing fallbacks failed

**Fix:**
```python
if limit is None:
    logger.warning(f"[{symbol}] All limit pricing fallbacks failed - SKIPPING order")
    record_attempt(
        symbol, "force_close", "skipped",
        "no_viable_limit_all_fallbacks_failed",
        exp=str(exp), right=right.upper(),
    )
    continue  # Skip this spread, don't place market order

order_type = "LMT"  # Only place limit orders
```

**Impact:** Prevents bad market fills when pricing unavailable; position carried to next day

---

### Fix D: Theo Fallback for OPEN Orders (Jan 29)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py:3071-3107 (CALL_OPEN), 3230-3268 (PUT_OPEN)

**Issue:** OPEN orders skipped when CSV had theo values but no limit values

**Fix:** Extended column search to include theo columns after limit columns

**Impact:** OPEN orders now use theo values as fallback, matching CLOSE order behavior

---

### Fix F: Remove MARKET Fallback from from-signal CLOSE (Feb 3)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py:2830-2844

**Issue:** from-signal CLOSE handler called `close_any_spread_for_symbol()` when expiration mismatch occurred, which ALWAYS places MARKET orders (hardcoded at line 1755)

**Root Cause:**
```python
# OLD CODE (REMOVED)
if not closed_any:
    n_closed = close_any_spread_for_symbol(ib, symbol, side=restrict, max_qty=args.quantity)
    # This function has: order_type="MKT" hardcoded at line 1755
```

**Fix:**
```python
# NEW CODE
if not closed_any:
    # Defer to Stage 1.5/2 which use force_close_symbol_via_positions()
    logger.info(f"[{symbol}] from-signal CLOSE: exact/approx match failed for exp {expiration}; deferring to force-close Stage 1.5/2")
    record_attempt(symbol, "close", "skipped", "from_signal_exp_mismatch_defer_to_force_close", exp=str(expiration))
continue
```

**Impact:**
- Prevents MARKET orders from Stage 1 when CSV expiration doesn't match position expiration
- Stage 1.5/2 handle these positions with proper pricing fallbacks
- All 7 market orders on Feb 2-3 were from this bug

---

### Fix H: CSV-Independent Force-Close Mode (Feb 6)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py:2305-2331

**Issue:** Force-close mode with `--symbols` failed when today's CSV didn't exist, even though force-close is designed to be CSV-independent and uses live pricing.

**Root Cause:**
```python
# OLD CODE - CSV loading happened BEFORE force-close handling
csv_path = combined_csv_path_for_today(args.date)
try:
    df = pd.read_csv(csv_path)
except FileNotFoundError:
    logger.error(f"Combined CSV not found: {csv_path}")
    return  # <-- BUG: exits before reaching force-close logic at line 2432
```

The CSV-independent force-close code at lines 2430-2448 was never reached because the CSV check happened first and exited early.

**Fix:**
```python
# NEW CODE - Force-close with --symbols is handled FIRST
if args.mode == "force-close" and args.symbols:
    logger.info(f"Force-close mode (CSV-independent): symbols={sorted(list(only))}")
    ib = IB()
    ib.connect('127.0.0.1', 7497, clientId=101)
    # ... process symbols using live pricing
    return

# For other modes, require CSV
csv_path = combined_csv_path_for_today(args.date)
# ...
```

**Impact:**
- 3pm preclose now works even when today's CSV hasn't been created yet
- Force-close uses live pricing from IB instead of requiring CSV
- Attempts are properly logged even without CSV

---

### Fix I: Position-Aware CLOSE Signal Pricing (Feb 6)
**Status:** ✓ IMPLEMENTED

**Location:** listener.py:418-474 (_get_position_for_symbol), listener.py:728-950 (get_option_data)

**Issue:** When a CLOSE signal was received, listener.py populated the CSV with limit prices for **new hypothetical spreads** based on current stock price and ~30 DTE, instead of the **actual held position's strikes and expiration**. This made CSV limit prices useless for closing existing positions.

**Root Cause:**
```python
# OLD CODE - get_option_data() ignored signal type
def get_option_data(symbol: str, width: int = 5):
    # Always calculated NEW strikes based on current price
    atm_strike = _closest_existing(strikes_all, current_price)
    # Always used ~30 DTE expiration
    expiry_str = min(valid, key=lambda t: abs(t[1] - TARGET_DTE))[0]
```

**Fix:**
```python
# NEW CODE - get_option_data() checks signal type and uses position data for CLOSE
def get_option_data(symbol: str, width: int = 5, signal_type: str | None = None):
    # For CLOSE signals, look up actual held position
    if signal_type == "CLOSE":
        position_info = _get_position_for_symbol(ib, symbol)
        if position_info:
            # Use position's actual expiration and strikes
            expiry_str = position_info['expiration']
            atm_strike = position_info['atm_strike']
            # ...
```

**Helper function added:**
- `_get_position_for_symbol(ib, symbol)` - Queries IB positions and returns actual expiration, strikes, right (C/P), and width for the held spread

**Impact:**
- CLOSE signals now generate CSV rows with prices for the ACTUAL held spread
- PlaceAnOrder.py can use CSV theo prices as reliable fallback for after-hours closes
- Falls back to current behavior if no position found for the symbol

---

### Fix J: Preclose MARKET Fallback (Feb 6)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py:1925-1945, DailyCycleManagement.py:524-536

**Issue:** At 3pm preclose, if all limit pricing fails, the order was skipped. But the market is OPEN at 3pm, so a MARKET order would fill at reasonable prices.

**Fix:** Added `--allow-market-fallback` flag that DailyCycleManagement passes during preclose:
```python
if limit is None:
    if getattr(args, "allow_market_fallback", False):
        # Preclose mode: market is open, MARKET order is acceptable
        logger.warning(f"[{symbol}] All limit pricing failed - using MARKET fallback")
        order_type = "MKT"
    else:
        # After-hours: skip to avoid bad MARKET fill
        continue
```

**Impact:**
- 3pm preclose uses MARKET as last resort (market is open, fills are reasonable)
- 5pm after-hours still skips (market closed, fills would be terrible)

---

### Fix M: Always Use Theo Values After Hours (Feb 7)
**Status:** ✓ IMPLEMENTED

**Location:** listener.py:624-724 (row assembly)

**Issue:** After hours, quote-based limit prices are unreliable (stale bids/asks, wide spreads). The theo values calculated via Black-Scholes are more accurate.

**Fix:** Simplified listener to always populate `*_limit_*` columns with theo values after hours:
```python
# In row assembly:
"call_debit_limit_1": theo.get("call_debit_theo_1"),
"put_debit_limit_1": theo.get("put_debit_theo_1"),
# ... same for _2_5 and _5 widths
```

**Impact:**
- After-hours signals always use Black-Scholes theo values in limit columns
- Quote-based prices still logged for diagnostics but not used
- Simpler code, more predictable behavior

---

### Fix N: Live Price Enrichment at Market Open (Feb 7)
**Status:** ✓ IMPLEMENTED

**Location:** LiquidityFilter.py:559-685 (`_fetch_live_spread_price`, `enrich_live_spread_prices`)

**Issue:** At market open, the CSV has theo-only prices from after-hours. Need to update limit columns with live market prices.

**Fix:** Added live spread price fetching to LiquidityFilter.py (already called by DailyCycleManagement at 9:35 AM):
- `_fetch_live_spread_price()` - Fetches debit spread price from IB (ask_long - bid_short)
- `enrich_live_spread_prices()` - Updates all limit columns with live prices
- Called automatically via `enrich_if_rth()` when `update_prices=True` (default)
- Prices capped at spread width (a $1 spread can't exceed $1.00)

**Impact:**
- 9:35 AM enrichment updates previous day's CSV with fresh live prices
- 3pm preclose can use these live prices via Fix O
- Manual use: `python LiquidityFilter.py --day-dir C:\OptionsHistory\26_02_07 --update-prices`

---

### Fix O: Previous Trading Day CSV Fallback (Feb 7)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py:492-522 (`_get_previous_trading_day_folder`, `_get_csv_paths_for_close`), PlaceAnOrder.py:1893-1922

**Issue:** At 3pm preclose, if live pricing fails, the system only looked at today's CSV. But positions opened yesterday won't have a row in today's CSV.

**Fix:** Changed CSV fallback to use only the previous trading day's CSV:
```python
def _get_csv_paths_for_close(date_override: str | None = None) -> list[Path]:
    """Return CSV path: previous trading day only."""
    paths = []
    prev_folder = _get_previous_trading_day_folder()
    if prev_folder:
        prev_path = OUTPUT_BASE / prev_folder / "combined_listener_spreads.csv"
        paths.append(prev_path)
    return paths
```

**Why this works:**
1. **5 PM (prev day):** Listener populates CSV with position-based strikes (Fix I)
2. **9:35 AM (today):** LiquidityFilter updates CSV with live prices (Fix N)
3. **3 PM (today):** Preclose uses these live prices for closing

**Impact:**
- 3pm preclose has reliable limit pricing from previous day's CSV
- Stale position market prices fallback removed (not needed with live prices)
- Simplified fallback chain: live quotes → previous day CSV → MARKET (preclose only)

---

### Fix P: Theo Spread Pricing Uses Both IVs (Feb 7)
**Status:** ✓ IMPLEMENTED

**Location:** listener.py:366-387 (`_theo_spread_debits`), listener.py:668-693 (call site)

**Issue:** Theo spread pricing used a single IV for both legs. For symbols like FSK with 20% IV, both $2.5 and $5 spreads calculated to identical values (0.58) because far OTM options are essentially worthless at low IV.

**Root Cause:**
```python
# OLD CODE - single sigma for both legs
sigma = iv_atm or iv_otm or 0.25
call_long = _bs_price(S, atm, T, r, sigma, call=True)
call_short = _bs_price(S, atm + W, T, r, sigma, call=True)  # Same sigma!
```

**Fix:** Modified `_theo_spread_debits()` to accept separate IVs for ATM and OTM legs:
```python
def _theo_spread_debits(S, atm, T, sigma_atm, sigma_otm=None, ...):
    if sigma_otm is None:
        sigma_otm = sigma_atm
    call_long = _bs_price(S, atm, T, r, sigma_atm, call=True)   # ATM IV
    call_short = _bs_price(S, atm + W, T, r, sigma_otm, call=True)  # OTM IV
```

**Impact:**
- When both `iv_atm` and `iv_otm` are present, each leg uses its own IV
- Accounts for volatility skew (OTM options typically have higher IV)
- Falls back to single IV when only one is available (same as before)

---

### Fix Q: Low-OI Cancellation Enhancements (Feb 7)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py:2940-2998 (`_cancel_low_oi_working_orders_from_csv`)

**Issue:** The existing low-OI cancellation logic needed two enhancements:
1. Should only cancel completely unfilled orders (not partially filled)
2. Cancellations should be logged to attempts CSV for auditability

**Fix 1:** Changed status filter from blacklist to whitelist:
```python
# OLD: if st in ("filled", "cancelled", "apicancelled"): continue
# NEW: Only cancel unfilled orders
if st not in ("presubmitted", "submitted"):
    continue
```

**Fix 2:** Added attempts CSV logging after cancellation:
```python
_AttemptLogger.write(
    symbol=sym,
    action="cancel_open",
    status="placed",
    reason="low_oi_both_legs",
    exp=exp, right=right, atm=str(atm), oth=str(oth),
)
```

**Impact:**
- Partially filled orders are no longer cancelled (only unfilled)
- All low-OI cancellations appear in attempts CSV with reason `low_oi_both_legs`
- Better auditability of RTH cleanup actions

---

### Fix R: Secdef Retry with Backoff (Feb 7)
**Status:** ✓ IMPLEMENTED

**Location:** listener.py:164-210 (`_collect_secdef`)

**Issue:** AMZN CALL_OPEN signal on 2/6/2026 was skipped because `_collect_secdef()` returned empty strikes list:
1. No retry logic, only 250ms delay
2. Without strikes list, ATM was guessed as `round(209.01) = 209` (doesn't exist!)
3. OTM was guessed as `209 + 5 = 214` (also doesn't exist!)
4. AMZN actual strikes are 205, 210, 215, 220... (5-wide spacing)
5. IB contract qualification failed → order skipped with `no_viable_limit_or_conditions`

**Fix:** Added retry with exponential backoff:
```python
def _collect_secdef(ib: IB, symbol: str, con_id: int, max_retries: int = 3):
    delays = [0.5, 1.0, 2.0]  # Exponential backoff
    for attempt in range(max_retries):
        params = ib.reqSecDefOptParams(symbol, '', 'STK', con_id)
        ib.sleep(delays[attempt])
        # ... parse params ...
        if strikes_all:
            return expirations, strikes_all, trading_classes, multipliers
        # Log and retry if no strikes
    return expirations, [], trading_classes, multipliers
```

**Impact:**
- Retries up to 3 times with 0.5s, 1.0s, 2.0s delays
- Logs warning on retry and final failure
- Should resolve AMZN and other high-priced stocks with 5-wide strike spacing

---

### Fix S: Close Worthless Spreads with Fixed Pricing (Feb 9)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py:2001-2004, 2091-2111, 2289-2339, 2727-2743, 2766-2782, 3033-3037

**Issue:** NWG and BSY had MARKET orders placed on 2/9/2026 at 5pm because:
1. Both legs of the spread were nearly worthless (< $0.05)
2. Combo close orders failed because spread value was negligible
3. System fell back to `close_any_spread_for_symbol()` which always uses MARKET orders
4. MARKET orders after-hours have poor fills

**Root Causes Fixed:**
1. `opposite_unwind_before_open` (BSY) - called `close_any_spread_for_symbol()` → MARKET
2. `force-close` mode fallback (NWG) - called `close_any_spread_for_symbol()` → MARKET
3. Both-legs-worthless case wasn't handled (only one-leg-worthless was)

**Fix:** When both legs are worthless (< $0.05), close with guaranteed-fill fixed pricing:
```python
# In force_close_symbol_via_positions():
elif long_worthless and short_worthless:
    skip_combo = True
    both_worthless = True
    # ...then later:
    # Long position: sell for $0.01
    leg_order = LimitOrder("SELL", int(abs(pos)), 0.01)
    # Short position: buy for $0.05
    leg_order = LimitOrder("BUY", int(abs(pos)), 0.05)
```

**Additional Changes:**
- DailyCycleManagement Stage 2 now passes `--fallback-individual-legs` by default
- Removed MARKET fallback from force-close CSV mode (lines 3033-3041)
- Replaced `close_any_spread_for_symbol()` in opposite_unwind_before_open with `force_close_symbol_via_positions()`

**Impact:**
- Worthless spreads (both legs < $0.05) close with fixed pricing: sell long @$0.01, buy short @$0.05
- Net cost: $0.04 per contract (acceptable for worthless positions)
- No more MARKET orders for worthless spread closes
- Once legs submitted, symbol is free for new OPEN signals

---

### Fix U: Cross-ClientId Order Visibility + After-Hours Order Tagging (Feb 10)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (lines 127, 370, 1296, 1315, 1586), ib_close_guard.py (line 8)

**Issue:** Three separate bugs caused the 5pm reconcile's buffered close orders to be invisible to downstream stages, resulting in unbuffered duplicate orders (CP at $1.54, MKC at $4.98, PFE at $0.83). The same root cause also prevented the 3pm preclose from cancelling and replacing stale close orders (TSM).

**Bug U1: Three DCM functions missing `reqAllOpenOrders()`**

In ib_insync, `openTrades()` on a fresh connection only returns trades from the current clientId. Orders placed by other clientIds (reconcile=880+random, PlaceAnOrder=101) are invisible unless `reqAllOpenOrders()` is called first.

Three functions affected:
- `_has_working_close_order` (clientId=883): Detection for skip/cancel decisions
- `_working_close_limit_symbols` (clientId=887): Preclose candidate gathering
- `_cancel_symbol_close_orders` (clientId=886): Used `reqOpenOrders()` instead of `reqAllOpenOrders()`

**Fix:** Added `ib.reqAllOpenOrders()` + `ib.sleep(0.5)` before `ib.openTrades()` in all three functions.

**Bug U2: `_place_combo` missing `outsideRth` and TIF**

The reconcile's `_place_combo` created orders without `outsideRth=True`, causing IB to set them to "Inactive" status after hours. While DCM detected Inactive orders, the ib_close_guard only detected Inactive+GTC, not Inactive+DAY.

**Fix:** Added `order.tif = "DAY"` and `order.outsideRth = True` to both LimitOrder and MarketOrder paths in `_place_combo`.

**Bug U3: IB Close Guard ClientId Collision**

Both DCM's `_has_working_close_order` and `ib_close_guard.has_working_auto_close` used clientId=883, causing connection failures when both ran concurrently.

**Fix:** Changed `has_working_auto_close` default clientId from 883 to 884.

**Impact:**
- After-hours reconcile orders now visible to all downstream stages (no duplicate unbuffered orders)
- 3pm preclose can now detect, cancel, and replace stale close orders with better pricing
- Close guard no longer collides with DCM's order detection

---

### Fix V: Width Bucket Selection for Small Strikes (Feb 10)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py:1215 (`_get_theo_limit`)

**Issue:** PFE with strikes 26.0/26.5 (width=$0.50) was assigned to the wrong width bucket. The boundary-based logic `abs(0.5-1.0) < 0.5` evaluates to `0.5 < 0.5` which is False, causing $0.50 widths to fall through to the "5" bucket. PFE got `call_debit_limit_5=$1.85` instead of `call_debit_limit_1=$0.83`.

**Fix:** Replaced boundary-based bucket selection with nearest-neighbor approach matching PlaceAnOrder's `_width_bucket()`:
```python
_buckets = [("1", 1.0), ("2_5", 2.5), ("5", 5.0)]
bucket, _ = min(_buckets, key=lambda t: abs(width - t[1]))
```

**Impact:**
- $0.50 width → bucket "1" (distance 0.5 from 1.0, closest)
- $1.00 width → bucket "1" (exact match)
- Consistent with PlaceAnOrder's existing `_width_bucket()` function

---

### Fix W: Duplicate Attempts CSV Entries (Feb 10)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py:3429-3435, 2400-2403

**Issue:** Every attempts CSV entry appeared twice because of double-write:
1. `record_attempt()` (line 405): Immediately appends each row via `_attempts_append([row])`
2. Final flush (line 3431): Re-writes ALL accumulated rows via `_attempts_append(ATTEMPTS)`
3. Force-close flush (line 2401): Another redundant flush

**Fix:** Removed the final bulk flush at line 3431 and the force-close flush at line 2401. The per-row writes in `record_attempt()` are sufficient and more crash-resilient.

**Impact:** Each attempts entry now appears exactly once in the CSV.

---

### Fix X1: Deduplicate CLOSE Symbols in from-signal Mode (Feb 11)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py (place_debit_spread guard-skip path, from-signal CSV processing)

**Issue:** ENB had 2 CLOSE rows in Feb 11 CSV (timestamps 16:07:03 and 16:16:12). PlaceAnOrder processed both rows. The `has_working_auto_close()` guard caught the first but failed on the second due to rapid IB disconnect/reconnect (7-second gap). Two close orders submitted = risk of reverse position.

**Fix:** Two changes:
1. When `has_working_auto_close()` skips a CLOSE order, add the symbol to `CLOSE_SEEN_KEYS` so subsequent CSV rows for the same symbol are caught by the in-memory set
2. Pre-filter the CLOSE DataFrame with `drop_duplicates(subset="symbol", keep="last")` to keep only the most recent CLOSE row per symbol

**Impact:** Each symbol gets at most one CLOSE order per invocation, regardless of CSV duplicates.

---

### Fix X2: MARKET Orders for Worthless Legs at Preclose (Feb 11)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py (force_close_symbol_via_positions, both_worthless leg placement)

**Issue:** Fix S placed individual leg orders at $0.01/$0.05 as DAY limit orders. These prices are too aggressive for illiquid options and the orders expire unfilled at 4pm.

**Fix:** Check `--allow-market-fallback` flag (set during preclose when market is open):
- **Preclose (market open):** Use MarketOrder for individual worthless legs (guaranteed fill)
- **After-hours (market closed):** Keep LimitOrder at $0.01/$0.05 (no MARKET after-hours)

Reason strings differentiate: `both_worthless_market_preclose` vs `both_worthless_fixed_price`

**Impact:** Worthless positions (BSY, NLY, NWG) close at preclose via MARKET when limit pricing fails after-hours.

---

### Fix X3: Risk Exits Use avgCost + DTE Fallback (Feb 11)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (_rth_risk_exits → _process_vertical)

**Issue:** `_rth_risk_exits()` submitted 0 orders since Nov 2025 (~3 months). Root cause: `reqExecutions()` with `openClose == "O"` filter returned empty results (IB API limited retention, cleared on TWS restart). `_avg_open_price()` returned `(None, None)` for every leg → `_process_vertical()` returned early without evaluating stop/TP thresholds.

**Fix:** Two changes:
1. **Entry price:** Use `avgCost` from `ib.positions()` (always available) instead of `reqExecutions()`. `avgCost / 100.0` gives per-share cost; `long_entry - short_entry` = net debit entry.
2. **Position age:** Try execution-based age first; fall back to DTE estimate (`estimated_age = 30 - current_dte`, assuming ~30 DTE at entry).

Added diagnostic logging: `Risk exits: SYM C/P entry=X.XX curr=X.XX width=X.XX stop=T/F tp=T/F`

**Impact:** Risk exits now functional. MNST (75% loss), ODFL (>100% gain), HRL (no close signal but caught by stop/TP) should trigger on next market day.

---

### Fix X4: Apply 5% Close Buffer + Width Cap (Feb 11)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py (width_aligned_close_limit, force-close CSV fallback)

**Issue:** 5% buffer only existed in DCM's `_get_theo_limit()` (direct-close path). The main close flow through PlaceAnOrder's from-signal mode used `width_aligned_close_limit()` with no buffer. Additionally, no width cap existed - T's $0.60 limit exceeded its $0.50 spread width.

**Fix:** In `width_aligned_close_limit()`:
1. **5% buffer:** `buffered = round(v * 0.95, 2)`
2. **Width cap:** `capped = min(buffered, round(width, 2))`

Also changed force-close CSV fallback from 10% buffer to 5% (since `width_aligned_close_limit` now applies 5% internally, preventing double-buffering).

**Impact:**
- TSM (width=2.50): $2.47 × 0.95 = $2.35 (was $2.47)
- T (width=0.50): min($0.57, $0.50) = $0.50 (was $0.60, exceeding width!)
- All close limits now bounded by actual spread width

---

### Fix X5: Listener - Separate Theo and Limit Columns (Feb 11)
**Status:** ✓ IMPLEMENTED

**Location:** listener.py (row assembly, lines 727-736)

**Issue:** Fix M made `*_limit_*` columns = `*_theo_*` values. Both columns were identical, losing the distinction between model prices (Black-Scholes) and live market prices.

**Fix:** Set all limit columns to `None` in row assembly:
- `call_debit_limit_1`: None (was theo value)
- `put_debit_limit_1`: None (was theo value)
- Same for `_2_5`, `_5`, and non-width-specific `call_debit_limit`/`put_debit_limit`

Theo columns remain unchanged (still populated with Black-Scholes values).

**Pricing flow after Fix X5:**
- **5 PM (listener):** Limit=None, Theo=Black-Scholes → PlaceAnOrder falls back to theo (Fix B)
- **9:35 AM (LiquidityFilter):** Limit updated with live prices (Fix N) → PlaceAnOrder uses limit
- **3 PM (preclose):** Limit has live prices from 9:35 AM → PlaceAnOrder uses these

**Impact:** Clean separation of model vs market prices. Live prices only appear after LiquidityFilter enrichment.

---

### Fix Y1: Increase Live Pricing Timeout for Risk Exits (Feb 12)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py (argparse + force_close_symbol_via_positions), DailyCycleManagement.py (_rth_risk_exits)

**Issue:** At 9:39 AM on Feb 12, risk exits correctly identified positions for stop-loss/take-profit but `live_spread_price()` used a 3-second timeout. IB option market data for low-liquidity strikes doesn't populate within 3s at market open. 8 of 15 symbols skipped with `no_viable_limit_all_fallbacks_failed`.

**Fix:**
- Added `--live-timeout` CLI argument to PlaceAnOrder.py (default 3.0)
- `force_close_symbol_via_positions()` now uses `args.live_timeout` instead of hardcoded 3.0
- `_rth_risk_exits()` passes `"--live-timeout", "8"` to PlaceAnOrder for longer polling at market open

**Impact:** Risk exit close orders get 8 seconds for live market data instead of 3, reducing failures at market open.

---

### Fix Y2: Negative Theo Values — IV Fallback + Clamp (Feb 12)
**Status:** ✓ IMPLEMENTED

**Location:** listener.py (_theo_spread_debits, IV assignment block)

**Issue:** AMT CLOSE signal had `iv_atm=None` (defaulted to 0.25) and `iv_otm=0.37`. With this IV mismatch, the OTM put at 170 (37% IV) was worth MORE than the ATM put at 175 (25% IV), producing negative put debit theo values: -0.8, -1.31, -1.65. MNST had similar issue (iv_atm=0.28 vs iv_otm=0.38 with 8 DTE).

Negative theo caused `_get_theo_limit()` in DailyCycleManagement to return `None` (check `if v > 0`), which triggered the MKT fallback at 5pm — placing an AMT MARKET order after hours.

**Fix — two changes:**
1. **IV fallback order:** When `iv_atm` is None/NaN, use `iv_otm` instead of hardcoded 0.25. Moved OTM IV parsing before ATM IV fallback.
2. **Clamp outputs:** `max(0.0, float(call_long - call_short))` — debit spread closing value cannot be negative.

**Impact:** All theo values are now >= 0. `_get_theo_limit()` returns valid limit prices for CLOSE signals. No more MKT fallback due to negative theos.

---

### Fix Y3: _place_combo — No MKT After Hours (Feb 12)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (_place_combo, line ~1313)

**Issue:** When `_get_theo_limit()` returned None (due to negative theos from Fix Y2), `_place_combo` fell back to MarketOrder for "previous day" positions after hours. This placed AMT MKT SELL at 5:05 PM.

**Fix:** Added market-hours check before allowing MKT fallback. If market is closed and no theo limit available, skip the order (return False) and defer to the after-hours batch placement which has a more robust fallback chain via `force_close_symbol_via_positions()`.

**Impact:** Safety net — even if theo values are somehow invalid, no MKT orders after hours from the reconcile path.

---

### Fix Y4: Risk Exits Diagnostic Logging (Feb 12)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (_process_vertical, _rth_risk_exits)

**Issue:** BK (CALL spread 130/135, stock at 115.12) was deeply OTM and nearly worthless but never appeared in the 9:39 AM risk exit attempts. The function returned silently at three filter points without any logging, making diagnosis impossible.

**Fix:**
1. Added LOG.debug for "too new" age filter (execution-based and DTE-estimated)
2. Added LOG.info for "entry <= 0.01" skip (likely BK's issue — avgCost calculation produced negligible entry)
3. Upgraded market data failure from LOG.debug to LOG.info with strike info
4. Added position scan summary at start: "scanning N symbols: SYM1, SYM2, ..."

**Impact:** Next time a symbol is silently filtered, the log shows exactly why. BK's root cause (likely avgCost-based entry = 0) will be visible.

---

### Fix Y5: Worthless Legs limit=0.0 Logging (Feb 12)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py (force_close_symbol_via_positions, worthless leg record_attempt calls)

**Issue:** When `use_market=True` at preclose, `record_attempt()` passed `limit=0.0` for MarketOrders on worthless legs. BSY and NLY showed `limit=0.0` in the attempts CSV, looking like a $0.00 limit order instead of a market order.

**Fix:** Changed `limit=0.0 if use_market else 0.05` to `limit=None if use_market else 0.05` (and same for 0.01 long leg). MKT orders now show empty limit field in attempts CSV.

**Impact:** Attempts CSV correctly distinguishes between limit orders ($0.01/$0.05) and market orders (no limit).

---

### Fix Y6: Mid-Day Risk Exit Retry at 10:30 AM (Feb 12)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (__main__ CLI), Windows Task Scheduler

**Issue:** Risk exits run once at 9:35 AM. If live pricing fails (common at market open), positions aren't retried until 3:00 PM preclose — a 5.5-hour gap. XP, PG, PYPL, SYY all sat unclosed from 9:40 AM to 3:00 PM.

**Fix:**
1. Added `--risk-exits-only` CLI flag that runs just `_rth_risk_exits()` with market-hours guard
2. Created Windows scheduled task `IB_RiskExits_Retry_1030` running daily at 10:30 AM

**Impact:** Positions that fail risk exit at 9:35 AM get a second attempt at 10:30 AM when market data is more reliable.

---

### Fix Y7: IB-DCM-Preclose Task Disabled (Feb 12)
**Status:** ✓ IMPLEMENTED

**Location:** Windows Task Scheduler

**Issue:** `IB-DCM-Preclose` had `Start In: N/A` and `Last Result: 2` (failed). It ran `python.exe DailyCycleManagement.py --preclose` without a working directory. However, `IB_ForceClose_MarketOrders_1500` (ForceMktClose.cmd) covers the same 3pm preclose and works (Last Result: 0).

**Fix:** Disabled the broken `IB-DCM-Preclose` task. `ForceMktClose.cmd` handles preclose.

**Impact:** Eliminates confusing duplicate task with different error states.

---

### NWG Investigation Note (Feb 12)

**Status:** MANUAL INVESTIGATION NEEDED

NWG has NO entries in any CSV or attempts file (Feb 11 or 12). The position exists but was not picked up by:
- 21-day reconcile (no CLOSE signal in any CSV within 21 days)
- Risk exits (either not detected as a vertical, or filtered out by age/entry/market-data)

**Action:** Check NWG position in TWS. If worthless, manually close or add to force-close list.

---

## Operational Issues

### Preclose Scheduler (Feb 4-5, 2026)

**Issue:** ~~3pm preclose cycle did not run on Feb 4 or Feb 5~~ **RESOLVED (Feb 6)**

The scheduled task WAS running correctly. The actual issue was Fix H above - force-close mode required CSV to exist.

**Evidence from Feb 5 logs:**
```
2026-02-05 15:00:56 Launching PlaceAnOrder: --mode force-close --symbols PCG --use-live-close mid
2026-02-05 15:01:00 [ERROR] Combined CSV not found: C:\OptionsHistory\26_02_05\combined_listener_spreads.csv
```

The preclose ran but PlaceAnOrder.py exited early before reaching the force-close logic.

**Resolution:** Fix H (above) makes force-close mode CSV-independent.

---

## Key Design Principles

### 1. Latest-Signal-Wins Policy
- When opposite-side signal received (CALL_OPEN when holding PUT), system closes existing position first
- Cancels working close orders to prevent conflicts
- Uses market orders for opposite-side unwinding (design decision)

### 2. Expiration Mismatch Handling
- CSV may have newer expirations than held positions (strategy rolled, positions lagging)
- System uses symbol-only CSV lookup (ignores expiration)
- Positions at any expiration can be closed using CSV pricing for current signal

### 3. Limit-First, Theo-Fallback
- CLOSE orders: Try `*_limit_*` columns first, fall back to `*_theo_*` columns
- OPEN orders: Same logic (after Fix D)
- 5% buffer applied to close limits (Fix X4), capped at spread width

### 4. Never Place Market Orders After-Hours
- Market is closed - fills are terrible
- Use limit orders with aggressive pricing (10% buffer)
- If limit doesn't fill, next day's preclose converts to market (during market hours with live pricing)

### 5. After-Hours vs Market-Open Pricing (Feb 7)

**After-Hours (5 PM):**
- Listener populates theo columns with Black-Scholes values; limit columns are None (Fix X5)
- PlaceAnOrder falls back to theo columns when limit is empty (Fix B)
- CSV populated with position-based strikes (Fix I for CLOSE signals)

**Market Open (9:35 AM):**
- LiquidityFilter updates previous day's CSV with live prices (Fix N)
- Uses actual position strikes from CSV (populated by Fix I)
- Prices capped at spread width (can't exceed $1 for $1 spread)

**Preclose (3 PM):**
- First tries live quotes (market is open)
- Falls back to previous day's CSV (has live prices from 9:35 AM via Fix N and Fix O)
- Last resort: MARKET order with `--allow-market-fallback` (Fix J)

---

## Critical Functions

### `force_close_symbol_via_positions()`
**Location:** PlaceAnOrder.py:1812-1930

**Purpose:** Close all spreads for a symbol using position scan + pricing fallbacks

**Key Features:**
- Scans positions by symbol (no expiration filter)
- Tries 2 pricing sources: live → previous day CSV (Fix O)
- Fix C: Skips if all pricing fails (no MARKET) for after-hours
- Fix J: Uses MARKET if `--allow-market-fallback` set (preclose only)
- Records detailed attempt reasons for diagnostics

### `close_any_spread_for_symbol()`
**Location:** PlaceAnOrder.py:1663-1762

**Purpose:** DEPRECATED - Close spreads using MARKET orders

**Issues:**
- Hardcoded `order_type="MKT"` at line 1755
- No limit pricing attempted
- Used by OPEN handlers for opposite-side unwinding (Fix G addresses this)

**Status:** Should NOT be used for any new code; being replaced by force_close_symbol_via_positions()

### `width_aligned_close_limit()`
**Location:** PlaceAnOrder.py:587-602

**Purpose:** Get limit price from CSV for CLOSE orders

**Logic:**
1. Determine width bucket: 1.0 → "1", 2.5 → "2_5", 5.0 → "5"
2. Try `{call|put}_debit_limit_{bucket}` column
3. Fall back to `{call|put}_debit_theo_{bucket}` column (Fix B logs this)
4. Return None if both fail

---

## CSV Structure

### combined_listener_spreads.csv Columns

**Signal Columns:**
- `symbol`: Stock ticker
- `signal_type`: CALL_OPEN, PUT_OPEN, CLOSE, CALL_CLOSE, PUT_CLOSE
- `strategy_position`: +1 (CALL_OPEN), -1 (PUT_OPEN), 0 (CLOSE)
- `expiration`: YYYYMMDD format
- `atm`: At-the-money strike price

**Pricing Columns (CLOSE):**
- `call_debit_limit`, `call_debit_limit_1`, `call_debit_limit_2_5`, `call_debit_limit_5`
- `put_debit_limit`, `put_debit_limit_1`, `put_debit_limit_2_5`, `put_debit_limit_5`
- `call_debit_theo`, `call_debit_theo_1`, `call_debit_theo_2_5`, `call_debit_theo_5`
- `put_debit_theo`, `put_debit_theo_1`, `put_debit_theo_2_5`, `put_debit_theo_5`

**Pricing Columns (OPEN):**
- Same structure as CLOSE
- Used for BUY debit spreads (opening positions)

**Width Buckets:**
- `_1`: $1 width spreads
- `_2_5`: $2.50 width spreads
- `_5`: $5 width spreads

---

## Testing & Verification

### After-Hours Cycle Verification (5:00 PM)

**Check logs for:**
1. `from_signal_exp_mismatch_defer_to_force_close` in attempts CSV (Fix F working)
2. `force_close,<exp>,SELL,LMT,1,success` (limit orders placed, not MARKET)
3. `no_viable_limit_all_fallbacks_failed` (Fix C working - skipping instead of MARKET)
4. `Using theo fallback for` in ib_cycle.log (Fix B logging, Fix D working for OPEN)

**Should NOT see:**
- `force_close,<exp>,SELL,MKT,1,success` from Stage 1 (Fix F prevents this)
- `opposite_unwind_before_open` with MKT orders (Fix G will prevent this)

### Preclose Verification (3:00 PM)

**Check logs for:**
1. `preclose_cancel_existing_close` in attempts CSV (cancelling unfilled limits)
2. New LMT orders placed with live pricing (market is open)
3. DailyCycleManagement session log exists for 3pm run

---

## Common Issues & Diagnostics

### Market Orders Still Being Placed

**Check:**
1. Which stage? Look at timestamp in attempts CSV
2. What reason? Check `reason` column in attempts CSV
3. From CLOSE handler? Should see `from_signal_exp_mismatch_defer_to_force_close` skip
4. From OPEN handler? May be opposite-side unwind (Fix G needed)

**Diagnostic Commands:**
```bash
# Check for market orders in attempts CSV
grep "MKT.*success" C:\OptionsHistory\26_XX_XX\attempts_26_XX_XX.csv

# Check for Fix F working (should see skips, not MKT)
grep "from_signal_exp_mismatch_defer_to_force_close" C:\OptionsHistory\26_XX_XX\attempts_26_XX_XX.csv

# Check for Fix C working (should see skips when pricing fails)
grep "no_viable_limit_all_fallbacks_failed" C:\OptionsHistory\26_XX_XX\attempts_26_XX_XX.csv
```

### Positions Not Closing

**Check:**
1. Preclose running? Check for DailyCycleManagement logs at 3pm
2. CSV expiration vs position expiration? May have mismatch
3. CSV pricing available? Check limit and theo columns in CSV
4. Working orders already exist? Check TWS or ib_close_guard

### Duplicate Orders

**Check:**
1. `ib_close_guard.has_working_auto_close()` called before placement?
2. Stage 2 running multiple times? Check DailyCycleManagement logic
3. Menu option 8 used? Guard added (Jan 26) should prevent duplicates

---

## Future Improvements

### 1. Implement Fix G (Priority: HIGH)
Replace `close_any_spread_for_symbol()` calls in OPEN handlers with limit-based close logic

### 2. Add Preclose Monitoring (Priority: MEDIUM)
Implement alerting when scheduled cycles don't run

### 3. Expiration Reconciliation (Priority: MEDIUM)
Add warnings when CSV expiration doesn't match position expiration

### 4. Automated Testing (Priority: LOW)
Create test cases for pricing fallback scenarios

---

## Contact & References

**Plan Files (Detailed Investigation Logs):**
- `C:\Users\Administrator\.claude\plans\splendid-chasing-kernighan.md` - Main investigation (Jan 23-27, Feb 4-5)
- `C:\Users\Administrator\.claude\plans\splendid-chasing-kernighan-jan29.md` - Fix D (OPEN theo fallback)
- `C:\Users\Administrator\.claude\plans\feb2-all-market-orders.md` - Feb 2-3 market orders investigation

**Key Files:**
- `C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader\PlaceAnOrder.py`
- `C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader\DailyCycleManagement.py`
- `C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader\LiquidityFilter.py` - Live price enrichment (Fix N)
- `C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader\listener.py` - Signal processing (Fix I, M)
- `C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader\ib_close_guard.py`

**Last Updated:** February 12, 2026 by Claude (Opus 4.6) - Added Fix U (cross-clientId order visibility), Fix V (width bucket), Fix W (duplicate CSV)
