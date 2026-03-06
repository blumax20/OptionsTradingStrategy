# Options Trading System - Architecture & Bug Fixes

## Overview

This document summarizes the architecture of the Interactive Brokers options trading system and the bug fixes implemented to prevent unwanted market orders.

**Last Updated:** March 6, 2026 (Fix BQ reverted — original bid-ask was correct; Fix BP: preclose always join; Fix BN-3/BO/BO2)

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

### Fix X2: Fixed-Price LimitOrders for Worthless Legs (Feb 11, corrected Feb 12)
**Status:** ✓ IMPLEMENTED (CORRECTED)

**Location:** PlaceAnOrder.py (force_close_symbol_via_positions, both_worthless leg placement)

**Issue:** Fix S placed individual leg orders at $0.01/$0.05 as DAY limit orders. Fix X2 originally changed preclose to use MarketOrder, but MarketOrders logged `limit=0.0` and risk bad fills on illiquid worthless options.

**Fix (corrected):** Always use LimitOrder with fixed pricing regardless of preclose or after-hours:
- **Long leg (SELL):** LimitOrder at $0.01
- **Short leg (BUY):** LimitOrder at $0.05
- Reason: `both_worthless_fixed_price` (unified, no more `both_worthless_market_preclose`)

**Impact:** Worthless legs always close with predictable fixed pricing. Attempts CSV logs correct limit values ($0.01/$0.05).

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

### Fix Y5: Worthless Legs — Always Use Fixed-Price LimitOrders (Feb 12, corrected)
**Status:** ✓ IMPLEMENTED (CORRECTED)

**Location:** PlaceAnOrder.py (force_close_symbol_via_positions, both_worthless leg placement)

**Issue:** Original Fix Y5 only changed logging from `limit=0.0` to `limit=None`. The actual order type at preclose was still MarketOrder (from Fix X2), which submits $0.00 and won't fill.

**Fix:** Removed the `use_market` branching entirely. Worthless legs always use LimitOrder:
- Long leg: `LimitOrder("SELL", qty, 0.01)` with `limit=0.01` logged
- Short leg: `LimitOrder("BUY", qty, 0.05)` with `limit=0.05` logged
- Unified reason: `both_worthless_fixed_price` (removed `both_worthless_market_preclose`)

**Impact:** Worthless legs always close with predictable $0.01/$0.05 pricing. Attempts CSV shows correct limit values.

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

### Fix Z1: NaN Guard in `_mid()` — False Stop-Loss Prevention (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py:2492-2504 (`_mid` function inside `_rth_risk_exits`)

**Issue:** On Feb 13 at 9:48 AM, `_rth_risk_exits()` submitted 15 CLOSE orders — ALL were false stop-loss triggers. IB returned Error 10091 (market data subscription required) for all option contracts. ib_insync set bid/ask/last to `float('nan')`. Python's NaN has dangerous comparison behavior:
- `nan is not None` → `True` (NaN is not None!)
- `max(0.0, nan - nan)` → `0.0` (Python NaN comparison quirk)
- Every symbol got `curr=0.00` → false stop-loss detected

**Root Cause:** `_mid()` returned `nan` (via `t.last`), which passed the `is not None` check. `max(0.0, ml - ms)` silently produced `0.0`.

**Fix:** Added `math.isnan()` guard to `_mid()`. NaN bid/ask/last values now return `None` instead of `nan`. The existing `curr is None` guard at line 2659 then correctly skips the symbol.

**Impact:** Risk exits no longer trigger false stop-losses when market data subscription fails. Symbols with Error 10091 are properly skipped with "no valid market data" log message.

---

### Fix Z2: TypeError Guard in Risk Exit Logging (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py:2713 (success logging in `_rth_risk_exits`)

**Issue:** After PlaceAnOrder placed an order, the success log line computed `(now - t0).days` where `t0` could be None (when both execution-based and DTE-based age calculations failed). This TypeError was caught but logged "failed to submit CLOSE" — misleading, since the order WAS actually placed.

**Fix:** Changed `(now - t0).days` to `f"{(now - t0).days}d" if t0 else "?"`.

**Impact:** No more TypeError exceptions masking successful order submissions. Log accurately shows "age ?" when age is unknown.

---

### Fix Z3: Remove Duplicate Attempts CSV Entries (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py:2160-2178 (force_close_symbol_via_positions)

**Issue:** Every successful force-close order produced TWO entries in the attempts CSV:
1. `place_debit_spread()` internally logs `record_attempt(reason="success")`
2. `force_close_symbol_via_positions()` logs another `record_attempt(reason="positions_fallback")`

Same order, same limit, same strikes — duplicate entries.

**Fix:** Removed the redundant `record_attempt` at lines 2163-2178. The `place_debit_spread()` "success" entry is sufficient. Kept `CLOSE_SEEN_KEYS.add()` and `submitted += 1` logic.

**Impact:** Each force-close order now appears exactly once in the attempts CSV.

---

### Fix Z4: Risk Exit Deduplication Guard (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (inside `_process_vertical()` in `_rth_risk_exits()`)

**Issue:** The 10:30 AM retry (Fix Y6) called `_rth_risk_exits()` fresh, re-evaluating all positions and placing new orders without checking if working close orders already existed from the 9:48 run. PBR, T, TSM, ZBH each got duplicate orders.

**Fix:** Before calling `_run_place_an_order()`, check for existing working close orders using the already-established risk-exit IB connection (clientId=878). Uses `ib.reqAllOpenOrders()` + `ib.openTrades()` to find PreSubmitted/Submitted orders for the symbol.

**Impact:** 10:30 retry skips symbols that already have working close orders from the 9:48 run.

---

### Fix Z5: TP/SL Reason in PlaceAnOrder Attempts CSV (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py (argparse, record_attempt, run_from_csv), DailyCycleManagement.py (_rth_risk_exits)

**Issue:** Risk exit close orders appeared in the attempts CSV as generic `force_close,placed,success` with no indication of WHY the exit was triggered (stop-loss vs take-profit).

**Fix:**
1. Added `--close-reason` CLI argument to PlaceAnOrder.py
2. DCM passes `--close-reason "STOP(>=50% loss)"` or `"TP(>=50% max profit)"` from risk exits
3. Added `close_reason` column to `ATTEMPT_FIELDS`
4. Module-level `_CLOSE_REASON` variable auto-populates all `record_attempt` calls
5. Set via `global _CLOSE_REASON` in `run_from_csv()` from args

**Impact:** Attempts CSV now shows the TP/SL trigger reason in the `close_reason` column for risk exit orders.

---

### Fix AA1: Block OPEN When Opposite-Side Unwind Fails (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py (CALL_OPEN path lines 2766-2784, PUT_OPEN path lines 2804-2822)

**Issue:** When a CALL_OPEN signal was received and the account held PUT spreads, the system tried to unwind the PUT first via `force_close_symbol_via_positions()`. But `allow_call = True` at line 2784 was **unconditional** — it was set even when the unwind returned 0 (no spreads closed) or threw an exception.

**Impact on Feb 13:**
- HRL: CALL spread not closed (no_viable_limit), but PUT_OPEN placed anyway → simultaneous CALL + PUT positions
- KEY: CALL partially closed (worthless_leg_fallback for one leg), but PUT_OPEN placed anyway

**Fix:** Moved `allow_call = True` (and `allow_put = True` for PUT_OPEN path) inside the `if n_unw > 0:` branch. When unwind returns 0 or throws, the OPEN is now skipped with `opposite_unwind_failed` or `opposite_unwind_exception` logged to attempts CSV.

**Impact:** No more simultaneous opposite-side positions when unwind fails. OPEN is blocked until opposite position is actually closed.

---

### Fix AA2: Remove Double-Logging for OPEN Orders (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py (CALL_OPEN handler lines 3173-3195, PUT_OPEN handler lines 3340-3375, fallback paths)

**Issue:** When an OPEN order succeeded, `place_debit_spread()` already logged a `record_attempt` internally (line 1213-1226 with full details: longK, shortK, order_type, limit, qty, order_action). The CALL_OPEN/PUT_OPEN handlers then logged a SECOND `record_attempt` with less detail.

**Fix:** Removed the redundant `record_attempt` calls in the CALL_OPEN and PUT_OPEN handlers. Kept only in dry-run paths (where `place_debit_spread()` is not called). The internal logging from `place_debit_spread()` has richer data and is sufficient.

**Impact:** Each OPEN order now appears exactly once in the attempts CSV.

---

### Fix AA3: Skip Expired Options in Reconcile Close (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (`_try_close_from_positions()`, line ~1260)

**Issue:** The 5 PM reconcile iterated through all positions and submitted close orders without checking if the options had already expired. HRL with exp=20260213 (Feb 13, today) got a combo close order at 5 PM — but those options expired at 4 PM. This "working close" order then blocked downstream stages from properly re-processing HRL.

**Fix:** Added `exp` field to each opt_leg dict entry, then filtered out legs where `exp <= today_str` (YYYYMMDD format). Expired legs are logged and skipped.

**Impact:** Reconcile won't try to close expired options that IB can't execute. These positions are automatically removed by IB's expiration processing.

---

### Fix AA4: Round Limit Prices to 2 Decimal Places (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py (`place_debit_spread()`, line 1183)

**Issue:** `LimitOrder(action.upper(), quantity, float(limit_price))` passed unrounded floats. Python floating-point arithmetic produced values like:
- KEY: `0.17099999999999999` (should be `0.17`)
- ADM: `1.311` (should be `1.31`)
- SYY: `2.2609999999999997` (should be `2.26`)

IB may reject or silently round these, or they may cause order status issues.

**Fix:** Changed to `round(float(limit_price), 2)` for both the LimitOrder constructor and `actual_limit`.

**Impact:** All limit orders submitted with clean 2-decimal-place prices.

---

### Fix AA5: Reconcile Force-Close Fallback (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (`_reconcile_positions_with_signals_lookback`, line ~2455)

**Issue:** When the reconcile detected a position that needed closing (flip scenario), it only used `_try_close_from_positions()` which places a direct combo LMT close. After hours, this fails when:
- No theo pricing in CSV for the symbol → `_get_theo_limit()` returns None
- Market is closed → MKT fallback blocked by Fix Y3
- Result: returns False, symbol NOT added to `_submitted_close_syms`, no downstream processing

This left positions like BSY (worthless CALL 40/45) and KEY (CALL 23/24) stuck indefinitely at 5 PM.

**Fix:** When `_try_close_from_positions()` returns False, fall back to `force_close_symbol_via_positions()` via PlaceAnOrder.py with `--fallback-individual-legs`. This fallback has:
- Worthless detection (both legs < $0.05 → fixed-price individual leg orders)
- CSV-based pricing from previous trading day
- Live pricing (if market open)
- `--force-close-side` to close only the relevant side

After the fallback, checks `_has_working_close_order()` to verify success. If successful, adds to `_submitted_close_syms` so downstream OPEN delegation can proceed (flip scenario).

**Impact:** Reconcile-detected flips now have a robust close path after hours, using all available pricing fallbacks including worthless detection.

---

### Fix AA6: Single-Leg Position Handling in Reconcile (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (_reconcile_positions_with_signals_lookback, lines 2288-2302)

**Issue:** After partial closes (one leg of a spread fills, one doesn't), symbols have 1 residual option leg. The reconcile's position scan at line 2219 (`if len(legs) >= 2:`) skips vertical detection for single-leg positions, leaving them with default values: `has_call_vert=False`, `has_put_vert=False`, `sign=None`. The flip logic at lines 2399-2407 then fails:
- `latest_open_sign == -1 and has_call_vert` → False
- `cur_sign is not None` → False (cur_sign=None)
- Falls through to "matches current orientation; holding" → WRONG

**Affected on Feb 13:** BSY (CALL 40/45, worthless, partially closed at 15:01) and KEY (CALL 23/24, partially closed at 17:08) — both had PUT_OPEN signals but reconcile skipped them.

**Fix:** Added `else` block after `if len(legs) >= 2:` for single-leg positions:
- Determines side from the single leg's right (C or P)
- Sets `has_call_vert=True` or `has_put_vert=True` accordingly
- Sets `sign` based on the leg's quantity direction

**Impact:** Single-leg orphans from partially-closed spreads now trigger proper flip detection and close orders.

---

### Fix AA7: Reconcile Diagnostic Logging (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (_reconcile_positions_with_signals_lookback)

**Issue:** When BSY/KEY were absent from the attempts CSV, diagnosis was impossible — no log of which symbols were found in positions, which had signals, or why they were skipped.

**Fix:** Added three LOG.info lines:
1. After position scan: "found N held symbol(s): SYM1, SYM2, ..."
2. After CSV signal loading: "matched signals for M/N held symbol(s) in 21d lookback: SYM1, SYM2, ..."
3. Unmatched symbols: "no signal found for: SYM3, SYM4"

**Impact:** Reconcile runs now show exactly which symbols were detected, matched, and processed.

---

### Fix AA8: Redirect DCM Stdout to Session Log (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** PushButtonMenu.ps1 (lines 384-406)

**Issue:** Menu option 8→3 declared a session log path (`DailyCycleManagement_session_*.log`) but Start-Process lacked `-RedirectStandardOutput`. DCM output went to console only; session log was never created.

**Fix:** Added `-RedirectStandardOutput $log -RedirectStandardError $logErr` to Start-Process. After completion, displays log content to user via Get-Content.

**Impact:** DCM session logs now persist for post-mortem analysis.

---

### Fix AA9: Unique ClientId for Reconcile (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (_reconcile_positions_with_signals_lookback, line 2191)

**Issue:** Reconcile used fixed `clientId=882`. If the scheduled 5PM task was still running (which also uses 882 via `daily_trading_cycle()`), the user's Menu 8→3 connection failed silently.

**Fix:** Randomized clientId: `882 + random.randint(0, 9)` (882-891). The reconcile disconnects after ~2 seconds, so the window for collision is minimal.

**Impact:** Manual reconcile runs no longer silently fail when concurrent with scheduled tasks.

---

### Fix AB1: Gate Individual-Leg MKT Fallback on Market Hours (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (`_try_close_from_positions()`, lines 1388-1410)

**Issue:** After Fixes AA6-AA9, the reconcile correctly detected BSY and KEY as `reconcile_flip_call_to_put`. But `_try_close_from_positions()` placed individual-leg **MarketOrders** after hours (18:50 ET) as a fallback when the combo LMT close failed. These MKT orders sat as PendingSubmit/Inactive and didn't fill. Since the function returned `True`, the Fix AA5 force-close fallback (`force_close_symbol_via_positions()` with worthless detection and fixed-price LimitOrders) was **never invoked**.

**Root Cause:** Lines 1388-1402 had an unconditional individual-leg MarketOrder fallback that fires whenever `_place_combo()` returns False, regardless of market hours. After hours, these MKT orders are useless but the function reports success.

**Fix:** Added market-hours check before the individual-leg MKT fallback. After hours, skips the MKT fallback and logs "deferring to force-close fallback", so the function returns `False`. This lets Fix AA5's `force_close_symbol_via_positions()` handle the close with proper pricing (worthless detection at $0.01/$0.05, CSV-based pricing, etc.). During market hours, individual-leg MKT orders still work as before.

**Impact:** Reconcile-detected flips after hours now reach the force-close fallback path, which handles worthless spreads correctly.

---

### Fix AB2: Allow Market Fallback in Reconcile Force-Close (Feb 13)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (`_reconcile_positions_with_signals_lookback`, line ~2503)

**Issue:** Fix AB1 correctly routes after-hours reconcile closes to the force-close fallback (Fix AA5), but the fallback call was missing `--allow-market-fallback`. When all limit pricing failed (live quotes unavailable after hours, no CSV pricing for the symbol), the order was skipped with `no_viable_limit_all_fallbacks_failed`. BSY (worthless CALL 40/45) hit this exact failure in the 20:37 session.

**Fix:** Added `"--allow-market-fallback"` to the `_run_place_an_order()` args in the reconcile force-close fallback. This allows MKT orders as a last resort when all limit pricing sources fail.

**Impact:** Reconcile force-close now has full fallback chain: live pricing → CSV pricing → worthless detection (fixed-price LMT) → MKT last resort.

---

### Fix AB3: Market Hours Check Must Include Weekday (Feb 15)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (`_place_combo` line ~1325, `_try_close_from_positions` line ~1396)

**Issue:** Fix AB1 gated individual-leg MKT orders on "market hours" but only checked time-of-day, not day-of-week. On Sunday Feb 15 at 12:57 PM, `hour > 9` and `hour < 16` → `market_open = True`. Individual-leg MKT orders were placed for BSY on a Sunday, `_try_close_from_positions()` returned True, and the force-close fallback (with worthless detection) was never reached.

**Fix:** Added `now_ny.weekday() < 5` (Mon-Fri only) to both `market_open` checks:
```python
market_open = (now_ny.weekday() < 5  # Fix AB3: Mon-Fri only
               and (now_ny.hour > 9 or (now_ny.hour == 9 and now_ny.minute >= 30))
               and now_ny.hour < 16)
```

**Impact:** Weekend runs correctly defer to force-close fallback with worthless detection and fixed-price LimitOrders.

---

### Fix AB4: outsideRth + Sleep for Individual Leg Orders (Feb 15)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py (`force_close_symbol_via_positions`, both-worthless path lines ~2335-2372, one-leg-worthless path lines ~2228-2312)

**Issue:** Worthless individual leg orders placed by PlaceAnOrder subprocess were not visible in TWS/portal because:
1. No `outsideRth=True` — orders may not be accepted/kept active outside regular trading hours
2. No `ib.sleep()` after `placeOrder()` — subprocess exited immediately, dropping connection before IB confirmed the orders

Compare with `place_debit_spread()` which has both `outsideRth=True` and `ib.sleep(0.3)` after placing.

**Fix:** Four changes across both individual-leg order paths:
1. **Both-worthless long leg:** Added `leg_order.outsideRth = True`
2. **Both-worthless short leg:** Added `leg_order.outsideRth = True`
3. **One-leg-worthless long leg:** Added `leg_order.outsideRth = True`
4. **One-leg-worthless short leg:** Added `leg_order.outsideRth = True`
5. **Both paths:** Added `ib.sleep(0.5)` after placing legs to let IB confirm before disconnect

**Impact:** Individual leg orders now persist in TWS/portal as pending orders even when placed outside regular trading hours.

---

### Fix AB5: BAG Combo MKT Orders for Worthless/Fallback Closes (Feb 15)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py (`_await_working`, `place_debit_spread`, `force_close_symbol_via_positions`)

**Issue:** After Fixes AB3+AB4, BSY/KEY/MNST close orders appeared as "placed" in the attempts CSV but none were visible in TWS/portal. Root cause: the worthless detection at lines 2134-2149 set `skip_combo = True`, which skipped the well-tested BAG combo path (`place_debit_spread()`) and instead placed individual OPT leg orders. These individual leg orders:
1. Lacked `_await_working()` — no confirmation IB accepted them
2. Used contracts from `ib.positions()` without building a proper BAG with resolved conIds
3. IB may not accept individual option orders on weekends the same way it accepts BAG combos

Additionally, the MKT order path in `place_debit_spread()` never set `outsideRth=True`, and `_await_working()` only accepted `Inactive+GTC` (not `Inactive+DAY`).

**Fix — six changes:**
1. **`_await_working()`:** Accept `Inactive+DAY` after hours (DAY orders with `outsideRth=True` go Inactive after hours but activate at market open)
2. **`place_debit_spread()` MKT paths:** Added `outsideRth=True` to both force_close MKT and legacy MKT MarketOrders
3. **Worthless detection:** When `order_type=="MKT"` (market fallback active), don't set `skip_combo=True` — let the BAG combo MKT order go through `place_debit_spread()` which resolves conIds, builds BAG, and calls `_await_working()`
4. **Individual leg fallback:** Added `_await_working()` after every `ib.placeOrder()` in both one-leg-worthless and both-worthless individual leg paths
5. **Failure reason:** Differentiated `place_failed_worthless_combo` from generic `place_failed_positions`
6. **Combo-failure fallback:** When BAG combo MKT is attempted for worthless spread but fails (contract qualification, close guard), fall back to individual fixed-price legs with `_await_working()` confirmation

**Impact:** Worthless/fallback closes now use the standard BAG combo approach (same as `place_debit_spread()`), which properly resolves conIds, builds a BAG contract, and confirms order acceptance. Individual legs remain as a safety net with proper order confirmation.

---

### Fix AB6: One-Leg-Worthless Combo + Close Guard + Exchange Fix (Feb 15)
**Status:** ✓ IMPLEMENTED

**Location:** PlaceAnOrder.py (worthless detection, individual leg paths), ib_close_guard.py

**Issue:** After Fix AB5, BSY (both-worthless + MKT) successfully placed a BAG combo visible in TWS. But KEY and MNST still failed with `place_failed_positions` + `worthless_leg_fallback`. Three compounding root causes:

1. **AB6a — One-leg-worthless skipped combo for LMT:** Fix AB5 only bypassed `skip_combo` when `order_type=="MKT"`. KEY got LMT ($0.2755 from CSV) and MNST got LMT ($0.845), so `skip_combo=True` → individual leg path used instead of BAG combo.

2. **AB6b — Close guard false positive:** `ib_close_guard.py` treated both BUY and SELL BAG orders as "close-related". KEY had an existing PUT OPEN order (BUY BAG). The guard found this BUY BAG and returned True, blocking KEY's CALL close (SELL BAG).

3. **AB6c — Error 321 "Missing order exchange":** Contracts from `ib.positions()` don't include `exchange` field. Individual leg orders using these contracts directly were rejected by IB with Error 321.

4. **AB6d — Floating-point limit prices:** Force-close limit prices had floating-point artifacts (e.g., `0.27549999999999997` instead of `0.28`).

**Fix — four changes:**
1. **AB6a:** Always try BAG combo first for one-leg-worthless, regardless of order_type (LMT or MKT). BAG combo resolves conIds via `qualifyContracts()` and handles pricing correctly.
2. **AB6b:** Changed `ib_close_guard.py` to only match SELL BAG orders as close orders. BUY BAG = OPEN order, should not block close placement. Also added Inactive+DAY recognition (matching DCM's `_has_working_close_order` behavior).
3. **AB6c:** Added `if not getattr(c, 'exchange', ''): c.exchange = "SMART"` before every individual leg `ib.placeOrder()` call (6 locations: both-worthless combo-failure fallback, one-leg-worthless, both-worthless fixed-pricing).
4. **AB6d:** Added `limit = round(limit, 2)` in `force_close_symbol_via_positions()` when order_type is LMT.

**Impact:**
- KEY: Close guard no longer falsely blocks (PUT OPEN is BUY BAG, ignored). One-leg-worthless with LMT → BAG combo placed.
- MNST: One-leg-worthless with LMT → BAG combo placed.
- Individual leg fallback (safety net) no longer hits Error 321.
- All force-close limit prices have clean 2-decimal-place values.

---

### Fix AB7: Position-Filter Sunday Close Sweep (Feb 15)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (`_delegate_close_from_csvs_within`, line ~879)

**Issue:** `_delegate_close_from_csvs_within(days=21)` is CSV-driven: it picks every symbol with a CLOSE signal in the 21-day window (~60 symbols) and processes all of them through 3 stages (Stage 1 from-signal, Stage 1.5 live-mid, Stage 2 CSV fallback). Only ~4 symbols had actual held positions. The other ~56 generated ~180 useless "skipped" entries in the attempts CSV (`from_signal_exp_mismatch_defer_to_force_close` and `no_spread_in_positions`).

Note: The reconcile function (`_reconcile_positions_with_signals_lookback`) was already position-based. Only `_delegate_close_from_csvs_within` had this issue.

**Fix:** After building the `pick` list from CSV CLOSE signals, connect to IB (clientId=885), scan positions for held option symbols, and filter `pick` to only include symbols with actual positions. Falls back to unfiltered behavior if the position scan fails.

**Impact:**
- Sunday close sweep processes only held symbols (~4) instead of all CLOSE signals (~60)
- Attempts CSV reduced from ~260 entries to ~12
- No functional change — all held positions still get their close orders

---

### Fix AB8: IB Watchdog — Auto-Restart During Trading Hours (Feb 17)
**Status:** ✓ IMPLEMENTED

**Location:** `C:\OptionsHistory\bin\IB_Watchdog.ps1`, Windows Task Scheduler (`IB_Watchdog_Every15Min`)

**Issue:** On Feb 17, no orders were placed all day because IB Gateway dropped its connection (`WinError 64`) and no automated recovery existed. The system had health checks (Health.ps1 at 7:15, 8:30, 12:00) but these were **read-only diagnostics** — they detected problems but never restarted anything. The only restart scripts were:
- `StartListener.cmd` (6 AM) — fast-path bail-out if service already RUNNING (even with broken IB connection)
- `RestartListener.cmd` (2:30 PM) — only restarts listener, NOT IBGateway

Result: 14-hour gap (6 AM to 8 PM) with zero auto-recovery.

**Fix:** Created `IB_Watchdog.ps1` PowerShell script that:
1. Checks port 7497 is LISTENING (IB Gateway alive)
2. Checks `/health` returns HTTP 200 (Listener alive)
3. If either fails, calls `BounceServices.cmd` to restart all services (CloudflareTunnel + IBGateway + OptionsListener)
4. 10-minute cooldown file (`watchdog_last_restart.txt`) prevents restart loops
5. Post-restart verification: re-checks `/health` and logs result

**Scheduled Task:** `IB_Watchdog_Every15Min` — runs every 15 minutes, Mon-Fri, 6:00 AM to 8:00 PM.

**Also fixed:** Re-created missing `IB_RiskExits_Retry_1030` scheduled task (Fix Y6 — was never in the task list).

**Impact:** If IB Gateway or listener goes down during trading hours, auto-recovery within 15 minutes instead of requiring manual intervention.

---

### Fix AB9: Put Vertical Sign Detection + ClientId Hardening (Feb 18)
**Status:** ✓ IMPLEMENTED

**Location:** DailyCycleManagement.py (lines ~2323, ~2330, ~1221, ~2549), PlaceAnOrder.py (argparse, lines ~2515, ~2615)

**Issue:** The 21-day reconcile (`_reconcile_positions_with_signals_lookback`) falsely detected MNST and TCOM as "reconcile_mismatch" on Feb 17 and Feb 18. Both were correctly-oriented PUT debit spreads matching their PUT_OPEN signals, but got unnecessary MKT close orders at 5 PM.

**Root Cause (AB9a):** `_detect_vertical()` uses notional comparison: for a PUT debit spread (long higher put, short lower put), the long leg has higher avgCost → `long_notional > short_notional` → `put_sign=+1`. But PUT_OPEN signal convention is `-1`. The code set `sign = put_sign` directly without negating.

**Fix AB9a — Negate put sign (PRIMARY):**
```python
# Line ~2323 (put-only vertical):
# BEFORE: sign = put_sign
# AFTER:
sign = -put_sign if put_sign is not None else None

# Line ~2330 (mixed case, put dominates):
# BEFORE: sign = -1
# AFTER:
sign = -put_sign if put_sign is not None else None
```

Logic: PUT debit spread (long higher put) → `put_sign=+1` → negated to `-1` (matches PUT_OPEN). Short put credit spread → `put_sign=-1` → negated to `+1` (bullish).

**Fix AB9b — Widen clientId range:**
Changed `_try_close_from_positions()` clientId from `880 + random.randint(0, 99)` to `900 + random.randint(0, 99)`. Avoids overlap with DCM functions at 882-891 and close guard at 884.

**Fix AB9c — Add `--client-id` to PlaceAnOrder.py:**
Added `--client-id` CLI argument (default 101). Both `ib.connect()` sites (force-close at line ~2515 and from-signal at line ~2615) now use `args.client_id`. DCM reconcile force-close passes `--client-id 102` to avoid collision with other PlaceAnOrder instances.

**Impact:**
- PUT debit spreads matching PUT_OPEN signals are correctly recognized as "matches current orientation; holding"
- No more false MKT close orders for correctly-oriented PUT positions
- ClientId collisions between DCM in-process connections and PlaceAnOrder subprocesses eliminated

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

**Last Updated:** February 20, 2026 by Claude (Sonnet 4.6) - Added Fix AD/AD2/AE: enrichment at 10:30 AM, 9:45 AM rescheduling, worthless leg fix

---

### Fix AD: Add CSV Enrichment to 10:30 AM Risk Exits Retry (Feb 18)
**Status:** ✓ IMPLEMENTED

**Location:** `DailyCycleManagement.py` (`--risk-exits-only` path, line ~3518)

**Issue:** At 9:45 AM, `enrich_live_spread_prices: updated=0` because IB option market data feeds haven't initialized (Error 10091 at market open). The previous day's CSV has no live prices for 3 PM preclose to use as fallback. The 10:30 AM retry only called `_rth_risk_exits()` — it didn't re-run enrichment.

**Fix:** Added `_enrich_today_and_prev_trading_day(only_rth=True)` before `_rth_risk_exits()` in the `--risk-exits-only` path. By 10:30 AM, option market data is reliably available, so live prices populate correctly.

**Impact:** Previous day's CSV gets live prices at 10:30 AM. 3 PM preclose has reliable CSV fallback pricing. Risk exits also run with better market data.

---

### Fix AD2: Move IB_Open_PlaceMissing_0935 to 9:45 AM (Feb 18)
**Status:** ✓ IMPLEMENTED

**Location:** Windows Task Scheduler (`IB_Open_PlaceMissing_0935`)

**Issue:** At 9:35 AM (5 min after open), IB option market data feeds haven't initialized. Risk exits get Error 10091, CSV live enrichment gets `updated=0`. Moving to 9:45 AM gives 15 minutes for market data to stabilize.

**Fix:** Changed task trigger from `09:35:00` to `09:45:00`. The 10:30 AM retry (`IB_RiskExits_Retry_1030`) remains as the safety net.

---

### Fix AE: Always Use Fixed-Price Individual Legs for Worthless Spreads (Feb 18)
**Status:** ✓ IMPLEMENTED

**Location:** `PlaceAnOrder.py` (`force_close_symbol_via_positions`, lines ~2143-2160)

**Issue:** When both legs are worthless (both < worthless_threshold) AND `order_type=="MKT"` (because `--allow-market-fallback` is set and all limit pricing failed), Fix AB5 set `skip_combo=False` → routed to BAG combo MKT. The individual fixed-price leg path ($0.01/$0.05) was never reached. Reason in attempts CSV was always `market_fallback_preclose`, never `both_worthless_fixed_price`.

**Root Cause of User Confusion:** The individual leg approach should be PRIMARY (deterministic, known price, fills at market open), with MKT only as last resort. The code had it backwards: MKT first, individual legs only if combo failed.

**Fix:** Removed the `if order_type.upper() == "MKT"` branch. `both_worthless` now always sets `skip_combo=True`, routing directly to fixed-price individual legs regardless of `order_type`:

```python
# BEFORE: MKT bypassed individual legs
if order_type.upper() == "MKT":
    skip_combo = False  # → BAG combo MKT → market_fallback_preclose
else:
    skip_combo = True   # → $0.01/$0.05 individual legs

# AFTER: Always individual legs for worthless spreads
skip_combo = True  # → $0.01/$0.05 individual legs always
```

**Impact:**
- `both_worthless_fixed_price` now appears in attempts CSV for truly worthless spreads
- Fixed-price orders ($0.01 SELL long, $0.05 BUY short) fill immediately during market hours and at next open if placed after-hours
- BAG combo MKT path for worthless spreads eliminated (it was unreliable for near-$0 spread values)

---

### Fix AC0: PUT Spread Leg Assignment in Risk Exits (Feb 18)
**Status:** ✓ IMPLEMENTED

**Location:** `DailyCycleManagement.py` line ~2884 (`_rth_risk_exits`)

**Issue:** MNST PUT debit spread (long 77.5P, short 75P) got `entry=0.0000` and was skipped by risk exits every morning. avgCost showed: long=73.95, short=156.05 — reversed.

**Root Cause:** In `_rth_risk_exits()`, the PUT debit loop finds pairs where `s1 > s2`, `l1.qty > 0` (long at s1=higher), `l2.qty < 0` (short at s2=lower). Then calls:
```python
# BEFORE (WRONG):
_process_vertical(s2, l2, s1, l1, "PUT")  # l2=short passed as long_leg!
# long_entry = 73.95/100 = 0.74, short_entry = 156.05/100 = 1.56
# entry = max(0, 0.74 - 1.56) = 0.0 → skipped every time
```

**Fix:**
```python
# AFTER (CORRECT):
# Fix AC0: PUT debit — long=l1 (higher strike s1), short=l2 (lower strike s2)
_process_vertical(s2, l1, s1, l2, "PUT")
# long_entry = 156.05/100 = 1.56, short_entry = 73.95/100 = 0.74
# entry = max(0, 1.56 - 0.74) = 0.821 ✓
```

**Impact:** PUT debit spreads (MNST, TCOM, any PUT position) now correctly compute entry price from avgCost. Stop-loss and take-profit thresholds can be properly evaluated.

---

### Fix AC1: Health.ps1 — Add Missing Tasks to Monitoring (Feb 18)
**Status:** ✓ IMPLEMENTED

**Location:** `C:\OptionsHistory\bin\Health.ps1` (lines 76-92)

**Issue:** `IB_RiskExits_Retry_1030` and `IB_Watchdog_Every15Min` were not in the `$wanted` array, so Health reports never showed their LastRun/LastResult.

**Fix:** Added both to `$wanted`. Also added `IB_RiskExits_Retry_1030` to `$expectDaily` since it invokes DCM.

**Impact:** Health reports now show status of 10 tasks instead of 8. Missing or failing risk-exit retry and watchdog tasks will be visible.

---

### Fix AC2: Create RiskExitsRetry.cmd (Feb 18)
**Status:** ✓ IMPLEMENTED

**Location:** `C:\OptionsHistory\bin\RiskExitsRetry.cmd` (NEW FILE)

**Issue:** `IB_RiskExits_Retry_1030` task called Python directly, so there was no `==== [RiskExitsRetry ...] ====` header in `ib_cycle.log` — impossible to confirm via log that the 10:30 AM retry ran.

**Fix:** Created `RiskExitsRetry.cmd` matching the pattern of `PlaceOpen.cmd`:
```cmd
>>"%LOG%" echo ==== [RiskExitsRetry %DATE% %TIME%] ====
"%PY%" ".\DailyCycleManagement.py" --risk-exits-only >>"%LOG%" 2>&1
```

**Impact:** 10:30 AM risk exit retry output now appears in ib_cycle.log with a recognizable header, consistent with all other scheduled tasks.

---

### Fix AC3: Update IB_RiskExits_Retry_1030 Task to Use .cmd (Feb 18)
**Status:** ✓ IMPLEMENTED

**Location:** Windows Task Scheduler (`IB_RiskExits_Retry_1030`)

**Issue:** Task ran `python.exe DailyCycleManagement.py --risk-exits-only --verbose` directly — bypassing the .cmd wrapper and not logging to ib_cycle.log.

**Fix:** Updated task action to:
- Execute: `cmd.exe`
- Arguments: `/c "C:\OptionsHistory\bin\RiskExitsRetry.cmd"`
- WorkingDirectory: `C:\OptionsHistory\bin`

**Impact:** Task output now goes to ib_cycle.log. Health.ps1 can detect it via log parsing. Consistent with PlaceOpen.cmd / ForceMktClose.cmd pattern.

---

### Fix AF1: CSV ATM Strike Validation — Discard Wrong-Strike Pricing (Feb 19)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/PlaceAnOrder.py` (`force_close_symbol_via_positions`, CSV fallback block ~line 1948)

**Issue:** BK CALL 130/135 got a force-close LMT SELL at **$2.48** when the market bid was ~$0.32 (order will not fill). Root cause: the Feb 18 CSV row for BK was generated by a PUT_OPEN signal — Fix I only applies to CLOSE signals, so the listener computed call pricing based on the current ATM spread (120/125), not the held position's strikes (130/135). `call_debit_limit_5 = 2.75` → after double 5% buffer = **$2.48**. The detection key: `row['atm_strike'] = 120` vs `longK = 130` (diff = 10 > 1.5 × width 5 = 7.5).

**Fix:** After reading the CSV row, compare `row['atm_strike']` to position `longK`. If `|atm_strike − longK| > 1.5 × width`, discard the CSV pricing with a warning (but still cache the row for Fix AF2's theo check):

```python
_csv_row_for_symbol = row  # cache for AF2 theo check regardless of validation
_row_atm = pd.to_numeric(row.get("atm_strike"), errors="coerce")
if pd.notna(_row_atm) and longK is not None:
    _atm_diff = abs(float(_row_atm) - float(longK))
    if _atm_diff > 1.5 * width:
        logger.warning(f"[{symbol}] CSV atm_strike={_row_atm} vs position longK={longK} ...")
        continue  # skip this CSV source
```

Also added `_csv_row_for_symbol = None` initialization before the CSV fallback loop.

**Impact:**
- BK: `|120 − 130| = 10 > 7.5` → CSV discarded → `no_viable_limit_all_fallbacks_failed` (no $2.48 order). Next day 3 PM preclose with live quotes closes correctly.
- Normal case (e.g., CTVA 75/80 with atm_strike=75): `|75 − 75| = 0 ≤ 7.5` → pricing kept.

---

### Fix AF2: CSV Theo as Worthless Proxy — Enable Worthless Close When limit=None (Feb 19)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/PlaceAnOrder.py` (`force_close_symbol_via_positions`, `if limit is None:` block ~line 2004; override block ~line 2195)

**Issue:** NWG CALL 20/22.5 (deeply OTM, stock well below $20) had `call_debit_theo_2_5 = 0.01` in the Feb 18 CSV. After 2× 5% buffer: `0.01 × 0.95 × 0.95 = $0.009 < min_limit ($0.05)` → limit discarded. The `if limit is None: continue` gate fires BEFORE the `both_worthless` worthless-detection block (initialized at line ~2076). NWG gets `force_close,skipped,no_viable_limit_all_fallbacks_failed`, then `open_put,skipped,opposite_unwind_failed` — PUT_OPEN blocked.

**Root Cause:** The worthless detection (`if use_fallback:` block) is only reachable after the `if limit is None:` gate. But the gate fires `continue` before the `skip_combo/both_worthless/use_fallback` variables are initialized.

**Fix:** Inside the `if limit is None:` block, check the cached CSV row's theo value before the MKT/skip branch. If theo ≤ 0.05, set `_af2_theo_worthless = True` and fall through (no `continue`). Apply override after the `if use_fallback:` block (AFTER lines that re-initialize `both_worthless`):

```python
_af2_theo_worthless = False  # initialized before CSV block
if limit is None:
    # Fix AF2: Check CSV theo as worthless proxy
    if _csv_row_for_symbol is not None:
        _wb = _width_bucket(width)
        _theo_col = f"{'call' if right.upper() == 'C' else 'put'}_debit_theo_{_wb}"
        _theo_val = pd.to_numeric(_csv_row_for_symbol.get(_theo_col), errors="coerce")
        if pd.notna(_theo_val) and float(_theo_val) <= 0.05:
            _af2_theo_worthless = True
    if not _af2_theo_worthless:
        if getattr(args, "allow_market_fallback", False):
            order_type = "MKT"  # preclose: MKT ok
            ...
        else:
            record_attempt(..., "no_viable_limit_all_fallbacks_failed")
            continue
    # If _af2_theo_worthless: fall through, override applied after use_fallback block below

# After if use_fallback: block:
if _af2_theo_worthless:
    both_worthless = True
    skip_combo = True
    use_fallback = True
```

**Impact:**
- NWG: `call_debit_theo_2_5 = 0.01 ≤ 0.05` → `both_worthless=True, skip_combo=True, use_fallback=True` → routes to fixed-price individual leg close (`close_individual_leg,placed,both_worthless_fixed_price`) → `open_put` opposite-unwind now succeeds.
- BK (after AF1 discards CSV pricing): `call_debit_theo_5 = 1.97 > 0.05` → NOT worthless → falls through to `no_viable_limit_all_fallbacks_failed`. Correct — BK at ~$0.32 is not worthless.
- Normal spreads (e.g., CTVA theo ≫ 0.05): AF2 does not trigger.

---

### Fix AF3: SELL Long Worthless Leg at $0.05 (Exchange Minimum Tick) (Feb 19)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/PlaceAnOrder.py` (`force_close_symbol_via_positions`, both-worthless long leg placement ~line 2461)

**Issue:** The `both_worthless` individual-leg path placed the SELL for the long leg at `LimitOrder("SELL", qty, 0.01)`. Most US equity options (non-penny-pilot) have a minimum price variation of **$0.05**. IB silently rejects a $0.01 SELL limit order — it never appears in TWS as pending, but `record_attempt()` still logs "placed". Result: the long position remains open indefinitely with no working close order.

Evidence:
- BSY Feb 19: SELL 40C at $0.01 logged "placed" but not visible in TWS; long leg position still held
- NWG Feb 20: SELL 20C at $0.01 logged "placed"; long leg still held; only BUY 22.5C ($0.05) appeared in TWS

**Fix:** Change SELL long leg from $0.01 → $0.05:

```python
# BEFORE:
leg_order = LimitOrder("SELL", qty, 0.01)
record_attempt(..., limit=0.01, ...)

# AFTER (Fix AF3):
leg_order = LimitOrder("SELL", qty, 0.05)
record_attempt(..., limit=0.05, ...)
```

Also improved `record_attempt` status: `"placed" if ok else "error"` (was always `"placed"` regardless of `_await_working()` result).

**Impact:**
- SELL long leg at $0.05 conforms to minimum tick → accepted by IB → appears as pending in TWS
- Net cost to close both worthless legs: SELL long @$0.05 + BUY short @$0.05 = $0 net (break even)
- If no buyer at $0.05, order remains pending; long position costs nothing to hold (worth ~$0, expires at expiration)
- `_await_working()` result now reflected in attempts CSV status (`error` if IB rejects)

---

### Fix AG1: Portfolio Price Fallback for Risk Exit TP/SL Detection (Feb 20)
**Status:** ✓ IMPLEMENTED

**Location:** `DailyCycleManagement.py` (`_rth_risk_exits()`, lines ~2668 and ~2796)

**Issue:** `_rth_risk_exits()` uses `reqMktData()` exclusively to get current leg prices for TP/SL evaluation. When IB returns Error 354 ("Requested market data is not subscribed") or Error 10091, `_mid()` returns `None` for both legs and every position is skipped with "no valid market data". CP CALL 80/82.5 (March 20 exp) had a spread value of $1.94 exceeding the TP threshold of $1.76 but was skipped every morning.

**Root Cause (investigation Feb 20):** Paper trading account was not configured to share live market data from the live account — required explicit enablement in TWS (Account → Paper Trading → Use live market data). Additionally, `reqMktData()` with no `genericTickList` requests OPRA real-time bid/ask/last which may differ from OI/IV subscription tier used by the listener (`'101,106'`).

**Fix — two changes in `_rth_risk_exits()`:**

1. Build portfolio price lookup after position scan:
```python
# Fix AG1: Build portfolio price lookup as fallback when reqMktData fails (Error 354).
# ib.portfolio() prices come from TWS's account update stream — no market data subscription required.
port_prices: dict[int, float] = {}
try:
    for _pi in ib.portfolio():
        _mp = _pi.marketPrice
        if _mp and not _math_ag1.isnan(_mp) and _mp > 0:
            port_prices[_pi.contract.conId] = _mp
    LOG.info("Risk exits: portfolio price lookup: %d entries", len(port_prices))
except Exception as _ag1_err:
    LOG.warning("Risk exits: portfolio price lookup failed: %s", _ag1_err)
```

2. In `_process_vertical()`, fallback to portfolio prices after reqMktData polling:
```python
# Fix AG1: fallback to portfolio market prices if reqMktData returns None
if ml is None:
    ml = port_prices.get(long_leg["conId"])
if ms is None:
    ms = port_prices.get(short_leg["conId"])
```

**Impact:**
- TP/SL detection now works even when `reqMktData()` fails (Error 354, Error 10091, timeout)
- Portfolio prices come from TWS's account update stream — always available when connected, no separate subscription required
- Both stop-loss and take-profit use the same `curr = max(0.0, ml - ms)`, so both are fixed by this change
- CP CALL 80/82.5: portfolio long=$5.84, short=$3.90, curr=$1.94 > TP threshold $1.76 → TP triggers

---

### Fix AH1+AH2: BAG Cancel + Individual Leg Dedup Before Worthless Close (Feb 21)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/PlaceAnOrder.py` (`force_close_symbol_via_positions`, `both_worthless` block ~line 2481 and `one_leg_worthless` block ~line 2338)

**Issue:** NWG CALL 20/22.5 (both worthless) accumulated 3× BUY 22.5C at $0.05 with no SELL 20C. Two compounding problems:

1. **Dedup gap (AH2):** The `both_worthless` individual leg block has no cross-run dedup guard. `ib_close_guard.has_working_auto_close()` only detects SELL BAG orders, not individual OPT orders. Each 9:45 AM and 10:30 AM re-run placed a fresh BUY without checking if one already existed.

2. **BAG conflict (AH1):** The 5 PM reconcile placed a BAG SELL combo order (`outsideRth=True, tif=DAY`) that remained Inactive over the weekend and activated Monday. IB rejects an individual SELL 20C when a BAG already contains that leg as a SELL. Individual BUY 22.5C succeeded (not conflict-blocked), but SELL 20C was silently rejected.

**Fix AH1:** Before placing individual legs (in both `both_worthless` and `one_leg_worthless` blocks), cancel any Presubmitted/Submitted/Inactive BAG combo orders for the symbol. Uses `ib.reqAllOpenOrders()` + `ib.sleep(0.4)` pattern (same as `_has_working_close_order()`). Allows 0.4s for cancellation to propagate.

**Fix AH2:** After AH1, build a set of already-working individual OPT orders: `_working_sells_ah` (strikes with working SELL) and `_working_buys_ah` (strikes with working BUY). Before placing each leg, check if a working order already exists for that strike+action — skip with `AH2: skipping` log if so.

Applied in **both** `both_worthless` and `one_leg_worthless` blocks.

**Impact:**
- AH1: BAG cleared before individual placement → IB accepts individual SELL for legs previously blocked by BAG conflict
- AH2: On 2nd/3rd run (9:45 AM retry, 10:30 AM retry), already-working individual legs are skipped → no more 3× BUY duplicates
- Attempts CSV: `"AH2: SELL 20.0 individual leg already working; skipping"` visible in log on subsequent runs

---

### Fix AI1: Portfolio-Based Limit Price for Risk Exit Force-Closes (Feb 24)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_rth_risk_exits()`, PlaceAnOrder args ~line 2886); `InteractiveBrokersTrader/PlaceAnOrder.py` (`force_close_symbol_via_positions()`, inside `use_fallback` block ~line 2192)

**Issue:** Risk exits correctly detect TP/SL for CP, YUM, PFE, D, T, UNM via portfolio prices (Fix AG1) but then fail with `no_viable_limit_all_fallbacks_failed` when delegating to PlaceAnOrder. Root cause: `live_spread_price()` join scheme requires `L_bid > 0` for the long option being sold — but deeply OTM options have bid=0 structurally even with full market data subscriptions at 10:30 AM. This is NOT a timing issue. CSV fallback also fails (no valid listener row for these symbols). `use_fallback` block (portfolio price fallback) was never activated because `--fallback-individual-legs` wasn't passed.

**Fix — two changes:**
1. Add `"--fallback-individual-legs"` to `_run_place_an_order()` args in `_rth_risk_exits()` — enables the `use_fallback` block in PlaceAnOrder which fetches portfolio prices via `ib.portfolio()`
2. Inside `use_fallback` block in `force_close_symbol_via_positions()`, after worthless determination, add AI1 limit computation: when `limit is None`, both legs have portfolio prices, and spread is NOT worthless: `limit = round(max(0, long_value - short_value) * 0.95, 2)`

**Impact:**
- CP: long≈5.84, short≈3.90 → limit=$1.84
- YUM: long≈8.37, short≈4.84 → limit=$3.35
- PFE: similar — closes before Friday expiry
- OTM options with bid=0 no longer block risk exit pricing
- Log shows `"AI1: portfolio-based limit for C 80.0/82.5: limit=1.84"` when triggered

---

### Fix AI2: Respect `_submitted_close_syms` in Delegate Close Stages (Feb 24)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_delegate_close_from_csvs_within()`, Stage 1/1.5/2 pre-checks ~lines 916, 930, 969, 997)

**Issue:** YUM received two close orders at 5 PM on Feb 23: one from Stage 1 ($3.11) and one from reconcile ($2.80). Reconcile places orders synchronously BEFORE `_delegate_close_from_csvs_within()` and adds YUM to `_submitted_close_syms`. But Stage 1 only checks `_has_working_close_order()` (fresh IB connection, 0.5s sleep) — IB cross-clientId order propagation takes 1-5s, so the reconcile's just-placed order isn't visible yet. Stage 1 sees no working order and places a duplicate.

**Fix:** Compute `_submitted_syms = getattr(self, "_submitted_close_syms", set())` once at the top of `_delegate_close_from_csvs_within()`. Use it in all three stage pre-checks alongside `_has_working_close_order()`:
- Stage 1: `if s in _submitted_syms or self._has_working_close_order(s):`
- Stage 1.5: `if not (s in _submitted_syms or self._has_working_close_order(s)):`
- Stage 2: `if not (s in _submitted_syms or self._has_working_close_order(s)):`

**Impact:** `_submitted_close_syms` is populated synchronously — no IB connection or timing dependency. YUM-style race condition eliminated.

---

### Fix AI3: Fix False `place_failed_worthless_combo` Error Log (Feb 24)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/PlaceAnOrder.py` (`force_close_symbol_via_positions()`, ~line 2242)

**Issue:** NWG attempts CSV showed `force_close,error,place_failed_worthless_combo` as a false positive. When `skip_combo=True` (both legs worthless, individual-leg path used), the combo is intentionally not attempted → `tr = None`. The `else:` block at line 2223 fired unconditionally when `tr is None`, logging an error even though the combo was never tried.

**Fix:** Changed `else:` to `elif not skip_combo:` so the error log only fires when the combo was actually attempted (and failed).

**Impact:** NWG attempts CSV path: `market_fallback_preclose` → directly `both_worthless_fixed_price` (no false `place_failed_worthless_combo` error in between).

---

### Fix AJ1: Skip 7-Day Enforce-Recent-Closes on Sundays (Feb ~24)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`daily_trading_cycle`, ~line 3344)

**Issue:** On Sundays, `daily_trading_cycle()` called `_enforce_recent_closes(days=7)` followed by `_after_hours_batch_placement()`. The 7-day enforce is a subset of the 21-day sweep inside `_after_hours_batch_placement`, so every held symbol was processed twice through all 3 close stages on Sundays — duplicate close order attempts.

**Fix:** Skip `_enforce_recent_closes(days=7)` on Sundays (`weekday() == 6`):
```python
_ahp_wday = self._now_ny().weekday()
if _ahp_wday != 6:  # Fix AJ1: 21-day sweep already covers Sunday
    self._enforce_recent_closes(days=7)
self._after_hours_batch_placement()
```

**Impact:** Sundays only run the 21-day sweep (via `_after_hours_batch_placement`), eliminating duplicate processing.

---

### Fix AJ2: Pre-Existing SELL BAG Counts as Successful Unwind (Feb ~24)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/PlaceAnOrder.py` (CALL_OPEN and PUT_OPEN opposite-side unwind paths, ~lines 3071–3088 and ~3133–3149)

**Issue:** Fix AA1 blocked OPEN orders when opposite-side unwind returned 0 spreads closed (`n_unw=0`). But `n_unw=0` can also mean the close guard blocked a new close order because one **already existed** in IB. In that case blocking the OPEN was wrong — the pre-existing SELL BAG close is functionally equivalent to a successful unwind.

**Fix:** When `n_unw=0`, check for a pre-existing working SELL BAG close order via `ib.openTrades()`. If found, allow the OPEN:
```python
ib.reqAllOpenOrders(); ib.sleep(0.3)
_pre_close_aj2 = any(
    getattr(_t2.contract, "secType", "") == "BAG"
    and getattr(_t2.contract, "symbol", "").upper() == symbol.upper()
    and (getattr(_t2.order, "action", "") or "").upper() == "SELL"
    and _t2.orderStatus.status.lower() in ("presubmitted", "submitted", "inactive")
    for _t2 in ib.openTrades()
)
if _pre_close_aj2:
    allow_call = True  # pre-existing close ≡ successful unwind
else:
    record_attempt(..., "opposite_unwind_failed"); continue
```

Applied to both CALL_OPEN and PUT_OPEN paths.

**Impact:** OPEN orders are no longer blocked when a same-symbol close order already exists from a prior cycle.

---

### Fix AK: Watchdog — Add CloudflareTunnel Service Check (Feb ~24)
**Status:** ✓ IMPLEMENTED

**Location:** `C:\OptionsHistory\bin\IB_Watchdog.ps1` (Check 3 block)

**Issue:** The watchdog (Fix AB8) only checked IB Gateway port 7497 and listener `/health`. A CloudflareTunnel crash left TradingView signals unable to reach the listener while the watchdog reported everything healthy.

**Fix:** Added Check 3 — if CloudflareTunnel is not Running and IB + listener are healthy, do a **targeted tunnel-only restart** (`Start-Service CloudflareTunnel`) instead of full `BounceServices.cmd` (which disrupts IB connections unnecessarily). Full restart only fires when IB Gateway or listener is down.

**Impact:** Tunnel crashes auto-recover within 15 minutes without disrupting IB Gateway or open orders.

---

### Fix AL: After-Hours Stock Price Priority — close > last > mid (Feb ~24)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/listener.py` (`get_option_data`, ~line 816)

**Issue:** During after-hours, IB's `last` tick reflects thin/illiquid AH trading. Black-Scholes pricing requires the official 4 PM closing price. The listener always preferred `last` over `close`, feeding unreliable AH prices into theo calculations for the 5 PM batch.

**Fix:** After-hours (outside 9:30 AM–4:30 PM ET, weekdays): prefer `close > last > mid`. During market hours: prefer `last > close > mid`. The 4:30 PM threshold was later refined to 4:30 PM by Fix AN (IB's `close` tick shows previous session's settlement until ~4:15–4:30).

**Impact:** 5 PM batch signals use the official closing price for theo calculations instead of unreliable AH trade prices.

---

### Fix AM: Corrected AI1 — Portfolio Price Before Skip Gate (Feb 24)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/PlaceAnOrder.py` (`force_close_symbol_via_positions()`, `use_fallback` block ~line 2192)

**Issue:** Fix AI1 added portfolio-based limit pricing inside the `use_fallback` block. But the `use_fallback` block is only entered when individual-leg fallback is active. For most risk exits, the code reaches a skip gate (`if limit is None: continue`) BEFORE the `use_fallback` block, so AI1's portfolio pricing was never reached. PFE, D, T risk exits with bid=0 options still hit `no_viable_limit_all_fallbacks_failed`.

**Fix:** Moved portfolio price lookup to BEFORE the `if limit is None:` gate. When `limit is None` and portfolio prices are available for both legs, compute `limit = round(max(0, long_value - short_value) * 0.95, 2)` immediately. Added log: `"[SYM] AM: portfolio-based limit for C/P atm/oth: long=X.XX short=X.XX limit=X.XX"`.

**Impact:** Risk exit close orders with bid=0 options (OTM, illiquid) now get a valid limit price from portfolio data instead of hitting the skip gate. PFE/D/T/UNM correctly close at 9:45 AM.

---

### Fix AN: Fix AL Regression — After-Hours Threshold 16:00 → 16:30 (Feb 27)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/listener.py` (after-hours stock price priority block, ~line 822)

**Issue:** Fix AL changed stock price priority after hours to `close > last > mid`. But IB's `close` tick (Tick ID 9) shows the **previous session's settlement** until ~4:15–4:30 PM ET. At 4:01 PM when the close batch signals arrive, `close` = yesterday's price, `last` = today's final trade. Fix AL's `_now_al.hour >= 16` fires at exactly 4:00 PM, so all batch signals get yesterday's close price.

Evidence from Feb 26: PLD `current_price=140.03` (Feb 25 close vs Feb 26 actual 142.66), LXP `current_price=49.02` vs actual ~50. This corrupted all Black-Scholes theo values for the evening batch.

**Fix:**
```python
# BEFORE:
_after_hours_al = (
    _now_al.weekday() >= 5
    or _now_al.hour < 9
    or (_now_al.hour == 9 and _now_al.minute < 30)
    or _now_al.hour >= 16
)

# AFTER (Fix AN):
# IB's 'close' tick shows previous session's settlement until ~16:30 ET.
# Batch signals arrive 16:01-16:16 ET — 'last' is today's close trade at that point.
# Only prefer close>last after 16:30, when today's settlement has propagated.
_after_hours_al = (
    _now_al.weekday() >= 5
    or _now_al.hour < 9
    or (_now_al.hour == 9 and _now_al.minute < 30)
    or (_now_al.hour == 16 and _now_al.minute >= 30)
    or _now_al.hour > 16
)
```

**Impact:** 4:01–4:29 PM batch signals use `last` (today's final trade) instead of `close` (yesterday's settlement). After 4:30 PM, `close` is used (today's settlement has propagated). Theo values for evening batch are now correctly priced.

---

### Fix AO: Enrichment OI — Timeout Fix + Column Backfill for `_oi_ok()` (Feb 27)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/LiquidityFilter.py`

**Issue:** Two gaps caused PUT_OPEN signals received after market close to always fail OI validation:

1. **OI tick timeout too short (0.6s):** `_ib_fetcher_factory` waited only 0.6s after `reqMktData`. OI (tick type 101) takes 1-3s to arrive. IV populated immediately; `oi_atm`/`oi_oth` stayed `None` on every run. Evidence: LXP row ends with `,,0.4939` (oi_atm=empty, oi_oth=empty, iv_oth=0.494).

2. **Wrong column names:** `_oi_ok()` in PlaceAnOrder reads `open_interest_atm_put`/`open_interest_otm_put`. The enrichment only wrote to `oi_atm`/`oi_oth` (summary columns). PlaceAnOrder never read those.

Affected: LXP PUT_OPEN (4:01 PM), HLN PUT_OPEN (4:02 PM), BCE PUT_OPEN (4:16 PM) — all `no_viable_limit_or_conditions`.

**Fix — two parts:**

Part 1: `_ib_fetcher_factory(ib, poll_seconds=0.6)` → `poll_seconds=1.5`

Part 2: After writing `oi_atm`/`oi_oth`, also backfill `open_interest_atm_put`/`open_interest_otm_put` when right == "P" and the columns are empty:
```python
if oi1 is not None and right == "P":
    if "open_interest_atm_put" in cols and _need(row.get("open_interest_atm_put")):
        row["open_interest_atm_put"] = int(oi1); updates += 1
if oi2 is not None and right == "P":
    if "open_interest_otm_put" in cols and _need(row.get("open_interest_otm_put")):
        row["open_interest_otm_put"] = int(oi2); updates += 1
```

**Impact:** PUT_OPEN signals received after market close now get OI data written to both the enrichment columns and the `_oi_ok()` validation columns. Evening batch PUT_OPEN orders (LXP, HLN, BCE type) no longer fail OI validation.

---

### Fix AP: Increase Close Guard Sleep — Prevent Duplicate After-Hours Close Orders (Feb 27)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/ib_close_guard.py` line 39; `InteractiveBrokersTrader/DailyCycleManagement.py` line 1655

**Issue:** T (AT&T) accumulated 2 pending Inactive+DAY close orders on Feb 26:
- `17:06:40` — T `close_call,placed,success` (5 PM reconcile)
- `22:21:52` — T `close_call,placed,success` (10 PM cycle — `DAILY_ANALYSIS_COOLDOWN_HOURS=2` expired at 7 PM; new webhook at 10:21 PM triggered a fresh DCM cycle)

Both guard functions (`has_working_auto_close()` in `ib_close_guard.py` and `_has_working_close_order()` in DCM) called `ib.reqAllOpenOrders()` then slept only 0.5s before reading `ib.openTrades()`. The 0.5s was not enough for IB to propagate the existing Inactive+DAY T order across clientIds — the guard returned False and a second order was placed.

Note: 3 PM preclose handles duplicates by cancelling ALL pending SELL BAG orders for a symbol before re-placing one with live pricing.

**Fix:** Increased sleep from 0.5s → 1.5s in both guard locations:
```python
# ib_close_guard.py line 39:
ib.sleep(1.5)  # Fix AP: was 0.5 — allow more time for IB to propagate cross-clientId Inactive+DAY orders

# DailyCycleManagement.py line 1655 (_has_working_close_order):
ib.sleep(1.5)  # Fix AP: was 0.5 — allow more time for IB to propagate cross-clientId Inactive+DAY orders
```

**Impact:** Reduced likelihood of duplicate after-hours close orders when a second DCM cycle fires within the same evening (e.g., late webhook after cooldown expires).

---

### Fix AQ: Watchdog Cascade — Task Schedule Collision + BounceServices Timeout (Feb 27)
**Status:** ✓ IMPLEMENTED

**Location:** `C:\OptionsHistory\bin\BounceServices.cmd`; Windows Task Scheduler (`IB_PreClose_RestartListener_1530`, `IB_Watchdog_Every15Min`)

**Issue:** Every trading day starting at ~2:30 PM, the watchdog fired a cascade of IBGateway restarts that continued for 1-3 hours. Three compounding problems:

1. **Wrong task trigger time:** `IB_PreClose_RestartListener_1530` was scheduled at **14:30 (2:30 PM)** instead of 15:30 (3:30 PM). The task restarts the OptionsListener for ~45 seconds, and the watchdog runs at exactly :30 — so it always caught the listener mid-restart and triggered a full `BounceServices` (killing IBGateway unnecessarily).

2. **BounceServices poll timeout too short:** After `nssm restart IBGateway`, BounceServices only polled health for ~10 seconds. IBGateway (Java) needs 60-90 seconds to stop and restart. So BounceServices almost always returned `rc=1 WARN`, the watchdog treated this as failure, and fired again 15 minutes later (after the 10-minute cooldown expired).

3. **NSSM restart cascade:** When the next BounceServices fired while IBGateway was still stopping from the previous restart, NSSM returned `Unexpected status SERVICE_STOP_PENDING`. OptionsListener got `StartService FAILED 1056: already running`. The cascade self-perpetuated.

**Secondary impact:** The cascade caused `_working_close_limit_symbols()` to return an empty set at 3 PM because IBGateway had just restarted and hadn't re-synchronized its order state with IB's servers. Risk exit LMT orders placed at 9:45 AM (OHI, PG) were not detected and not converted to MKT at preclose.

**Fix — three changes:**
1. **Task trigger time fixed:** `IB_PreClose_RestartListener_1530` trigger changed from `14:30` → `15:30` via Task Scheduler.
2. **Watchdog schedule offset:** `IB_Watchdog_Every15Min` start time changed from `06:00` → `06:07` so it runs at `:07, :22, :37, :52`. The listener restart at 15:30 completes by ~15:31; the next watchdog check at 15:37 sees health 200 → no cascade.
3. **BounceServices.cmd:** Added `timeout /t 30 >nul` after `nssm restart IBGateway` (gives IBG time to start before polling); increased poll iterations from 10 → 30 (~90 seconds total polling window).

```cmd
"C:\Program Files\nssm-2.24\win64\nssm.exe" restart "IBGateway" >>"%LOG%" 2>&1
timeout /t 30 >nul         ← NEW: 30s for IBG to start before listener connects
sc stop  OptionsListener   >>"%LOG%" 2>&1
sc start OptionsListener   >>"%LOG%" 2>&1
for /l %%i in (1,1,30) do (  ← was 10; now covers 90s of polling
```

**Impact:** Daily cascade eliminated. BounceServices returns `rc=0` reliably after genuine failures. Watchdog no longer fires during the scheduled 3:30 PM listener restart.

---

### Fix AR: `_working_close_limit_symbols()` Sleep 0.5s → 1.5s (Feb 27)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` line ~128 (`_working_close_limit_symbols`)

**Issue:** Fix AP updated `_has_working_close_order()` (line 1655) and `ib_close_guard.py` (line 39) from `ib.sleep(0.5)` → `ib.sleep(1.5)` to allow more time for IB to propagate cross-clientId orders. But `_working_close_limit_symbols()` — which is called by `_pre_close_market_conversion()` to find open close orders for the 3 PM preclose — was not updated. The 0.5s sleep can cause the preclose to miss recently-placed orders when IB's cross-clientId propagation is delayed (especially common after IBGateway restarts).

**Fix:**
```python
ib.reqAllOpenOrders()  # Fix U1: see orders from ALL client IDs
ib.sleep(1.5)  # Fix AP: match _has_working_close_order sleep; 0.5s was too short for cross-clientId propagation
```

**Impact:** Preclose reliably detects risk exit LMT orders placed by other processes (clientId=101 at 9:45 AM) even when IB's order synchronization is delayed.

---

### Fix AS: Central IB Connection Config for Paper→Live Switchover (Feb 27)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/ib_config.py` (NEW FILE); all IB connect calls across 5 Python files and 2 PowerShell scripts

**Issue:** IB Gateway port (7497=paper, 7496=live) was hardcoded in 18+ locations across 5 Python files and 2 PowerShell scripts. Switching from paper to live trading required hunting down every occurrence.

**Fix:** Created `InteractiveBrokersTrader/ib_config.py` as the single source of truth:
```python
IB_HOST: str = "127.0.0.1"
IB_PORT: int = 7497  # Paper trading (change to 7496 for live)
```

All Python files now import and use these constants:
- `ib_close_guard.py`: function defaults use `IB_HOST, IB_PORT`
- `PlaceAnOrder.py`: both `ib.connect()` calls use `IB_HOST, IB_PORT`
- `DailyCycleManagement.py`: all 14 `ib.connect()` calls use `IB_HOST, IB_PORT`
- `listener.py`: both connect calls + `_ib_ports_status()` use `IB_PORT`
- `LiquidityFilter.py`: function defaults and argparse defaults use `IB_HOST, IB_PORT`

PowerShell scripts use their own variable (can't import Python modules):
- `IB_Watchdog.ps1`: `$IB_GW_PORT = 7497` at top; port check uses `$IB_GW_PORT`
- `Health.ps1`: `$IB_PORT = 7497` at top; embedded Python heredocs use `$IB_PORT` (converted from single-quoted to double-quoted to allow PS variable expansion)

**To switch to live trading:** Use `switch_trading_mode.py` (Fix AT) — one command does everything.

**Impact:** Switching between paper and live trading is now a 3-line change across 3 files (vs hunting 18+ locations).

---

### Fix AT: switch_trading_mode.py — One-Command Paper↔Live Switch (Feb 27)
**Status:** ✓ IMPLEMENTED

**Location:** `switch_trading_mode.py` (NEW FILE, repo root)

**Issue:** Fix AS centralised the config but still required manually editing 3 files and restarting IBGateway. Additionally, `C:\IBC\config.ini` (IBC auto-login config) also needs updating — it controls `TradingMode`, `ApiPort`, and `OverrideTwsApiPort` — but was not covered by Fix AS.

**Fix:** Created `switch_trading_mode.py` that atomically updates all 4 config locations and restarts IBGateway via NSSM:
```
python switch_trading_mode.py live    # Switch to live trading (port 7496)
python switch_trading_mode.py paper   # Switch to paper trading (port 7497)
python switch_trading_mode.py status  # Show current mode
```

**Files updated by the script:**
1. `InteractiveBrokersTrader/ib_config.py` — `IB_PORT = 7497/7496`
2. `C:\OptionsHistory\bin\IB_Watchdog.ps1` — `$IB_GW_PORT = 7497/7496`
3. `C:\OptionsHistory\bin\Health.ps1` — `$IB_PORT = 7497/7496`
4. `C:\IBC\config.ini` — `TradingMode=paper/live`, `ApiPort=7497/7496`, `OverrideTwsApiPort=7497/7496`

Then restarts IBGateway via `nssm restart IBGateway`. IBC handles auto-login with saved credentials — no interactive login required.

**Live trading checklist** (printed after switching to live):
1. IB Gateway shows 'Live Trading' in title bar
2. 'Read-Only API' is OFF in IB Gateway API settings
3. Market data subscriptions are active (Error 354 = subscription missing)
4. Run `Health.ps1` to confirm all services healthy on port 7496

**Impact:** Paper↔live switch is now a single command. IBC config updated automatically — no manual file edits.

---

### Fix AU: Remove Redundant `_enforce_recent_closes` — Eliminate Duplicate 5 PM Close Orders (Feb 27)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`daily_trading_cycle`, ~line 3344)

**Issue:** OHI received two SELL close orders on Feb 27 within 47 seconds:
- `17:06:52` — `close_call, $0.58 LMT` (from `_enforce_recent_closes(days=7)`)
- `17:07:39` — `close_call, $0.52 LMT` (from `_after_hours_batch_placement()`)

**Root Cause:** `daily_trading_cycle()` called `_enforce_recent_closes(days=7)` THEN `_after_hours_batch_placement()`. Both call `_delegate_close_from_csvs_within()` — the 7-day call is a strict subset of the 21-day call. Orders placed by the 7-day call are NOT added to `_submitted_close_syms` (only reconcile writes to that set). When the 21-day call ran 47 seconds later, `_submitted_close_syms` didn't contain OHI, and the IB guard `_has_working_close_order()` failed to detect the 47-second-old `Inactive+DAY` order (cross-clientId propagation lag — same issue that drove Fix AP).

Fix AJ1 already diagnosed and fixed this exact problem on Sundays. That fix's own comment stated: *"The 7-day enforce is a subset of the 21-day sweep"* — which is true every day.

**Fix:** Removed the `_enforce_recent_closes(days=7)` call entirely. The 21-day sweep always covers everything the 7-day sweep would process.

```python
# BEFORE (Fix AJ1 — Sunday-only):
_ahp_wday = self._now_ny().weekday()
if _ahp_wday != 6:  # 6 = Sunday
    self._enforce_recent_closes(days=7)
self._after_hours_batch_placement()

# AFTER (Fix AU — all days):
self._after_hours_batch_placement()
```

**Impact:** Each symbol gets at most one close attempt per 5 PM cycle (from the 21-day sweep). No more duplicates from the redundant 7-day subset call.

---

### Fix AV: Restart OptionsListener After Mode Switch (Feb 28)
**Status:** ✓ IMPLEMENTED

**Location:** `switch_trading_mode.py` (repo root)

**Issue:** After `python switch_trading_mode.py live`, IBGateway restarted on port 7496, but OptionsListener kept running with the old `IB_PORT=7497` loaded in memory. The listener's health endpoint returned `positions_count: 0` and `"not connected"` because it was still trying to connect to 7497 (not listening after IBGateway switched to 7496).

Evidence from health_20260228_131937:
- `listener.err.log: Connecting to 127.0.0.1:7497 with clientId 42...ConnectionRefusedError`
- `positions_count: 0`, `positions_error: "not connected"`

**Fix:** Added `restart_options_listener()` function and called it after `restart_ibgateway()`:
```python
def restart_options_listener() -> None:
    """Restart OptionsListener so it reloads ib_config.py with the new IB_PORT."""
    if DRY_RUN:
        print("  [DRY RUN] Would run: nssm restart OptionsListener")
        return
    print("\nRestarting OptionsListener service (to pick up new IB_PORT)...")
    result = subprocess.run(["nssm", "restart", "OptionsListener"], ...)
    ...

# After file updates:
restart_ibgateway()
restart_options_listener()   # ← added
```

**Impact:** After mode switch, OptionsListener restarts and reloads `ib_config.py` with the new `IB_PORT`. Listener connects to the correct IBGateway port immediately.

---

### Fix AW: Update Repo Health.ps1 in switch_trading_mode.py (Feb 28)
**Status:** ✓ IMPLEMENTED

**Location:** `switch_trading_mode.py` (repo root)

**Issue:** Even after Fix AV, P/L, positions, and orders sections in the health report still failed with `ConnectionRefusedError` on port 7497. Root cause: there are **two separate Health.ps1 files**:

| File | Used by | Updated before Fix AW? |
|------|---------|------------------------|
| `C:\OptionsHistory\bin\Health.ps1` | Scheduled `IB_DailyHealth_0830` | ✓ Yes |
| `Health.ps1` (repo root) | `IB_Health_0715` task, `PushButtonMenu.ps1` health check | ✗ Never |

The repo version always had `$IB_PORT = 7497` hardcoded (line 15). The heredocs are double-quoted (so `$IB_PORT` expands correctly), but since the variable was never updated, all generated temp Python files always got `7497`.

Evidence from health_20260228_132735:
- Positions temp file: `ib.connect('127.0.0.1', 7497, clientId=897)` — clientId 897 matches repo Health.ps1
- P/L error: `At PushButtonMenu.ps1:44` calling `& powershell ... -File $Script` — PushButtonMenu calls the repo Health.ps1

**Fix:** Added repo Health.ps1 as a 6th file in the update loop. Also added a `path` parameter to `update_health_ps1()` to support updating both paths from the same function:

```python
HEALTH_PS1_REPO = SCRIPT_DIR / "Health.ps1"   # Fix AW: also updated by PushButtonMenu + IB_Health_0715

def update_health_ps1(port: int, path: Path = HEALTH_PS1) -> None:
    original = _read(path)
    updated = _sub(r"^(\$IB_PORT\s*=\s*)\d+", rf"\g<1>{port}", original, flags=re.MULTILINE)
    _write(path, original, updated)

# Update loop now covers 6 files:
for name, fn, fargs in [
    ("ib_config.py",                           update_ib_config_py,    (port,)),
    ("IB_Watchdog.ps1",                        update_watchdog_ps1,    (port,)),
    ("C:\\OptionsHistory\\bin\\Health.ps1",    update_health_ps1,      (port,)),
    ("C:\\IBC\\config.ini",                    update_ibc_config,      (port, trading_mode)),
    ("C:\\IBC\\run_gateway_service.cmd",       update_run_gateway_cmd, (trading_mode,)),
    ("Health.ps1 (repo)",                      update_health_ps1,      (port, HEALTH_PS1_REPO)),  # ← added
]:
```

Also updated the module docstring from "5 files" to "6 files" and "Then restarts IBGateway" to "Then restarts IBGateway via NSSM" with OptionsListener restart noted.

**Impact:** All six config locations are updated atomically. Both `IB_Health_0715` (scheduled task) and `PushButtonMenu.ps1` health checks connect to the correct port after a mode switch. Confirmed in health_20260228_133631: P/L shows data, positions/orders show no ConnectionRefusedError.

---

### Fix AX: Health.ps1 Improvements — Pause, Byte-Seek, ib_cycle.log Patterns (Mar 2)
**Status:** ✓ IMPLEMENTED

**Location:** `Health.ps1` (repo root), `PushButtonMenu.ps1` (repo root)

**Changes (4 sub-fixes):**

**AX1 — Press Enter pause:** Added `Read-Host "Press Enter to continue"` at the end of Health.ps1 so the report doesn't immediately return to the menu. Removed redundant `Pause-Enter` from PushButtonMenu option 1.

**AX2 — Byte-seek for ib_cycle.log:** `Read-SharedFile` previously read the entire file into memory before taking the tail — causing a hang on large logs. Replaced with file-seek approach: seeks to `Length - (tail * 900)` bytes from end, reads only that chunk. Handles shared-read locking for concurrent Python writes.

**AX3 — Fix ib_cycle.log search patterns:** All 5 search patterns were written for an old log format that no longer exists. Updated to match the actual Python/ib_insync format:
- `$placed` → `orderStatus:.*status='(Submitted|PreSubmitted)'` (excludes `openOrder:` broadcasts that double-count)
- `$closed` → `orderStatus:.*permId=\d+, action='SELL'.*status=...` (anchors to Order-level action, not comboLeg action)
- `$weekly` → `[Ff]orce.close|[Ss]tage\s+2[^0-9]|force_close`
- `$failed` → `\bERROR\b|Exception:|no_viable_limit|SKIPPING order`
- `$skipped` → `skipped|defer_to_force|no_spread_in_positions`

Added `$formatOrder` scriptblock that parses raw ib_insync lines into readable one-liners:
`2026-02-16 17:05  MNST    SELL  LMT  @$0.85`

Fixed `$extractSym` to handle both `[SYM]` and `symbol='SYM'` log formats.

**AX4 — Filter listener.err.log noise in Recent Logs:** listener.err.log is always among the 3 most-recently-modified logs. Its tail is full of verbose `ib_insync.wrapper:(position|updatePortfolio|openOrder):` lines (~400 chars each) already shown in Positions section. Added filter to suppress these, leaving only meaningful ERROR/WARNING lines.

**Impact:** Health report "Recent PlaceAnOrder activity" section now shows accurate counts (not 0/false-positives) and readable order summaries. "Recent Logs" section no longer flooded with position dump noise.

---

### Fix AY: Worthless Close Guard + AH1 Race Condition (Mar 2)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/ib_close_guard.py`, `InteractiveBrokersTrader/DailyCycleManagement.py` (`_has_working_close_order`), `InteractiveBrokersTrader/PlaceAnOrder.py` (both AH1 blocks)

**Issue:** NWG CALL 20/22.5 accumulated 2 conflicting open orders: a SELL BAG combo (from reconcile) and an individual BUY 22.5C (from the worthless individual-leg path). If both filled, the BAG would close the spread while the standalone BUY 22.5C created an unintended LONG 22.5C position.

**Three root causes:**

**AY1 — Close guard blind to individual OPT SELL orders:**
`_has_working_close_order()` (DCM) and `has_working_auto_close()` (ib_close_guard) only looked for SELL BAG orders. When the worthless path placed individual SELL 20C + BUY 22.5C orders, both guards returned `False` → the 5 PM reconcile saw no working close and placed a new SELL BAG alongside the existing individual legs.

**Fix:** Added second pass in both guard functions to detect individual OPT SELL orders for the same symbol as a working close indicator.

**AY2 — AH1 cancel is fire-and-forget (race condition):**
AH1 sent `ib.cancelOrder()` then slept only 0.4s before proceeding to place individual legs. IB cancel acknowledgement can take >0.4s (especially for Inactive+DAY orders activating at market open) → individual legs placed while BAG still "pending cancellation".

**Fix:** After sending cancels, polls `ib.openTrades()` up to 10× (5 seconds) to confirm all SELL BAGs are gone. If still present after 5s, skips individual leg placement (`continue` to next spread pair) and lets the existing BAG handle the close.

**AY3 — AH1 cancelled ALL BAGs including BUY (OPEN) orders:**
Previously cancelled any BAG for the symbol regardless of action. BUY BAGs are OPEN orders — should not be cancelled during close operations.

**Fix:** Added `action == "SELL"` filter to AH1 cancel loop in both one-leg-worthless and both-worthless paths.

**Impact:**
- Reconcile no longer places duplicate SELL BAGs when individual worthless legs are already working
- Individual legs are only placed after confirming the SELL BAG is fully cancelled
- OPEN BUY BAG orders are never accidentally cancelled by the worthless close path

---

### Fix AZ: Health.ps1 P/L — Non-Blocking reqAccountUpdates (Mar 4)
**Status:** ✓ IMPLEMENTED

**Location:** `Health.ps1` (repo root), `C:\OptionsHistory\bin\Health.ps1`

**Issue:** "Day Realized" and "Day Unrealized" P/L showed "-" in health reports. A prior fix added `ib.reqAccountUpdates(acct)` which caused Health.ps1 to hang indefinitely — requiring the user to kill the process.

**Root cause:** `ib.reqAccountUpdates(acct)` is the high-level ib_insync blocking wrapper. It calls `self.run(self.wrapper.updateAccountTimeEvent.wait())` — waiting forever for IB to send an `updateAccountTime` event. Paper trading accounts often never send this event, causing an infinite hang.

**Fix:** Replaced the blocking high-level call with the non-blocking EClient call + sleep + unsubscribe:

```python
# BEFORE (hangs indefinitely on paper accounts):
if acct:
    ib.reqAccountUpdates(acct)  # blocking — waits for updateAccountTime event
avs = ib.accountValues(acct) if acct else []

# AFTER (non-blocking, 4s sleep to let IB push values):
if acct:
    ib.client.reqAccountUpdates(True, acct)   # non-blocking EClient subscribe
    ib.sleep(4.0)                               # wait for IB to push account values
    avs = ib.accountValues(acct)
    ib.client.reqAccountUpdates(False, acct)   # unsubscribe cleanly
```

Adds ~5 seconds to health report runtime (1s connect sleep + 4s account sleep) but never hangs.

**Impact:** Day Realized and Day Unrealized P/L now show numeric values instead of "-". Health report completes reliably.

---

### Fix BA: OI Cancel Column Alias Fix — Low-OI Order Cancellation Never Worked (Mar 4)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_cancel_low_oi_working_orders_from_csv`, `_find_csv_oi` helper)

**Issue:** The 9:45 AM low-OI cancellation guard (`_cancel_low_oi_working_orders_from_csv`) never cancelled any orders — despite being called correctly and enrichment running. Orders like LXP (OI 15/10) and FER (OI 9/28), both well below the 100 threshold, remained as working orders all day.

**Root cause:** The `_find_csv_oi()` helper used alias lists that don't match the actual listener CSV column names:

| Alias looked for | Actual listener CSV column |
|-----------------|---------------------------|
| `"atm"`, `"k_atm"` | **`"atm_strike"`** |
| `"oth"`, `"k_oth"` | **`"otm_strike_call"` / `"otm_strike_put"`** |
| `"open_interest_atm"` | **`"open_interest_atm_call"` / `"open_interest_atm_put"`** |

Since `ra = _get(row, atm_strike_aliases)` returned `None` on every row (no alias matched `atm_strike`), the check `if ra is None or ro is None: continue` skipped every row → `_find_csv_oi` always returned `(None, None, None)` → no orders were ever cancelled.

**Fix:** Made the alias lists right-aware (CALL vs PUT) and added the actual listener CSV column names:

```python
# BEFORE: static aliases that never matched listener CSV format
cand_keys = {
    "atm_strike": ("atm", "k_atm", "strike_atm", ...),          # missed atm_strike
    "oth_strike": ("oth", "k_oth", "strike_oth", ...),           # missed otm_strike_call/put
    "oi_atm":     ("oi_atm", ..., "open_interest_atm", ...),     # missed open_interest_atm_call
    ...
}

# AFTER: right-aware aliases with listener CSV column names first
if r_u == 'C':
    cand_keys = {
        "atm_strike": ("atm_strike", "atm", "k_atm", ...),
        "oth_strike": ("otm_strike_call", "oth", "k_oth", ...),
        "oi_atm":     ("oi_atm", ..., "open_interest_atm_call", ...),
        "oi_oth":     ("oi_oth", ..., "open_interest_otm_call", ...),
    }
else:  # PUT
    cand_keys = {
        "atm_strike": ("atm_strike", "atm", "k_atm", ...),
        "oth_strike": ("otm_strike_put", "oth", "k_oth", ...),
        "oi_atm":     ("oi_atm", ..., "open_interest_atm_put", "open_interest_atm_call", ...),
        "oi_oth":     ("oi_oth", ..., "open_interest_otm_put", "open_interest_otm_call", ...),
    }
```

For PUT signals, call OI columns serve as fallback proxies for put OI (which the listener often leaves empty — matching Fix AO's proxy logic in LiquidityFilter).

The fix works with the raw listener CSV even when LiquidityFilter enrichment (`oi_atm`/`oi_oth`) has not yet run. When enrichment has run, the enriched values (first in alias list) are preferred.

**Impact:**
- LXP CALL 50/55 (OI 15/10): both < 100 → **cancelled at 9:45 AM**
- FER PUT 70/65 (OI 9/28 via call proxy): both < 100 → **cancelled at 9:45 AM**
- BG CALL 115/120 (OI 960/435): both ≥ 100 → **kept**
- All previously placed low-OI OPEN orders will be correctly evaluated going forward

---

### Fix BB: Add `reqAllOpenOrders()` to `_cancel_low_oi_working_orders_from_csv()` (Mar 5)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_cancel_low_oi_working_orders_from_csv`, line ~3189)

**Issue:** LXP and FER low-OI BUY orders were NOT cancelled at 9:45 AM on March 5 despite Fix BA correctly fixing the CSV column aliases. The cancel function connected with clientId=887 and immediately called `ib.openTrades()` without first calling `ib.reqAllOpenOrders()`. On a fresh connection, `openTrades()` only returns orders placed by the current clientId in the current session — returning empty. The LXP/FER/BG BUY orders (placed by clientId=101/listener at 5 PM as Inactive+DAY) were invisible. Log confirmed: `"CSV OI cancel: no open trades."` at 9:46 AM.

This is the same class of bug as Fix U1 (which fixed `_has_working_close_order`, `_working_close_limit_symbols`, `_cancel_symbol_close_orders`) — `_cancel_low_oi_working_orders_from_csv()` was missed.

**Fix:**
```python
# BEFORE (bug — no reqAllOpenOrders):
trades = ib.openTrades() or []

# AFTER (Fix BB):
ib.reqAllOpenOrders()
ib.sleep(1.5)  # Fix BB: allow IB to propagate cross-clientId orders (same as Fix AP)
trades = ib.openTrades() or []
```

**Impact:** Low-OI cancellation function now sees cross-clientId orders (Inactive+DAY placed by PlaceAnOrder/reconcile). LXP/FER-type orders will be correctly evaluated and cancelled at 9:45 AM.

---

### Fix BC: Add `reqAllOpenOrders()` to `_rth_liquidity_cleanup()` (Mar 5)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_rth_liquidity_cleanup`, line ~2998, clientId=879)

**Issue:** Same pattern as Fix BB — `_rth_liquidity_cleanup()` (live-OI-based cleanup) also called `ib.openTrades()` without `reqAllOpenOrders()` first, returning empty and logging `"RTH cleanup: no open trades to evaluate."`. ib_cycle.log confirmed this at 9:46:23 on March 5.

**Fix:**
```python
# BEFORE (bug):
try:
    trades = ib.openTrades()

# AFTER (Fix BC):
try:
    ib.reqAllOpenOrders()
    ib.sleep(1.5)  # Fix BC: allow IB to propagate cross-clientId orders (same as Fix AP)
    trades = ib.openTrades()
```

**Impact:** Live-OI cleanup function now sees all open orders from any clientId. Both CSV-based and live-OI-based cleanup paths now correctly evaluate low-OI cancellations.

---

### Fix BD: Health.ps1 `@\${price}` Syntax Error in Placed-Orders Probe (Mar 5)
**Status:** ✓ IMPLEMENTED

**Location:** `Health.ps1` (repo root), lines 401 and 427 (inside `@" "@` double-quoted PS here-string)

**Issue:** Health report showed `SyntaxError: invalid escape sequence '\ '` in the placed-orders probe section. Root cause: `@\${price}` inside a PS double-quoted here-string — PowerShell expands `${price}` as a PS variable (empty, undefined), leaving `@\ ` (backslash + space) in the generated Python file. Python 3.12 raises `SyntaxWarning: invalid escape sequence '\ '`. Same for `@\${px:.2f}` at line 427.

**Fix:** Changed `\$` → `` `$ `` (backtick escapes `$` in PS double-quoted strings, producing literal `$` in output):
- Line 401: `@\${price}` → `` @`${price} ``
- Line 427: `@\${px:.2f}` → `` @`${px:.2f} ``

Both produce `@${price}` and `@${px:.2f}` as literal Python text (valid f-string syntax where `$` is a literal char and `{price}`/`{px:.2f}` are Python variables).

Note: `C:\OptionsHistory\bin\Health.ps1` uses an older placed-orders implementation without this pattern — no change needed there.

**Impact:** Placed-orders section in health report no longer shows SyntaxError. The probe generates and runs valid Python, displaying filled order summary with prices.

---

### Fix BE: Log `_rth_liquidity_cleanup()` Cancellations to Attempts CSV (Mar 5)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_rth_liquidity_cleanup`, cancel block ~line 3057)

**Issue:** Fix Q (Feb 7) added `_AttemptLogger.write()` to `_cancel_low_oi_working_orders_from_csv()` (CSV-based OI path) but missed `_rth_liquidity_cleanup()` (live-OI path). Cancellations from the live-OI path only appeared in `ib_cycle.log` (`RTH cleanup: cancelled low-OI order...`) and not in the attempts CSV, making them invisible to post-trade audit.

**Fix:** Added `_AttemptLogger.write()` after `ib.cancelOrder(o)` in `_rth_liquidity_cleanup()`:
```python
_AttemptLogger.write(
    symbol=sym,
    action="cancel_open",
    status="placed",
    reason="low_oi_live",   # distinguishes from CSV-based "low_oi_both_legs"
    exp=exp, right=_r, atm=_atm, oth=_oth,
)
```
Uses `reason="low_oi_live"` to distinguish from `_cancel_low_oi_working_orders_from_csv()`'s `"low_oi_both_legs"`.

**Impact:** All low-OI cancellations now appear in the attempts CSV regardless of which path (CSV-based or live-OI) triggered them.

---

### Fix BF: clientId=0 Attempted for Cross-ClientId Cancel (Mar 5)
**Status:** ✗ DID NOT WORK — superseded by Fix BG

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py`

**Attempt:** Changed both `_rth_liquidity_cleanup()` (clientId=879) and `_cancel_low_oi_working_orders_from_csv()` (clientId=887) to connect with `clientId=0`, which IB API documentation says can cancel orders from any client (TWS 9.80+ "master client"). Still received Error 10147 in paper trading.

**Outcome:** clientId=0 does not grant cross-clientId cancel permissions in IB paper trading (and likely live too). See Fix BG.

---

### Fix BG: clientId=101 for Cross-ClientId Cancel (Mar 5)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_rth_liquidity_cleanup` ~line 2973; `_cancel_low_oi_working_orders_from_csv` ~line 3202)

**Issue:** Low-OI order cancellations failed with Error 10147 ("OrderId that needs to be cancelled is not found"). IB only allows cancelling orders from the same clientId that placed them. All OPEN BUY orders are placed by PlaceAnOrder.py which uses `clientId=101`. Both cancel functions were using different clientIds (879, 887, then 0 via Fix BF) — none of which placed the orders.

**Fix:** Changed both cancel function connections to use `clientId=101`:
```python
ib.connect(IB_HOST, IB_PORT, clientId=101, timeout=6)
```

**Safety:** The cleanup runs at 9:45 AM. PlaceAnOrder's 5 PM batch finished ~17 hours earlier. No concurrent clientId=101 session expected. (Risk exit subprocess at 9:45 AM also uses clientId=101 but runs sequentially, not concurrently with the cleanup.)

**Impact:** `ib.cancelOrder(o)` now cancels the correct orders — IB recognizes clientId=101 as the placing client and accepts the cancel.

---

### Fix BH: `_AttemptLogger.write()` NameError — Redundant Local `datetime` Import (Mar 5)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_AttemptLogger.write()`, ~line 84)

**Issue:** `_AttemptLogger.write()` threw `NameError: cannot access local variable 'datetime' where it is not associated with a value` when called from `_rth_liquidity_cleanup()`. The error occurred on line 60: `datetime.now(NY).isoformat()`.

**Root cause:** Python's scoping rules: when a function contains ANY assignment to a variable name, Python's compiler treats ALL references to that name as local to the function. Inside `write()`, the nested `try` block at line 84 had `from datetime import datetime` — a local import/assignment. This made Python treat `datetime` as local throughout `write()`. At line 60 (before the assignment at line 84), the local `datetime` was unbound → NameError.

Note: `datetime` IS imported at module level (`from datetime import datetime` at line 3). The redundant local import was unnecessary.

**Fix:** Removed `from datetime import datetime` from inside `write()`'s nested try block. Added comment: `# datetime already imported at module level`.

**Impact:** `_AttemptLogger.write()` no longer throws NameError when called from any DCM function. All low-OI cancellations write to the attempts CSV correctly.

---

### Fix BI: Separate `_AttemptLogger.write()` from `cancelOrder` try/except (Mar 5)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_rth_liquidity_cleanup`, cancel block ~line 3059)

**Issue:** The `_AttemptLogger.write()` call (Fix BE) was inside the same `try/except` as `ib.cancelOrder(o)`. If the write failed, the except block logged `"RTH cleanup: failed to cancel order for X"` — misleading, since the cancel DID succeed and the logging was what failed.

**Fix:** Moved `_AttemptLogger.write()` outside the cancel try/except. Added `continue` in the cancel's except block (so a cancel failure skips the write). Wrapped the write in its own `try/except _be_err` that logs `"attempts CSV write failed"` separately.

**Structure after fix:**
```python
try:
    ib.cancelOrder(o)
    cancelled += 1
    LOG.info("RTH cleanup: cancelled ...")
except Exception as e:
    LOG.warning("RTH cleanup: failed to cancel order ...")
    continue  # skip write if cancel failed
# Fix BE/BI: log to attempts CSV outside the cancel try/except
try:
    _AttemptLogger.write(...)
except Exception as _be_err:
    LOG.warning("RTH cleanup: attempts CSV write failed ...")
```

**Impact:** Cancel failures and write failures are independently logged. Cancel success is not masked by write errors.

---

### Fix BJ: `_rth_liquidity_cleanup()` — BUY-Only Filter (Mar 5)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_rth_liquidity_cleanup`, ~line 3018)

**Issue:** `_rth_liquidity_cleanup()` evaluated ALL BAG combo orders for OI cancellation regardless of `order.action`. This caused SELL BAG orders (close orders placed by risk exits) to be cancelled when their legs had low OI. The live-OI path lacked the BUY filter that already existed in `_cancel_low_oi_working_orders_from_csv()` (lines 3240-3242).

Evidence: PBR and T SELL close orders placed at 9:47 AM by risk exits were cancelled by the cleanup at 11:27 AM.

**Fix:** Added `if (getattr(o, 'action', '') or '').upper() != 'BUY': continue` immediately after the BAG type check:
```python
# Only consider active, unfilled COMBO (BAG) BUY orders.
# SELL BAGs are close orders — must NOT be cancelled here.
if getattr(c, 'secType', '') != 'BAG':
    continue
if (getattr(o, 'action', '') or '').upper() != 'BUY':
    continue
```

**Impact:** SELL BAG (close) orders are never cancelled by `_rth_liquidity_cleanup()`. Only BUY (open) orders with low OI are cancelled. Consistent with `_cancel_low_oi_working_orders_from_csv()`'s behavior.

---

### Fix BK: `_rth_liquidity_cleanup()` — OI=-1 Conservative Guard (Mar 5)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_rth_liquidity_cleanup`, ~line 3058)

**Issue:** When IB's `reqMktData` couldn't return OI data (e.g., timing issue, Error 10091), `_live_oi()` returned `None` which was stored as `-1`. The old cancel condition:
```python
leg1_ok = oi_values[0] is not None and oi_values[0] > MIN_OI_FOR_RTH
if not (leg1_ok or leg2_ok):  # cancel if neither leg meets threshold
```
`-1 is not None` is `True`, `-1 > 100` is `False` → `leg1_ok = False`. If BOTH legs returned -1 (no data), BOTH were treated as low-OI → order cancelled incorrectly. BG 115/120 CALL April 17 was cancelled this way.

**Fix:** Added explicit known-data check before the threshold evaluation:
```python
# Cancel only if we have actual OI data for BOTH legs and BOTH are below threshold.
# OI=-1 means IB returned no data — treat as unknown, do NOT cancel (conservative).
leg1_known = oi_values[0] != -1
leg2_known = oi_values[1] != -1
if not (leg1_known and leg2_known):
    LOG.info("RTH cleanup: skipping %s — OI data unavailable (OI=%s); not cancelling.", sym, oi_values)
    continue
leg1_ok = oi_values[0] > MIN_OI_FOR_RTH
leg2_ok = oi_values[1] > MIN_OI_FOR_RTH
if not (leg1_ok or leg2_ok):
    # cancel
```

**Impact:** Orders for which IB cannot return OI data are conservatively kept (not cancelled). Only orders where BOTH legs have confirmed OI data showing both below threshold are cancelled. Consistent with `_cancel_low_oi_working_orders_from_csv()`'s `if oi_atm is None or oi_oth is None: continue` guard.

---

### Fix BL: LiquidityFilter `_ib_fetcher_factory` — Wrong OI Attribute Name (Mar 5)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/LiquidityFilter.py` (`_ib_fetcher_factory`, ~line 443)

**Issue:** `enrich_combined_csv()` correctly ran at 9:45 AM (confirmed by `iv_oth` being populated in the March 4 CSV for FER, BG, LXP), but `oi_atm`/`oi_oth` were always NaN. Root cause: the attribute loop tried:
```python
for attr in ("optionOpenInterest", "openInterest", "optOpenInterest"):
    val = getattr(t, attr, None)  # ← always None — wrong attribute names
```
ib_insync's `Ticker` object exposes `callOpenInterest`/`putOpenInterest` (confirmed by `listener.py` which uses them successfully to get OI=960 for BG, OI=15 for LXP). The names `optionOpenInterest` etc. don't exist on the Ticker → `oi_atm`/`oi_oth` remained NaN despite enrichment running. The 9:45 AM low-OI cancel was silently falling back to the listener's unreliable after-hours OI instead of the fresh RTH data enrichment was supposed to provide.

**Fix:**
```python
# Fix BL: ib_insync Ticker uses callOpenInterest/putOpenInterest (confirmed by listener.py).
_primary = "callOpenInterest" if str(right).upper() == "C" else "putOpenInterest"
for attr in (_primary, "optionOpenInterest", "openInterest", "optOpenInterest"):
    val = getattr(t, attr, None)
    if isinstance(val, (int, float)) and not (val != val):  # not NaN
        oi = int(val)
        break
```
`right` is already in scope from the `_fetch(symbol, right, exp, strike)` closure — no signature change.

Also increased `poll_seconds` default from 1.5 → 3.0: with 1.5s, OTM legs (less liquid) often timed out even when ATM legs returned data fine. Verified: at 3.0s both legs populated for all three OPEN signals (BG 960/435, LXP 15/10, FER 1/1).

**Impact:** `oi_atm`/`oi_oth` now populate correctly for both legs in the enriched CSV. The 9:45 AM cancel uses reliable live RTH OI instead of the listener's after-hours fallback.

---

### Fix BN-1: `_working_close_limit_symbols()` — SELL-Only Filter (Mar 5)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_working_close_limit_symbols`, ~line 136)

**Issue:** `_working_close_limit_symbols()` returned symbols with **either** SELL or BUY working BAG LMT orders. BUY BAG orders are OPEN orders. On March 5, BG had a working BUY order (CALL_OPEN placed at 1:47 PM) that hadn't filled. BG landed in `work_syms`, then `_latest_signal_is_close("BG")` found an old CLOSE signal in the CSV → preclose tried to close a non-existent position.

**Fix:**
```python
# BEFORE:
if (getattr(o, 'action', '') or '').upper() not in ('SELL','BUY'):
    continue

# AFTER (Fix BN-1):
# Only SELL BAG orders are close orders. BUY BAGs are open orders and should
# not cause a symbol to appear as a preclose candidate.
if (getattr(o, 'action', '') or '').upper() != 'SELL':
    continue
```

**Impact:** Symbols with only working BUY (open) orders no longer appear as preclose close candidates. BG correctly excluded from preclose.

---

### Fix BP: Preclose Always Uses "join" Scheme (Mar 6)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_submit_close_shared`, ~line 563)

**Issue:** On March 6, preclose used "mid" pricing instead of "join" pricing for CP, CTVA, O. Orders placed at 15:00 with mid pricing did not fill immediately — required 3 retry cycles before filling at 15:33 (33-minute delay).

**Root cause:** `_submit_close_shared()` called `_determine_live_close_scheme_for_symbol()` for all contexts including preclose. That function uses `_get_position_open_date()` which calls `reqExecutions()`. IB clears execution history on TWS restart (same root cause as Fix X3). When `reqExecutions()` returns empty, falls into `"unknown age → use 'mid'"` branch — silently overriding the intended "join" for preclose.

**Fix:** For `context == "preclose"`, hardcode `scheme = "join"` and skip `_determine_live_close_scheme_for_symbol()`:
```python
if context == "preclose":
    scheme = "join"  # Fix BP: always join at 3 PM
else:
    scheme = self._determine_live_close_scheme_for_symbol(sym)
```
Also updated post-check log reason from hardcoded `delegated_live_mid_working` → `delegated_live_{scheme}_working`.

**Why join is correct for preclose:** At 3 PM, ~55 minutes remain until market close. Positions reaching preclose have already failed to fill all day at conservative prices. "join" catches the current best bid. `--allow-market-fallback` escalates to MKT if join also fails. There is no scenario at 3 PM where "mid" is preferable to "join".

**Impact:** Preclose now consistently uses join pricing. Orders should fill within the first placement attempt instead of requiring multiple retry cycles.

---

### Fix BQ: REVERTED — Original bid(long)-ask(short) Was Correct (Mar 6)
**Status:** ✗ REVERTED

The original `bid(long) - ask(short)` formula for SELL+join is correct and was restored.

**Why the original is correct:** OPEN join = `ask(long) - bid(short)` (buyer crosses both legs at market). CLOSE join = `bid(long) - ask(short)` (seller crosses both legs at market). Both give maximum fill probability by accepting market prices on both legs. This is the "natural sell price" / combo BID — the lowest a seller would accept at market, so it fills against any buyer bidding above this floor.

**Why `bid-bid` and `ask-bid` are wrong for preclose:**
- `bid(long) - bid(short)` = combo mid ≈ $2.30 — passive on short leg, harder to fill
- `ask(long) - bid(short)` = combo ASK ≈ $2.50 — passive on both legs, hardest to fill

At 3 PM with ~55 minutes to close, fill probability > price optimization. `bid-ask` fills immediately; `ask-bid` may not fill at all.

---

### Fix BN-2: Preclose Uses Live Join Pricing (Mar 5)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_submit_close_shared`, ~line 479)

**Issue:** At 3 PM on March 5, the preclose cancelled the morning's risk-exit LMT orders for BK, CP, O, PBR, T (correct) but re-placed them using Stage 1 (from-signal with stale CSV prices from yesterday's 9:45 AM enrichment). Since Stage 1 placed a working order (at the stale price), Stage 2 (live join pricing + MKT fallback) was **never reached**. Result: all 5 positions expired unfilled at 4 PM.

**Root cause:** Stage 1 "succeeds" whenever it places any working order — even at a price that won't fill. Prices that failed to fill from 9:45 AM → 3 PM won't fill in the remaining hour.

**Fix:** For `context == "preclose"`, skip Stage 1 entirely and go directly to Stage 2 (force-close with live join pricing + `--allow-market-fallback` + `--fallback-individual-legs`):

```python
# Fix BN-2: At preclose (3 PM, market open) stale CSV prices already failed to fill
# all day; re-placing at the same prices won't fill in the remaining hour.
# Skip Stage 1 at preclose and go directly to Stage 2 (live join + MKT fallback).
if context != "preclose":
    # Stage 1: CSV-based pricing (after-hours / reconcile only)
    self._run_place_an_order(["--mode", "from-signal", "--use-live-close", "off", ...])
    has_working = self._has_working_close_order(sym)
    if has_working:
        ...log csv_limit_working...
        return  # Done for non-preclose contexts

# Stage 2: Force-close with live pricing (always for preclose; fallback for others)
force_close_args = ["--mode", "force-close", "--use-live-close", scheme,
                    "--fallback-individual-legs", ...]
if context == "preclose":
    force_close_args.append("--allow-market-fallback")
```

**Impact:**
- 3 PM preclose now uses current bid/ask via live join pricing (fills within remaining hour)
- Worthless spreads (BK, NWG): bid=0 → `both_worthless_fixed_price` → $0.05 individual legs
- Stop-loss/TP exits (T, PBR, O, CP): join current bid → fill before 4 PM
- MKT fallback available as last resort if bid=0 and not worthless
- Non-preclose flows (after-hours reconcile) unchanged — still use Stage 1 CSV first

---

### Fix BN-3: Attempts CSV Schema Unified — PlaceAnOrder Fields No Longer Dropped (Mar 6)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_AttemptLogger.write()`, ~line 59); risk exit `_AttemptLogger.write()` call (~line 2884)

**Issue:** DCM created the attempts CSV with a 12-column schema (`ts, symbol, action, status, reason, exp, right, atm, oth, limit, source, uid`). PlaceAnOrder's `_attempts_append()` reads the existing header and uses `extrasaction="ignore"`, silently dropping all PlaceAnOrder-specific fields: `longK`, `shortK`, `order_type`, `order_action`, `qty`, `close_reason`. Fix Z5's `--close-reason` (STOP/TP reason) was never actually appearing in the CSV because of this schema mismatch.

**Fix:** Expanded `_AttemptLogger.write()` row dict to include all PlaceAnOrder ATTEMPT_FIELDS columns (defaulting to `""`). New 18-column canonical schema: `ts, symbol, action, status, reason, exp, right, atm, oth, limit, longK, shortK, order_type, order_action, qty, close_reason, source, uid`. Added `close_reason=reason` to the risk exit `queued` entry so the STOP/TP trigger appears in its own column.

**Impact:** PlaceAnOrder `force_close,placed,success` entries now include `longK`, `shortK`, `order_type`, `order_action`, `limit`, and `close_reason` in the CSV. Risk exit `queued` entries show `close_reason` in its own column (not just embedded in `reason`).

---

### Fix BO: Preclose Cancel Race Condition — Poll Until Cancelled (Mar 6)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_cancel_symbol_close_orders()`, ~line 384)

**Issue:** On March 6, the 3 PM preclose cancelled existing close orders for CP, CTVA, O but then failed to place new orders at live prices. Attempts CSV showed `close_call,skipped,existing_working_close` + `force_close,error,place_failed_positions` for all three, then `preclose_delegated_live_mid_working` (false success). Root cause: `_cancel_symbol_close_orders()` sent `ib.cancelOrder()` and returned immediately — no waiting for IB confirmation. PlaceAnOrder's close guard started ~3 seconds later, still saw the old orders (IB cancellation not yet propagated), and blocked new placement. DCM's post-check found the stale order and logged false success.

**Fix 1 (polling):** After sending all cancel requests, poll `ib.openTrades()` in a loop (up to 10 seconds) until no active SELL BAG orders remain for the symbol. Same pattern as Fix AY1.

**Fix 2 (initial sleep):** Increased `ib.reqAllOpenOrders()` sleep from 0.5s → 1.5s (matches Fix AP/BB/BC/AR pattern).

**Impact:** `_cancel_symbol_close_orders()` only returns after IB confirms the cancellation. PlaceAnOrder's close guard finds nothing → replacement order placed at live prices.

---

### Fix BO2: `_cancel_symbol_close_orders()` Must Use clientId=101 (Mar 6)
**Status:** ✓ IMPLEMENTED

**Location:** `InteractiveBrokersTrader/DailyCycleManagement.py` (`_cancel_symbol_close_orders()`, ~line 376)

**Issue:** Same root cause as Fix BG (low-OI cleanup). `_cancel_symbol_close_orders()` connected with `clientId=886`, but all close orders are placed by PlaceAnOrder using `clientId=101`. IB only allows the placing clientId to cancel orders. The `ib.cancelOrder()` call didn't throw an exception (ib_insync is async), so DCM logged "cancelled" — but IB silently rejected the cancel. Orders remained active. Confirmed on March 6: CP/CTVA/O orders were in `PendingCancel` status when manually inspected with clientId=101, indicating IB had queued the cancel from clientId=886 but not processed it as authoritative.

**Fix:** Changed `clientId=886` → `clientId=101`. Safe because `_cancel_symbol_close_orders()` is only called from preclose (sequential), disconnects before PlaceAnOrder subprocess starts.

**Impact:** IB accepts the cancel from the placing clientId. Combined with Fix BO polling, the next PlaceAnOrder run sees cleared orders and places the replacement at live prices.
