# Re-Optimization Tradespace + Oct-16 Prediction Ledger

**Recorded:** 2026-09-02 (after the close)
**Purpose:** (1) map what is actually tunable before re-running the genetic optimizer, and
(2) record falsifiable predictions now so the October data can score them instead of being
rationalised after the fact.

**Live trading has been active ~5 weeks** (since 2026-07-27). Everything here should be read with
that sample size in mind.

---

# Part 1 — The Tradespace

## 1.1 Three parameter families; only one is optimized

### A. Pine / genetic optimizer — the only optimized set
`genetic_improved_hugging_optimizer.py`, `param_ranges`:

| Parameter | Range | Current |
|---|---|---|
| `K_OVERSOLD` | 10-30 | 28 |
| `K_OVERBOUGHT` | 70-90 | 76 |
| `MFI_OVERSOLD` | 10-30 | 25 |
| `MFI_OVERBOUGHT` | 70-90 | 87 |
| **`EXIT_BARS`** | **3-15** | **13** |
| **`MIN_SCORE`** | **1-4** | **1** |
| `profitFactor` (ATR x TP) | 1.0-3.0 | 1.17 |
| `stopFactor` (ATR x SL) | 1.0-3.0 | 2.88 |
| `hugBars` | 1-20 | 5 |
| `hugPct` | 0.05-0.33 | 0.72 (**outside the GA range**) |
| `stochLength` | 10-30 | 11 |
| `stochSmooth` | 3-10 | 9 |
| `mfiLength` | 10-30 | 23 |
| `bbLength` | 15-30 | 17 |
| `bbStdDev` | 1.0-3.0 | 1.51 |
| `macdFast` | 8-20 | 17 |
| `macdSlow` | 20-40 | 23 |
| `macdSignal` | 5-15 | 6 |
| `atrLength` | 10-30 | 11 |
| `adxLength` | 10-30 | 26 |
| `adxThreshold` | 20.0-60.0 | 30.0 |

> Note the live `hugPct = 0.72` sits **outside** the optimizer's `(0.05, 0.33)` range, so the
> deployed value was not produced by (and cannot be reproduced by) the current GA configuration.
> At 0.72 the "hugging zone" spans 72% of the band width from each side, i.e. the zones overlap and
> `isHugLower`/`isHugUpper` are true most of the time. Worth reconciling before re-optimizing.

### B. Option structure (`listener.py:568-570`) — never optimized, never backtested
```python
TARGET_DTE = 60          # Fix FC (was 30)
MIN_DTE = 42             # Fix FC (was 21)
MAX_SPREAD_WIDTH = 5.0   # Fix CX: expiry preference only
```
Width is not a parameter at all — the OTM leg is the **adjacent listed strike**, so width is
whatever the chain happens to offer ($1, $2.50, $5, $10).

`TARGET_DTE` is also **a preference, not a cap** (`listener.py:1232-1238`). Live evidence: ALC
entered at **101 DTE**, GLNG at **94 DTE**.

### C. Exit management (`DailyCycleManagement.py`) — never optimized
| Parameter | Value | Line |
|---|---|---|
| `gain_frac` (take-profit) | 0.5 of max profit | `:3555` |
| `loss_frac` (stop-loss) | 0.5 — **disabled** | `:3555`, `:54` |
| `RISK_EXIT_STOP_LOSS_ENABLED` | `False` | `:54` |
| `SALVAGE_EXIT_ENABLED` | `True` | `:66` |
| `SALVAGE_DTE_MAX` | 10 | `:67` |
| `days_old` (min age before exit) | 2 | `:3555` |

## 1.2 The binding problem: the objective function

`compute_fitness` accumulates, at every exit:

```python
pnl = price - call_entry_price        # long
pnl = put_entry_price - price         # short
total_profit += pnl
```

and `evaluate()` returns `compute_fitness(df, indiv)[1]` — the unweighted sum.

**That is raw underlying points on one share.** There is no debit, no width cap, no theta, no DTE,
no commissions, no bid-ask. `pop` (probability of profit) is computed, gated at
`total_trades >= 12`, and then **not optimized on**.

Three consequences that define the whole tradespace:

1. **It rewards moves the structure cannot monetize.** A $1-wide debit spread caps at $1 no matter
   how far the stock runs; the optimizer scores a $10 run as $10. Ten of the sixteen currently-held
   spreads are $1 or $2.50 wide. Parameter sets that win on stock points can be mediocre in spread
   terms — and the GA has been selecting on exactly the wrong axis.
2. **Family B is invisible to it.** DTE, width and debit are not parameters, so re-optimizing A
   **cannot** fix the DTE mismatch — and conversely the 42-60 DTE window has never been validated by
   any backtest.
3. **No risk adjustment.** No drawdown penalty, no per-trade normalization; an unweighted sum
   favours more trades and bigger moves.

### The highest-leverage change before re-running the GA

Replace the fitness with the actual spread payoff:

```python
# calls
payoff = min(max(S_exit - K_long, 0.0), W)
pnl    = payoff - debit - commissions
# puts: payoff = min(max(K_long - S_exit, 0.0), W)
```

`debit` can be modelled (Black-Scholes, and `theo_pricing.py` already has `_bs_price` /
`_theo_spread_debits`) or approximated from the observed **36.8-41.2% of width**. Even the crude
version reorders the search substantially, because capping upside at `W` removes the reward for
the tail moves the current objective chases.

## 1.3 The missing coupling: `EXIT_BARS` <-> `TARGET_DTE`

`maxBars = EXIT_BARS * 2` trading bars, and trading bars -> calendar days is roughly x1.4.

| `EXIT_BARS` | max hold (bars) | max hold (calendar) | matched DTE |
|---|---|---|---|
| 5 | 10 | ~14 d | ~20-25 |
| **8** (≈ observed median cadence) | 16 | ~22 d | **~25-30** |
| **13** (current) | 26 | ~36 d | **~40-45** |
| 15 (GA max) | 30 | ~42 d | ~45-50 |

**At the current `EXIT_BARS = 13`, the matched DTE is ~40-45 — not 60.** Any re-optimization should
carry `TARGET_DTE` out of the result rather than leaving it pinned at 60.

Observed reality that this table has to satisfy (from 1,693 matched OPEN->CLOSE signal pairs
across the full listener history):

| stat | days between OPEN and CLOSE signal |
|---|---|
| median | **8** |
| p75 | 17 |
| p90 | 35 |
| ≤30 days | 89% |

And the actual position holds: median 13 days overall, **9 days** in the 60-DTE era, giving
**hold/DTE = 0.16** (down from 0.42). Cost of the mismatch: **debit/width rose 36.8% -> 41.2%**
while average width *shrank* ($3.42 -> $3.04).

## 1.4 Parameter-by-parameter notes from the data

- **`MIN_SCORE = 1`** is the single biggest lever on trade count. One of four conditions fires an
  entry, and `macdLine > signalLine` alone qualifies — true roughly half the time. `2` would require
  confluence. Expect a large drop in trade count and a very different fitness landscape.
- **`adxThreshold` cannot fix the ADX filter.** No value in `(20, 60)` turns an OR into a trend
  filter (see §1.5). Its two uses also pull in opposite directions: a *higher* threshold is **more**
  permissive at entry (`adx < threshold` true more often) but **less** likely to allow the early
  time-exit (`adx >= threshold`). That tension plausibly explains the mid-range 30 the GA settled on
  — it is a compromise between two contradictory roles, not an optimum for either.
- **`profitFactor` / `stopFactor`** are ATR multiples on the *underlying* with no option
  translation. A 1.17-ATR target may be unreachable in spread terms (the spread caps at width)
  while DCM's own TP (`gain_frac = 0.5` of max profit) fires first. The two exit systems are not
  coherent with each other; check before tuning either.
- **`hugPct = 0.72`** is outside the GA range and makes the hugging zones overlap. Reconcile first.

## 1.5 Structural fixes that are NOT parameters

These are code edits; no amount of GA search reaches them.

**(a) The entry trend filter is an OR that inverts its own intent**
```pine
trendBull = (fastMA >= slowMA) or (adxValue < adxThreshold)
trendBear = (fastMA <= slowMA) or (adxValue < adxThreshold)
```
When ADX < 30 (chop) **both** are true, so the filter disables itself precisely in chop and only
constrains during strong trends.

**(b) The time-exit requires high ADX, so it cannot fire when the trend dies**
```pine
shouldExitDueTime = timeExceeded and ((fastMA < slowMA and adxValue >= adxThreshold) or extendedLimit)
```
At 13 bars you only time-exit if the trend flipped **and** ADX >= 30. If ADX has *dropped* — the
trend died — the early exit cannot fire and the position rides to 26 bars, bleeding in chop.
**The script holds longest precisely when the trend dies.** This is the direct mechanical answer to
"ADX is high when I enter and then drops."

**(c) `TARGET_DTE` should be a cap, not a preference** (`listener.py:1232-1238`) — otherwise
entries like ALC at 101 DTE keep happening.

## 1.6 Suggested order of work

1. Reconcile `hugPct` (live 0.72 vs GA range 0.05-0.33) — the deployed config is not GA-reachable.
2. Fix the objective function to spread payoff (§1.2). **Do this before any re-run.**
3. Fix (a) and (b) in §1.5 — they are structural and cost nothing to try.
4. Add `TARGET_DTE` / `MIN_DTE` to the search, coupled to `EXIT_BARS` via §1.3.
5. Re-run the GA; walk-forward the result rather than trusting in-sample profit.
6. Only then reconsider `MIN_SCORE`, the oscillator bounds, and the ATR multiples.

---

# Part 2 — Oct-16 Prediction Ledger

Recorded **2026-09-02** from live IB portfolio marks and underlying closes. These are predictions
about **system behaviour**, plus one arithmetic baseline. They are explicitly **not** forecasts of
stock prices.

## 2.1 Baseline: the Oct-16 book as measured today

13 of the 16 open spreads expire **2026-10-16**.

| sym | R | long/short | W | entry | mark | spot | moneyness | state if expiry were today |
|---|---|---|---|---|---|---|---|---|
| ABNB | C | 190/195 | 5.0 | 2.15 | 1.68 | 183.26 | -3.5% | worthless |
| BCS | P | 26/25 | 1.0 | 0.32 | 0.34 | 26.20 | -0.8% | worthless |
| CPB | C | 24/25 | 1.0 | 0.40 | 0.39 | 23.78 | -0.9% | worthless |
| MDT | C | 95/97.5 | 2.5 | 0.86 | 0.74 | 92.18 | -3.0% | worthless |
| MET | C | 97.5/100 | 2.5 | 1.25 | 1.01 | 96.42 | -1.1% | worthless |
| NEE | P | 82.5/80 | 2.5 | 0.97 | 0.87 | 83.10 | -0.7% | worthless |
| PR | C | 24/25 | 1.0 | 0.41 | 0.38 | 23.82 | -0.7% | worthless |
| RRC | C | 42/43 | 1.0 | 0.42 | 0.44 | 42.48 | +1.1% | partial (0.48) |
| SCHW | C | 115/120 | 5.0 | 1.18 | 0.87 | 108.24 | -5.9% | worthless |
| STRC | C | 95/100 | 5.0 | 2.75 | 2.50 | 97.07 | +2.2% | partial (2.07) |
| TJX | C | 140/145 | 5.0 | 2.27 | 0.69 | 131.31 | -6.2% | worthless |
| UL | P | 65/62.5 | 2.5 | 0.90 | 0.98 | 64.57 | +0.7% | partial (0.43) |
| WBD | C | 29/30 | 1.0 | 0.44 | 0.37 | 28.39 | -2.1% | worthless |

| Aggregate (Oct-16 only) | |
|---|---|
| total entry debit | **$1,432.99** |
| current mark | $1,125.62 |
| unrealized | **-$307.37** |
| **payoff if spot froze at today's prices** | **$298.00 -> P/L -$1,134.99** |
| both legs OTM now | **10 of 13** |
| partially ITM | 3 (STRC, RRC, UL) |
| both legs ITM (max) | 0 |
| theoretical best case (all max) | +$2,067 |
| theoretical worst case (all worthless) | -$1,433 |

Not on Oct-16: LIN 480/475P (exp 2026-09-18), ALC 75/77.5C and GLNG 50/55C (exp 2026-11-20).

**The number that matters:** the gap between the **-$307 mark** and the **-$1,135 frozen-spot
outcome** is roughly **$827 of extrinsic value that decays to zero by Oct 16** if nothing moves.
That gap — not direction — is what the exit machinery exists to harvest, and it is the cleanest
thing to measure over the next six weeks.

Account context: NetLiquidation $1,774.07, **BuyingPower $156.02**. Over the live window for which
the YTD baseline exists (2026-07-31 -> 2026-09-02): **realized +$231.10**, **NetLiq delta +$148.96**
(`YTD change NetLiq` -600.50 -> -451.54).

## 2.2 Predictions

**P1 — Exits beat expiry.** Realized loss on the Oct-16 book lands materially better than the
-$1,135 frozen-spot baseline, because CLOSE signals (median 8 days) close most positions first.

**P2 — Salvage fires in the first week of October.** Fix FK triggers at `dte <= 10`, i.e. from
**~2026-10-06**, on any Oct-16 spread that is below entry **and** below half its width. **10 of 13
satisfy both tests today.** Expect a visible cluster of `SALVAGE(dte=...)` close orders in the
attempts CSV and `ib_cycle.log`.

**P3 — At most 3 of the 13 actually reach 2026-10-16.** Historically 79.9% of positions close
within 21 days; these are already 7-13 days old.

**P4 — Between 0 and 2 expire worthless.** Materially more means the exit machinery is failing and
the expiry-concentration risk is confirmed.

**P5 — Buying power stays under ~$300 until the Oct-16 book clears**, so Error 201
"Available Funds are insufficient" rejections continue through September (37 already logged).
Count them in the September review.

**P6 — NetLiq, not the estimated ledger, is the scorecard.** Record `YTD change NetLiq` on
2026-09-30 and 2026-10-16. Do **not** sum the "est. P/L from limits" rows — see §2.4.

## 2.3 Decision rule

After 2026-10-16, run `MonthlyReview.py --month 26_09` and `--month 26_10` and score P1-P6.

- **If P2, P3 and P4 hold** — the exit machinery is sound, the DTE question is about *efficiency*
  rather than survival, and re-optimizing with the corrected objective (Part 1) is the next step.
- **If they fail** — fix the exits before touching any parameter. A DTE change cannot rescue a book
  that is not being exited.

## 2.4 Caveat that invalidates the naive scorecard

Both `Health.ps1` ("Last 20 Orders Closed (**est. P/L from limits**)") and `MonthlyReview.py`'s
`section_ledger` derive exit price from the **submitted limit** in the attempts CSV, not the fill.

Worked example, EQNR 2026-09-02:

| source | exit | P/L |
|---|---|---|
| attempts CSV `limit` (what the tools use) | 0.20 | **-$25.00** |
| IB `reqExecutions` (43C sold @2.62, 44C bought @2.14) | **0.48** | — |
| IB `Realized (day)` | — | **-$1.18** |

The trade was flat; -$1.18 is exactly the round-trip commission. A marketable limit fills at the
prevailing bid, so an aggressive `join` limit is a **floor, not a target price**. The error is noisy
in both directions — BEPC's estimate (-$140) landed near its actual (-$153.49 realized 2026-09-01).

**Therefore:** score P1-P6 from IB `Realized (day)` and `YTD change NetLiq`, never from
limit-derived per-trade P/L. Fixing that reporting path is the highest-value follow-up on the
no-code list.
