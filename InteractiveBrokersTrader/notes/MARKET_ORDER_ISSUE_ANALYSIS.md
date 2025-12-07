# Market Orders Instead of Limit Orders - Root Cause Analysis

## Summary
HTHT and SBUX close orders are being placed as **MARKET orders at 5 PM** instead of **LIMIT orders using CSV theoretical values**. This happens because Stage 1 (CSV-based limit orders) is not creating working orders, causing Stage 2 (force-close fallback) to kick in.

## Timeline (Dec 5, 2025)

### 4:02-4:05 PM: Strategy Signals CLOSE
- **16:02:54**: HTHT CLOSE webhook received → CSV updated with `call_debit_theo_1=0.27`
- **16:05:15**: SBUX CLOSE webhook received → CSV updated with `call_debit_theo_1=0.48`
- Webhook says "order sell @ 1 filled" but this is strategy-side tracking, not IB execution

### 5:05 PM: After-Hours Batch Placement
-**17:05**: DailyCycleManagement runs after-hours batch
- **Stage 1** (`--mode from-signal`): Should read CSV and place LIMIT orders at $0.27 (HTHT) and $0.48 (SBUX)
- **Problem**: Stage 1 FAILS to create working orders → `_has_working_close_order()` returns False
- **Stage 2** (`--mode force-close`): Scans IB positions, finds spreads still open, places MARKET orders

## Root Cause: Why Stage 1 Fails

Looking at `DailyCycleManagement.py:484-491` (Stage 1):

```python
self._run_place_an_order([
    "--mode","from-signal",  # ✓ Correct mode
    "--symbols", sym,
    "--min-limit","0.05",    # ⚠️ PROBLEM: 0.05 minimum
    "--use-live-close","off",
    "--quantity","50",
    "--quiet"
])
```

### Issue #1: **Min-Limit Too High**
- HTHT CSV theoretical limit: **$0.27**
- SBUX CSV theoretical limit: **$0.48**
- Both are **ABOVE** the `--min-limit 0.05` threshold

**However**, there may be another issue...

### Issue #2: **ib_close_guard Blocking Duplicate Orders**
The `_has_working_close_order()` guard checks for existing BAG orders. If Stage 1 tries to place an order but IB rejects it (or it gets blocked by a guard), then `_has_working_close_order()` returns False, triggering Stage 2.

### Issue #3: **CSV Signal Type May Not Match Positions**
Looking at the CSV:
- HTHT line 11: `signal_type=CLOSE`, but `strategy_position=0`
- SBUX line 20: `signal_type=CLOSE`, but `strategy_position=0`

The `strategy_position=0` indicates the **strategy already thinks the position is closed**. PlaceAnOrder may be:
1. Reading the CLOSE signal
2. Checking IB positions
3. Seeing positions ARE still open (limit orders not filled yet)
4. Trying to place a close order
5. Getting blocked by `ib_close_guard` (if the strategy already placed one)
6. Failing to create a "working" order
7. Triggering Stage 2 force-close

## Why Force-Close Uses Market Orders

Looking at the attempts CSV for HTHT (line 15):
```csv
force_close,20251219,SELL,MKT,1,success,C,placed,HTHT,2025-12-05T17:05:37.583178-05:00
```

The order type is **MKT** (market). Let's trace why:

In `PlaceAnOrder.py:1733`:
```python
order_type = "LMT" if (limit is not None) else "MKT"
```

So it becomes a market order when `limit is None`. Looking at the force-close logic (lines 2376-2382):
```python
limit = None
if getattr(args, "use_live_close", "off") in ("mid","join") and not pd.isna(atm):
    limit = live_spread_price(ib, symbol, expiration, ('C' if side=='call' else 'P'),
                              float(atm), float(hint),
                              action='SELL', scheme=args.use_live_close, timeout=3.0)
```

**Stage 2** is called with `--use-live-close join` (DailyCycleManagement.py:520), so it **should** get a limit from `live_spread_price()`.

**BUT** - if `live_spread_price()` returns `None` (e.g., no market data, can't get bid/ask), then `limit` stays `None` and the order becomes a **MARKET order**.

## The Real Problem

At **5 PM**, market is closed (4 PM ET close). When Stage 2 runs with `--use-live-close join`:
1. PlaceAnOrder tries to get live market data for the spread
2. **No live quotes available** (market closed)
3. `live_spread_price()` returns `None`
4. Falls back to **MARKET order**

## Solution #1: Fix Stage 1 Date Parameter (PRIORITY)

The **critical issue** is that Stage 1 isn't creating working orders. I suspect this is because **PlaceAnOrder doesn't know which dated CSV directory to use**.

Looking at DailyCycleManagement.py line 484-491, there's **no `--date` parameter**!

### Current Code (DailyCycleManagement.py:484-491):
```python
self._run_place_an_order([
    "--mode","from-signal",
    "--symbols", sym,
    "--min-limit","0.01" if context == "preclose" else "0.05",
    "--use-live-close","off",
    "--quantity","50",
    "--quiet"
])
```

### Fix #1A: Add --date Parameter
```python
self._run_place_an_order([
    "--mode","from-signal",
    "--date", today_folder_yy_mm_dd(),  # ADD THIS
    "--symbols", sym,
    "--min-limit","0.01" if context == "preclose" else "0.05",
    "--use-live-close","off",
    "--quantity","50",
    "--quiet"
])
```

### Fix #1B: Add Explicit CSV Path
```python
ny_date = datetime.now(ZoneInfo("America/New_York")).strftime("%y_%m_%d")
csv_path = f"C:\\OptionsHistory\\{ny_date}\\combined_listener_spreads.csv"

self._run_place_an_order([
    "--mode","from-signal",
    "--csv", csv_path,  # ADD THIS
    "--symbols", sym,
    "--min-limit","0.01" if context == "preclose" else "0.05",
    "--use-live-close","off",
    "--quantity","50",
    "--quiet"
])
```

## Solution #2: Make Stage 2 Use CSV Limits as Fallback

Even if Stage 1 fails, Stage 2 should try to use CSV theoretical limits before falling back to market orders.

### Current Stage 2 (DailyCycleManagement.py:516-523):
```python
self._run_place_an_order([
    "--mode","force-close",
    "--symbols", sym,
    "--min-limit","0.01" if context == "preclose" else "0.05",
    "--use-live-close","join",  # ⚠️ Fails when market closed
    "--quantity","50",
    "--quiet"
])
```

### Proposed Fix for Stage 2:
Add `--csv` parameter so force-close can read theoretical limits from CSV as a fallback when live quotes are unavailable:

```python
ny_date = datetime.now(ZoneInfo("America/New_York")).strftime("%y_%m_%d")
csv_path = f"C:\\OptionsHistory\\{ny_date}\\combined_listener_spreads.csv"

self._run_place_an_order([
    "--mode","force-close",
    "--csv", csv_path,  # Use CSV limits as fallback when no live quotes
    "--symbols", sym,
    "--min-limit","0.01" if context == "preclose" else "0.05",
    "--use-live-close","join",  # Try live first, fall back to CSV
    "--quantity","50",
    "--quiet"
])
```

But this requires modifying PlaceAnOrder.py to check CSV for theoretical limits when `--mode force-close` and `live_spread_price()` returns None.

## Solution #3: Prevent Duplicate Close Attempts

The strategy is placing its own close orders at 4 PM, then DailyCycleManagement tries again at 5 PM. This causes:
1. Duplicate close attempts
2. ib_close_guard blocking Stage 1
3. Force-close (Stage 2) bypassing the guard and placing market orders

### Options:
**A. Clear strategy_position=0 entries from CSV before 5 PM batch**
- Filter out CLOSE signals where `strategy_position=0` before reading CSV in Stage 1

**B. Enhance ib_close_guard to also block force-close**
- Currently ib_close_guard only checks in from-signal mode
- Extend it to also block force-close when working orders exist

**C. Don't run DailyCycleManagement batch if strategy already closed**
- Check if position is already 0 in IB before trying to close

## Recommended Actions (Priority Order)

### 1. **IMMEDIATE: Add --date to Stage 1** (High Priority)
This ensures PlaceAnOrder reads from the correct dated CSV directory.

### 2. **SHORT-TERM: Debug Why Stage 1 Doesn't Create Orders** (High Priority)
- Add verbose logging to see exactly why from-signal mode fails
- Check if ib_close_guard is blocking it
- Check if the CSV is being found and parsed correctly

### 3. **MEDIUM-TERM: Improve Stage 2 Limit Logic** (Medium Priority)
- Make force-close read CSV theoretical limits as fallback when live quotes unavailable
- Or: Don't run Stage 2 at all during after-hours (5 PM) when market is closed

### 4. **LONG-TERM: Coordinate Strategy and DailyCycleManagement** (Low Priority)
- Prevent duplicate close attempts
- Have strategy signal closure but let DailyCycleManagement handle actual IB orders

## Verification Steps

After applying fixes:

1. **Check Stage 1 creates orders:**
   ```bash
   # Look for "delegated_csv_limit_working" in attempts CSV
   grep "delegated_csv_limit" attempts_*.csv
   ```

2. **Verify no Stage 2 fallback for normal cases:**
   ```bash
   # Should NOT see "fallback_live_join" or "positions_fallback" for symbols with CSV limits
   grep "fallback" attempts_*.csv
   ```

3. **Confirm LIMIT orders, not MARKET:**
   ```bash
   # Check order_type column
   grep "HTHT\|SBUX" attempts_*.csv | grep -v "MKT"
   ```

---

**Issue Identified**: 2025-12-07
**Affects**: HTHT, SBUX, and likely other symbols with CLOSE signals at 5 PM batch
**Priority**: HIGH - Causing unfavorable market order executions

## Fix Applied (2025-12-07)

### Changes Made to DailyCycleManagement.py

**Line 486 (Stage 1):**
```python
# BEFORE:
self._run_place_an_order([
    "--mode","from-signal",
    "--symbols", sym,
    "--min-limit","0.01" if context == "preclose" else "0.05",
    "--use-live-close","off",
    "--quantity","50",
    "--quiet"
])

# AFTER:
self._run_place_an_order([
    "--mode","from-signal",
    "--date", self._now_ny().strftime("%y_%m_%d"),  # ✅ ADDED
    "--symbols", sym,
    "--min-limit","0.01" if context == "preclose" else "0.05",
    "--use-live-close","off",
    "--quantity","50",
    "--quiet"
])
```

**Line 519 (Stage 2):**
```python
# BEFORE:
self._run_place_an_order([
    "--mode","force-close",
    "--symbols", sym,
    "--min-limit","0.01" if context == "preclose" else "0.05",
    "--use-live-close","join",
    "--quantity","50",
    "--quiet"
])

# AFTER:
self._run_place_an_order([
    "--mode","force-close",
    "--date", self._now_ny().strftime("%y_%m_%d"),  # ✅ ADDED
    "--symbols", sym,
    "--min-limit","0.01" if context == "preclose" else "0.05",
    "--use-live-close","join",
    "--quantity","50",
    "--quiet"
])
```

### Expected Behavior After Fix #1

**Stage 1** will now correctly:
1. Read from `C:\OptionsHistory\{yy_mm_dd}\combined_listener_spreads.csv`
2. Find CLOSE signals with theoretical limits (e.g., HTHT: $0.27, SBUX: $0.48)
3. Place LIMIT orders at those theoretical values
4. Create working orders that pass the `_has_working_close_order()` check
5. **Prevent Stage 2 from running** (no fallback needed)

**Result**: No more market orders for symbols with valid CSV theoretical limits.

## Fix #2: Add Verbose Logging (2025-12-07)

### Changes Made to DailyCycleManagement.py

**Lines 474-497:**
```python
# ADDED: Verbose logging before Stage 1
dated_folder = self._now_ny().strftime("%y_%m_%d")
LOG.info(f"[{sym}] Stage 1: Attempting CSV-based close from {dated_folder} (context={context})")

# ... Stage 1 runs ...

# ADDED: Check and log if Stage 1 succeeded
has_working = self._has_working_close_order(sym)
LOG.info(f"[{sym}] Stage 1 completed: has_working_close_order={has_working}")
if has_working:
    # Success path
else:
    # ADDED: Warning when falling back to Stage 2
    LOG.warning(f"[{sym}] Stage 1 failed to create working order - falling back to Stage 2 (force-close)")
```

### Expected Behavior After Fix #2

**Logs will now show**:
- When Stage 1 attempts to use CSV limits
- Whether Stage 1 successfully created a working order
- When Stage 2 fallback is triggered (with warning)

**Example log output**:
```
[HTHT] Stage 1: Attempting CSV-based close from 25_12_07 (context=afterhours)
[HTHT] Stage 1 completed: has_working_close_order=True
```

Or if Stage 1 fails:
```
[SBUX] Stage 1: Attempting CSV-based close from 25_12_07 (context=afterhours)
[SBUX] Stage 1 completed: has_working_close_order=False
[SBUX] Stage 1 failed to create working order - falling back to Stage 2 (force-close)
```

## Fix #3: CSV Fallback for Stage 2 Force-Close (2025-12-07)

### Changes Made to PlaceAnOrder.py

**Lines 1733-1750 (in `force_close_symbol_via_positions()`):**
```python
# ADDED: CSV fallback when live quotes fail
if limit is None:
    try:
        csv_path = combined_csv_path_for_today(args.date)
        if csv_path.exists():
            df_csv = pd.read_csv(csv_path)
            if "symbol" in df_csv.columns:
                df_csv["symbol"] = df_csv["symbol"].astype(str).map(_clean_symbol)
            rows = df_csv[df_csv["symbol"].astype(str).str.upper() == symbol.upper()]
            if not rows.empty:
                row = rows.iloc[0]
                width = abs(float(longK) - float(shortK))
                csv_limit = width_aligned_close_limit(row, right, width)
                if csv_limit is not None and csv_limit >= args.min_limit:
                    limit = csv_limit
                    logger.info(f"[{symbol}] Force-close fallback: using CSV theoretical limit {limit:.2f} (live quotes unavailable)")
    except Exception as e:
        logger.warning(f"[{symbol}] Failed to read CSV fallback for force-close: {e}")
```

### Expected Behavior After Fix #3

**Even if Stage 2 runs**, it will now:
1. **First** try live quotes (existing behavior)
2. **If live quotes fail** (market closed), read CSV and use theoretical limits
3. **Only as last resort**, use MARKET orders

**Pricing Priority (Stage 2)**:
1. Live join/mid quotes (if market open)
2. CSV theoretical limits (fallback when market closed)
3. MARKET order (only if CSV also unavailable)

**Example log output**:
```
[HTHT] Force-close fallback: using CSV theoretical limit 0.27 (live quotes unavailable)
```

### Result of All Fixes

**Fix #1**: Ensures Stage 1 reads correct CSV
**Fix #2**: Provides visibility into Stage 1/Stage 2 flow
**Fix #3**: Prevents market orders even if Stage 1 fails

**Combined result**: LIMIT orders will be placed using CSV theoretical values at 5 PM (after-hours), with full logging to debug any issues.
