# Iron Condor Feasibility & Design Doc

**Date:** 2026-09-02
**Status:** NOT RECOMMENDED NOW — documented so the cost is known, not so it gets built.
**Trigger:** The question "would an iron condor make sense for positions where ADX is high at entry
but drops shortly after?"

---

## 1. The motivating intuition, and why it points somewhere else

The intuition is sound in isolation: if a directional entry is followed by the trend dying, a
long debit vertical bleeds while a short-premium structure (condor) profits from the resulting
range. The problem is that this system cannot identify those entries, cannot hold that structure,
and cannot fund it.

More importantly, the underlying complaint has a **cheaper and more direct cause** in the Pine
strategy itself — see §7. Fixing that targets the same intent at near-zero cost.

---

## 2. Verdict

Not feasible without a substantial rewrite spanning the listener, the order builder, the
reconcile, the risk exits, the daily credit sweep, and the entire pricing/CSV/reporting stack.
Eight independent blockers, each of which alone is sufficient to break it.

Separately, the account cannot fund one today: **BuyingPower $156.02** against NetLiquidation
$1,774.07 (2026-09-02), because long options carry no Equity-with-Loan-Value.

---

## 3. The eight blockers

### B1 — Every BAG in the repo is hardcoded to exactly 2 legs

`PlaceAnOrder.py:1281`
```python
combo.comboLegs = [leg_long, leg_short]
```
Two named locals, one literal list. No loop, no leg-list parameter, no ratio other than 1. Same
shape at four other sites: `DailyCycleManagement.py:1706`, `DailyCycleManagement.py:2748`,
`OrderPlacementSystem.py:26`, `fix_jan23_orders.py:148`.

Worse, `place_debit_spread` *force-canonicalizes* whatever strikes it is handed into a debit
(`PlaceAnOrder.py:1212`):
```python
# --- Canonicalize leg orientation: treat combo as a long debit vertical ---
if right.upper() == "C":
    if a > b: a, b = b, a      # ensure a is the lower strike
else:  # "P"
    if a < b: a, b = b, a      # ensure a is the higher strike
```
Hand it credit-spread strikes and it silently swaps them into a debit.

**Required:** a new leg-list order builder accepting N legs with per-leg action/ratio, plus a
*signed* limit price so a net credit is representable. `place_debit_spread` cannot be extended in
place without destabilising every existing caller.

### B2 — The signal carries no regime data at all

`listener.py:630` (`_parse_signal_fields`) is two regexes over the raw alert text:
- `order\s+(buy|sell)` -> `signal_side` (cosmetic; nothing branches on it)
- `new\s+strategy\s+position\s+is\s*([+-]?)\s*(\d+)` -> `strategy_position` -> `signal_type`

The live alert is TradingView's default strategy placeholder, e.g.
```
Adaptive Hugging Combined Debit Spread Strategy (200, 25, 87, ...): order buy @ 1 filled on EQNR. New strategy position is 1
```
`+N` -> CALL_OPEN, `-N` -> PUT_OPEN, `0` -> CLOSE. That is the entire signal grammar.

ADX, ATR, BB width and every other indicator exist **only** in the offline optimizer
(`genetic_improved_hugging_optimizer.py`) and in the `.pine` sources. There is **zero** occurrence
of `adx` in `listener.py`, `PlaceAnOrder.py`, or `DailyCycleManagement.py`. Any JSON keys beyond
`ticker`/`message` are logged raw to the webhook-events file and then discarded.

**Required:** extend the Pine `alert_message` to emit a regime field (ADX value, ADX slope, or a
categorical), extend `_parse_signal_fields` to parse it, add a CSV column, and add a branch that
chooses condor-vs-vertical. Today there is literally no field that could gate the decision.

### B3 — The reconcile dismembers a condor on the next cycle

`DailyCycleManagement.py:3429` and neighbours:
```python
if latest_open_sign == +1 and has_put_vert:
    should_close = True
    reason = "reconcile_flip_put_to_call"
    side_to_close = "put"
elif latest_open_sign == -1 and has_call_vert:
    should_close = True
    reason = "reconcile_flip_call_to_put"
    side_to_close = "call"
```
A condor is `has_call_vert=True and has_put_vert=True`. Whatever the last signal was, one of the
first two branches fires and closes that wing. **There is no state in which both wings are held
legitimately.** Within one cycle of establishing a condor you hold a single vertical.

The sign-determination block just above it is also incoherent for a delta-neutral structure: with
both wings present it picks a direction by comparing call-notional to put-notional, which is
arbitrary and flips day to day.

**Required:** a position-intent concept (this symbol is deliberately two-sided) threaded through
the reconcile, plus exemptions in the flip branches.

### B4 — The opposite-side unwind blocks the second wing at open time

`PlaceAnOrder.py` CALL_OPEN/PUT_OPEN handlers (Fix AA1). Before opening a call the handler
force-unwinds any put side, and if the unwind returns 0 spreads closed it **blocks the open**
(`opposite_unwind_failed`). A call and a put wing cannot coexist by construction: the system either
kills the existing side or refuses to place the new one.

**Required:** an exemption path so a deliberate second wing is not treated as an opposite-side
position needing unwinding.

### B5 — A daily sweep force-closes short verticals as defects

`DailyCycleManagement.py:318` — `_detect_credit_or_inverted_spreads()`, run in the after-hours
cycle. Anything not shaped like `long < short` (calls) is detected and added to the force-close
candidate set. The two short wings of a condor are exactly that shape.

`PlaceAnOrder.py`'s `orientation = "short_credit"` handling is likewise recognition-for-unwinding,
never an opening strategy. **A credit spread in this account is treated as a defect to be swept.**

**Required:** the same position-intent concept from B3, honoured by the sweep.

### B6 — Risk exits cannot see short verticals, and the TP/SL/salvage math is debit-shaped

`DailyCycleManagement.py:4084` / `:4093` pair legs with a hardcoded debit shape:
```python
if l1["qty"] > 0 and l2["qty"] < 0:      # long lower strike, short higher (calls)
```
A short vertical never matches, so a condor's credit wings are **invisible to TP, stop-loss and
salvage** entirely.

And the decision math assumes a positive debit converging toward `width`:
```python
stop_hit = curr <= (1.0 - loss_frac) * entry
tp_hit   = curr >= entry + gain_frac * max(0.0, (width - entry))
```
For a credit spread `entry` is negative and both expressions are nonsense.

Current settings for reference: `RISK_EXIT_STOP_LOSS_ENABLED = False`
(`DailyCycleManagement.py:54`), `SALVAGE_EXIT_ENABLED = True` / `SALVAGE_DTE_MAX = 10` (`:66-67`),
`gain_frac = 0.5`, `loss_frac = 0.5`, `days_old = 2`.

**Required:** a credit-aware branch in `_process_vertical` with its own P/L convention, plus
pairing that recognises short verticals.

### B7 — The pricing, CSV and reporting stack is debit-only

`listener.py:625`
```python
out[f"call_debit_theo_{key}"] = min(0.75 * W, max(0.0, float(call_long - call_short)))
out[f"put_debit_theo_{key}"]  = min(0.75 * W, max(0.0, float(put_long - put_short)))
```
The `max(0.0, ...)` and `min(0.75*W, ...)` clamps encode the debit-only assumption — a credit
spread's value is not representable. There is no `*_credit_*` column anywhere in the CSV header
(`listener.py:840-856`), and `MonthlyReview.py`'s ledger is keyed on a 5-tuple with exactly one
long and one short strike.

**Required:** credit columns end to end, a 4-leg ledger key, and width-bucket handling for a
two-wing structure.

### B8 — IB permission to *open* a credit spread is unverified

All the evidence in this repo shows only that a BAG **SELL works as a close**, which IB classifies
differently from opening a short position. The recorded rejections split two ways in
`ib_cycle.log`:

- 37x `We are unable to accept your order. Your Available Funds are insufficient ... your Equity
  with Loan Value [-NNN USD] must exceed the new total Initial Margin` — margin.
- 8x `You are not able to submit this order because you do not have trading permissions for this
  options strategy` — permissions, on **individual SELL legs** (the Fix CK path, e.g.
  `2026-08-14 [UL] Fix BS: SELL 66.0 rejected async`).

**Verification step before any build** (read-only, never transmitted):
```python
# ib.whatIfOrder() sets whatIf=True; IB returns margin impact and does NOT place the order.
res = ib.whatIfOrder(condor_bag_contract, LimitOrder("SELL", 1, credit))
print(res.initMarginChange, res.maintMarginChange, res.equityWithLoanChange)
```
A rejection or an error here answers the permission question definitively and costs nothing. Do
this **first** — it can invalidate the whole project in one call.

---

## 4. Capital prerequisite

| Measure | 2026-09-02 |
|---|---|
| NetLiquidation | $1,774.07 |
| **BuyingPower / EquityWithLoanValue** | **$156.02** |
| ExcessLiquidity | $156.02 |
| Cushion | 0.088 |
| OptionMarketValue | $1,618.05 |
| Open spreads | 16 (13 expiring 2026-10-16) |

A defined-risk short vertical requires `(width x 100) - credit` of margin. Even a $1-wide wing at
~$0.35 credit needs ~$65, and a condor is two wings. With $156 of buying power and every open above
~$150 already being rejected, there is no room. **Fund the account and clear the October book
before this is even a question.**

---

## 5. Cheaper intermediate (still not recommended)

Keep the 2-leg engine and hold **two independent positions per symbol** keyed on
`(symbol, right)` rather than building one 4-leg BAG. `_iter_spread_pairs_from_positions` already
keys on `(exp, right)`, so it would yield the two verticals separately and close them separately.

This still requires fixing **B3, B4, B5 and B6** — the reconcile flip, the opposite-side unwind,
the credit sweep and the risk-exit pairing — and it is not really a condor:

- two separate fills instead of one net-credit order
- doubled commissions
- no IB margin offset between the wings (each margined standalone)
- no single price to manage or close against

The only thing it saves is B1 and part of B7.

---

## 6. Rough cost

| Item | Scope |
|---|---|
| B1 leg-list order builder + signed limit | new function, ~150 lines, plus tests |
| B2 Pine alert + parser + CSV column + branch | touches the live listener; needs a restart |
| B3 position-intent through the reconcile | invasive; the reconcile is the most fix-dense code in the repo |
| B4 unwind exemption | small, but interacts with Fix AA1/AJ2 |
| B5 sweep exemption | small |
| B6 credit-aware risk exits | moderate; new P/L convention |
| B7 credit columns end to end | listener + LiquidityFilter + PlaceAnOrder + DCM + MonthlyReview |
| B8 IB permission verification | one read-only call — **do this first** |

Realistically a multi-week project touching every subsystem, on a codebase whose reconcile logic
already carries ~90 numbered fixes.

---

## 7. The cheaper thing that targets the same intent

The complaint is "the trend dies after I enter and the position bleeds." That is a **real bug in
the Pine exit logic**, not a missing option structure.

`Hugging_debit_spread_single.pine`:
```pine
shouldExitDueTime = timeExceeded and ((fastMA < slowMA and adxValue >= adxThreshold) or extendedLimit)
```
`timeExceeded` is `EXIT_BARS` (13) bars; `extendedLimit` is `maxBars = EXIT_BARS * 2` (26) bars.
At 13 bars the position only time-exits if the trend flipped **and ADX >= 30**. If ADX has
*dropped* — the trend died, exactly the described case — the early exit cannot fire and the
position rides to 26 bars, bleeding in chop.

**The script holds longest precisely when the trend dies.**

Related, in the entry filter:
```pine
trendBull = (fastMA >= slowMA) or (adxValue < adxThreshold)
trendBear = (fastMA <= slowMA) or (adxValue < adxThreshold)
```
This is an **OR**. When ADX < 30 both are true, so the filter disables itself in chop and only
constrains during strong trends — the opposite of a trend filter. Combined with `MIN_SCORE = 1`
(one of four conditions is enough; `macdLine > signalLine` alone qualifies), entries concentrate in
low-ADX conditions.

Neither is fixable by re-optimizing `adxThreshold` — no value in the GA's `(20.0, 60.0)` range
turns an OR into an AND. Both are code changes on the TradingView side, and they cost nothing to
try. See `reoptimization-tradespace-and-oct16-predictions.md`.

---

## 8. Recommendation

1. **Do not build the condor now.**
2. If curiosity persists, spend one read-only `ib.whatIfOrder()` call (B8) to learn whether the
   account could ever open a credit spread. That is the cheapest possible next step.
3. Fix the Pine exit condition in §7 first and measure it — it addresses the actual complaint.
4. Revisit only after the account is funded well past the $25k PDT floor and the exit machinery has
   been validated against the October book.
