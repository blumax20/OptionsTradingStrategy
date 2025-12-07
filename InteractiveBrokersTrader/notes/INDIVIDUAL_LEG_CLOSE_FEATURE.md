# Individual Leg Close Fallback Feature

## Summary
Added logic to close individual option legs when combo close fails due to one leg being worthless (< min_limit). This prevents stuck positions when credit/inverted spreads can't be closed as a combo.

## Problem Solved
When closing credit or inverted spreads, IB sometimes rejects combo orders if one leg is near-worthless (e.g., $0.02). This leaves the position stuck with the valuable leg remaining open.

**Example Scenario:**
- Short call spread: Sold 100C @ $2.00, Bought 101C @ $1.00
- Later: 100C is now $0.02 (worthless), 101C is $0.50 (has value)
- Combo close order gets rejected or never fills
- System needs to close the valuable 101C leg individually

## How It Works

### 1. Pre-Check Before Combo Close
When `--fallback-individual-legs` flag is enabled, PlaceAnOrder:

1. **Gets market data** for both legs (long & short strikes)
2. **Checks values**:
   - `long_value` = mid price of long leg
   - `short_value` = mid price of short leg
3. **Determines if combo should be skipped**:
   - If ONE leg < min_limit AND other leg >= min_limit → Skip combo
   - If BOTH legs have value → Try combo as normal
   - If BOTH legs are worthless → Skip (no value to recover)

### 2. Individual Leg Close
When combo is skipped:

1. **Find position quantities** for each leg
2. **Close valuable leg(s) only**:
   - Long leg has value → Place SELL/BUY limit order at mid price
   - Short leg has value → Place SELL/BUY limit order at mid price
3. **Log attempts** with reason "worthless_leg_fallback"

### 3. Flow Diagram

```
Credit Spread Detected (e.g., inverted position)
        ↓
[PlaceAnOrder --mode force-close --fallback-individual-legs]
        ↓
Check market values of both legs
        ↓
  ┌─────────────────────┬─────────────────────┐
  │ Both legs have value│ One leg worthless   │ Both worthless
  ↓                     ↓                     ↓
Try combo close       Skip combo            Skip (no recovery)
  │                     │
  ├─ Success → Done     Close valuable leg(s) individually
  │                     └─ SELL/BUY @ mid price
  └─ Failure → (original behavior, no fallback)
```

## Implementation Details

### Files Modified

#### 1. **PlaceAnOrder.py**

**New Argument** (Line 474):
```python
--fallback-individual-legs
```
Enables the individual leg close fallback when combo fails.

**Pre-Check Logic** (Lines 1755-1812):
```python
if use_fallback:
    # Get market data for both legs
    long_value = _ticker_mid(t_long)
    short_value = _ticker_mid(t_short)

    # Check if one leg is worthless
    long_worthless = (long_value is None or long_value < args.min_limit)
    short_worthless = (short_value is None or short_value < args.min_limit)

    # Skip combo if one leg worthless, other has value
    if (long_worthless and not short_worthless) or (short_worthless and not long_worthless):
        skip_combo = True
```

**Individual Leg Close** (Lines 1849-1948):
```python
if skip_combo and use_fallback:
    # Find position quantities
    for p in ib.positions():
        # Match strikes to get quantities
        ...

    # Close valuable legs with limit orders
    if not long_worthless and abs(long_qty) > 0:
        leg_order = LimitOrder(action, qty, long_value)
        ib.placeOrder(long_opt, leg_order)

    if not short_worthless and abs(short_qty) > 0:
        leg_order = LimitOrder(action, qty, short_value)
        ib.placeOrder(short_opt, leg_order)
```

#### 2. **DailyCycleManagement.py**

**Updated submit_closes_via_place_anorder** (Line 1428):
```python
def submit_closes_via_place_anorder(self,
                                    symbols: list[str] | set[str],
                                    use_live_close: str = "join",
                                    min_limit: float = 0.05,
                                    quiet: bool = True,
                                    fallback_individual_legs: bool = False):  # NEW
```

**Credit Spread Cleanup** (Line 1665):
```python
self.submit_closes_via_place_anorder(
    symbols=credit_syms,
    use_live_close="join",
    min_limit=0.05,
    quiet=True,
    fallback_individual_legs=True,  # Enable fallback for credit spreads
)
```

## Usage

### Manual Testing
```bash
# Test with a specific symbol that has a worthless leg
python PlaceAnOrder.py \
    --mode force-close \
    --symbols XYZ \
    --fallback-individual-legs \
    --use-live-close join \
    --min-limit 0.05 \
    --dry-run
```

### Automatic (Production)
The fallback is automatically enabled for:
- **Credit/inverted spread cleanup** (after-hours daily sweep)
- Triggered when `_detect_credit_or_inverted_spreads()` finds bad positions

## Expected Behavior

### Scenario 1: Both Legs Have Value
```
Long leg: $0.50 (>= $0.05)
Short leg: $0.30 (>= $0.05)
→ Try combo close as normal (no fallback needed)
```

### Scenario 2: One Leg Worthless
```
Long leg: $0.02 (< $0.05) - WORTHLESS
Short leg: $0.45 (>= $0.05) - HAS VALUE
→ Skip combo, close short leg individually @ $0.45
```

### Scenario 3: Both Legs Worthless
```
Long leg: $0.01 (< $0.05)
Short leg: $0.02 (< $0.05)
→ Skip (no value to recover)
```

## Monitoring

### Log Messages
Look for these in the output:

**Pre-Check Detection:**
```
[XYZ] C 100.0/101.0 exp 20260117: one leg worthless (long=$0.02, short=$0.45); will close individual valuable leg(s) only
```

**Individual Leg Close:**
```
[XYZ] Closed individual SHORT C 101.0 exp 20260117 (BUY 1 @ $0.45)
[XYZ] Closed 1 individual leg(s) via fallback
```

### Attempts CSV
Check `attempts_{date}.csv` for:

```csv
symbol,action,status,reason,leg_type,leg_value,limit
XYZ,close_individual_leg,placed,worthless_leg_fallback,short,0.45,0.45
```

## Configuration

### Min Value Threshold
Default: `$0.05` (controlled by `--min-limit`)

- Legs valued < $0.05 are considered worthless
- Legs valued >= $0.05 will be closed individually

To change:
```python
# In DailyCycleManagement.py
self.submit_closes_via_place_anorder(
    symbols=credit_syms,
    min_limit=0.10,  # Raise threshold to $0.10
    fallback_individual_legs=True,
)
```

## Safety Features

1. **Limit Orders Only**: Individual legs are closed with limit orders (not market)
2. **Value Check**: Only closes legs with market value >= min_limit
3. **DAY Orders**: Uses TIF=DAY to prevent overnight holds
4. **Logging**: Full audit trail in attempts CSV
5. **Error Handling**: Graceful failure if individual close fails

## Limitations

- **No guarantee of fills**: Limit orders may not fill if market moves
- **Partial closes**: May close only one leg if the other has no value
- **Market data dependency**: Requires valid bid/ask or last price

## Rollback Plan

If issues arise, disable the fallback:

### Temporary Disable (for specific run)
```bash
# Remove the flag from command line
python PlaceAnOrder.py --mode force-close --symbols XYZ  # No --fallback-individual-legs
```

### Permanent Disable (in code)
```python
# In DailyCycleManagement.py line 1665
self.submit_closes_via_place_anorder(
    symbols=credit_syms,
    fallback_individual_legs=False,  # Disable fallback
)
```

## Future Enhancements

Potential improvements:
1. **Retry logic**: Retry combo close after individual leg fills
2. **Market order fallback**: Use market orders for individual legs at 3 PM
3. **Configurable threshold**: Per-symbol min_limit values
4. **Partial combo**: Try to close both legs if both have some value (even if one is low)

---

**Feature Added**: 2025-12-05
**Files Modified**: PlaceAnOrder.py, DailyCycleManagement.py
**Default**: Enabled for credit/inverted spread cleanup
**Manual**: Use `--fallback-individual-legs` flag
