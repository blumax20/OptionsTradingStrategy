# Close Order Priority Fix - Summary

## Problem
Close orders were being submitted as **market orders** instead of respecting the limit values in `combined_listener_spreads.csv`. This happened because `DailyCycleManagement._submit_close_shared()` was using `--mode force-close` in Stage 1, which bypasses the CSV entirely.

## Root Cause
**File**: `DailyCycleManagement.py:485`

```python
# BEFORE (INCORRECT):
self._run_place_an_order([
    "--mode","force-close",  # ❌ Bypasses CSV, scans positions directly
    "--symbols", sym,
    "--use-live-close","off",
    ...
])
```

The `force-close` mode is designed to scan IB positions directly and place market orders, completely ignoring the CSV signals and their theoretical limits.

## Solution
**Changed**: `DailyCycleManagement.py:485`

```python
# AFTER (CORRECT):
self._run_place_an_order([
    "--mode","from-signal",  # ✅ Respects CSV CLOSE signals with limits
    "--symbols", sym,
    "--use-live-close","off",  # Don't override CSV limits
    ...
])
```

The `from-signal` mode reads the CSV, processes CLOSE signals, and uses the theoretical limit values (`call_debit_theo_*`, `put_debit_theo_*`) as intended.

## How It Works Now

### Correct Priority Flow

```
┌─────────────────────────────────────────────────────────────────┐
│ DailyCycleManagement._submit_close_shared()                     │
└─────────────────────────────────────────────────────────────────┘
                           ↓
        ┌──────────────────────────────────────┐
        │ Check: has_working_close_order(sym)  │
        └──────────────────────────────────────┘
                 ↓                    ↓
            [YES] Skip          [NO] Continue
                                      ↓
        ┌──────────────────────────────────────────────────────┐
        │ STAGE 1: CSV Theoretical Limits                      │
        │ --mode from-signal --use-live-close off              │
        │                                                       │
        │ • Reads: combined_listener_spreads.csv               │
        │ • Finds: CLOSE signals (CALL_CLOSE/PUT_CLOSE)       │
        │ • Limit: call_debit_theo_1 or put_debit_theo_1      │
        │ • Order: LIMIT @ CSV theoretical value               │
        │ • Guard: ib_close_guard prevents duplicates          │
        └──────────────────────────────────────────────────────┘
                           ↓
        ┌──────────────────────────────────────┐
        │ Check: has_working_close_order(sym)  │
        └──────────────────────────────────────┘
                 ↓                    ↓
         [YES] Success!       [NO] Continue to Stage 2
                                      ↓
        ┌──────────────────────────────────────────────────────┐
        │ STAGE 2: Force-Close with Live Join Quotes (Fallback)│
        │ --mode force-close --use-live-close join            │
        │                                                       │
        │ • Scans: IB positions directly (CSV-independent)     │
        │ • Finds: Vertical spreads from positions             │
        │ • Limit: bid(long) - ask(short) for SELL close      │
        │ • Order: LIMIT @ live join price                     │
        │ • Guard: ib_close_guard prevents duplicates          │
        └──────────────────────────────────────────────────────┘
                           ↓
                     [If still no fill]
                           ↓
        ┌──────────────────────────────────────────────────────┐
        │ STAGE 3: Pre-Close Sweep (3 PM ET) - Final Fallback │
        │ _pre_close_market_conversion()                       │
        │                                                       │
        │ • Cancels: Low-OI limit orders                       │
        │ • Replaces: MARKET order (guaranteed fill)           │
        │ • Timing: 3:00 PM - 3:30 PM ET                       │
        └──────────────────────────────────────────────────────┘
```

## Changed Files

### 1. DailyCycleManagement.py
**Lines 484-492** (Stage 1):
- Changed: `--mode force-close` → `--mode from-signal`
- Added: `--date` parameter to ensure correct dated CSV directory
- Comment updated to clarify CSV-based limit usage

**Lines 517-525** (Stage 2):
- Added: `--date` parameter to ensure correct dated CSV directory
- Comment updated to clarify this is the fallback

## Testing Instructions

### 1. Dry Run Test (Safe)
```bash
cd /path/to/InteractiveBrokersTrader

# Create a test CSV with CLOSE signals
python3 verify_close_fix.py

# Or manually test PlaceAnOrder with a real CSV
python3 PlaceAnOrder.py \
    --mode from-signal \
    --csv "/path/to/combined_listener_spreads.csv" \
    --symbols XYZ,ABC \
    --dry-run \
    --verbose
```

**Expected Output**:
- Should show LIMIT orders being prepared
- Should display theoretical limit values from CSV
- Should NOT show market orders (unless testing force-close mode)

### 2. Live Test (With Real IB Connection)
```bash
# Run the daily cycle (monitors all stages)
python3 DailyCycleManagement.py
```

**Monitor**:
1. **Log Output**: Look for "delegated_csv_limit" in attempt logs
2. **Attempts CSV**: Check `C:\OptionsHistory\{date}\attempts_{date}.csv`
   - Stage 1: `reason=...delegated_csv_limit_working`
   - Stage 2: `reason=...fallback_live_join` (only if Stage 1 failed)
3. **IB TWS**: Check order panel for LIMIT orders (not MARKET)

### 3. Verify Guard Works
```bash
# First run: Should place LIMIT order
python3 PlaceAnOrder.py --mode from-signal --symbols XYZ

# Second run: Should skip (guard detects existing order)
python3 PlaceAnOrder.py --mode from-signal --symbols XYZ
# Expected: "close-guard: existing working AUTO_CLOSE; skipping"
```

## Expected Behavior Changes

### Before Fix ❌
1. Stage 1 used `force-close` → bypassed CSV
2. Always placed market orders (or market-like limits)
3. CSV theoretical limits were ignored
4. Priority was: market → join → mid (backwards)

### After Fix ✅
1. Stage 1 uses `from-signal` → reads CSV
2. Places LIMIT orders using CSV theoretical values
3. CSV limits are respected first
4. Priority is: **CSV limits → join quotes → market (3 PM only)**

## Monitoring Checklist

After deploying this fix, monitor for:

- [ ] **Limit orders appear in IB TWS** (not market orders)
- [ ] **Attempts CSV shows correct reasons**:
  - `delegated_csv_limit_working` (Stage 1 success)
  - `fallback_live_join` (Stage 2 only if Stage 1 fails)
- [ ] **No duplicate close orders** (guard prevents this)
- [ ] **Close fills happen** (limits are reasonable)
- [ ] **3 PM sweep converts stubborn limits to market** (existing behavior preserved)

## Rollback Plan (If Needed)

If issues arise, revert the change:

```bash
# Edit DailyCycleManagement.py line 485
# Change back to: "--mode","force-close",

# Or use git:
git diff DailyCycleManagement.py  # Review changes
git checkout DailyCycleManagement.py  # Revert to previous version
```

## Additional Notes

- The `ib_close_guard.py` guard still prevents duplicate orders
- The 3 PM pre-close sweep logic is unchanged
- Stage 2 fallback still works for edge cases (e.g., CSV missing for a symbol)
- This fix only affects **CLOSE** orders; OPEN orders are unchanged

## Questions?

- Check verification output: `python3 verify_close_fix.py`
- Review test framework: `python3 test_close_order_priority.py`
- Examine log files: `C:\OptionsHistory\{date}\attempts_{date}.csv`

---

**Fix Applied**: 2025-12-05
**Files Modified**: DailyCycleManagement.py (lines 485, 517)
**Verified**: All code checks passed ✓
