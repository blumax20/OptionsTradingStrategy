#!/usr/bin/env python3
"""
Backfill theoretical spread debit values in combined_listener_spreads.csv
Uses Black-Scholes to calculate theoretical values for rows missing them.
"""
import csv
import math
import os
import sys
from datetime import datetime

DEFAULT_SIGMA = 0.25  # Default IV if not available
DEFAULT_R = 0.045     # Risk-free rate

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _bs_price(S: float, K: float, T: float, r: float, sigma: float, call: bool = True) -> float:
    if T <= 0 or sigma <= 0:
        intrinsic = max(S - K, 0.0) if call else max(K - S, 0.0)
        return intrinsic
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    if call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

def _theo_spread_debits(S: float, atm: float, T: float, sigma: float, r: float = DEFAULT_R, widths=(1.0, 2.5, 5.0)) -> dict:
    out = {}
    for W in widths:
        call_long = _bs_price(S, atm, T, r, sigma, call=True)
        call_short = _bs_price(S, atm + W, T, r, sigma, call=True)
        put_long  = _bs_price(S, atm, T, r, sigma, call=False)
        put_short = _bs_price(S, max(atm - W, 0.01), T, r, sigma, call=False)
        key = "2_5" if abs(W - 2.5) < 1e-9 else str(int(W))
        out[f"call_debit_theo_{key}"] = round(call_long - call_short, 4)
        out[f"put_debit_theo_{key}"]  = round(put_long - put_short, 4)
    return out

def _parse_float(x):
    try:
        v = float(x)
        if v != v:  # NaN
            return None
        return v
    except Exception:
        return None

def _is_empty(val):
    return val is None or val == '' or (isinstance(val, float) and math.isnan(val))

def backfill_csv(csv_path: str, dry_run: bool = False):
    """Read CSV, backfill missing theo values, and write back."""
    if not os.path.exists(csv_path):
        print(f"CSV not found: {csv_path}")
        return

    rows = []
    fieldnames = None
    updated_count = 0

    with open(csv_path, 'r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames

        # Ensure theo columns exist
        theo_cols = ['call_debit_theo_1', 'put_debit_theo_1',
                     'call_debit_theo_2_5', 'put_debit_theo_2_5',
                     'call_debit_theo_5', 'put_debit_theo_5']
        for col in theo_cols:
            if col not in fieldnames:
                fieldnames = list(fieldnames) + [col]

        for row in reader:
            # Check if theo values are missing
            needs_backfill = any(_is_empty(row.get(col)) for col in theo_cols)

            if needs_backfill:
                S = _parse_float(row.get('current_price'))
                atm = _parse_float(row.get('atm_strike'))
                days = _parse_float(row.get('days_to_exp'))
                iv = _parse_float(row.get('iv_atm'))

                if S and atm and days and days > 0:
                    T = days / 365.0
                    sigma = iv if iv else DEFAULT_SIGMA

                    theo = _theo_spread_debits(S, atm, T, sigma)

                    # Only fill in if currently empty
                    for col in theo_cols:
                        if _is_empty(row.get(col)) and col in theo:
                            row[col] = theo[col]
                            updated_count += 1

                    print(f"Backfilled {row.get('symbol')}: call_theo_1={theo.get('call_debit_theo_1'):.4f}, put_theo_1={theo.get('put_debit_theo_1'):.4f}")

            rows.append(row)

    if not dry_run and updated_count > 0:
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nUpdated {updated_count} theo values in {csv_path}")
    elif dry_run:
        print(f"\n[DRY RUN] Would update {updated_count} theo values")
    else:
        print(f"\nNo updates needed - all theo values already present")

def main():
    if len(sys.argv) < 2:
        # Default to today's CSV
        from datetime import datetime
        today = datetime.now().strftime("%y_%m_%d")
        csv_path = f"C:\\OptionsHistory\\{today}\\combined_listener_spreads.csv"
    else:
        csv_path = sys.argv[1]

    dry_run = '--dry-run' in sys.argv

    print(f"Backfilling theo values in: {csv_path}")
    print(f"Dry run: {dry_run}\n")

    backfill_csv(csv_path, dry_run)

if __name__ == "__main__":
    main()
