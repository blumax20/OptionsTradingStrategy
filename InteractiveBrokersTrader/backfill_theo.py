#!/usr/bin/env python3
"""
Backfill theoretical spread debit values in combined_listener_spreads.csv
Uses Black-Scholes to calculate theoretical values for rows missing them.

Fix N: Also supports --live mode to fetch live market prices from IB at market open.
This updates the call_debit_limit_* and put_debit_limit_* columns with current quotes.
"""
import csv
import math
import os
import sys
from datetime import datetime
import time

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


# ---- Fix N: Live price fetching from IB ----

def _fetch_live_spread_price(ib, symbol: str, expiration: str, atm: float,
                              width: float, right: str = 'C'):
    """Fetch live debit spread price from IB.

    Args:
        ib: Connected IB instance
        symbol: Stock symbol
        expiration: Expiration in YYYYMMDD format
        atm: ATM strike price
        width: Spread width (1.0, 2.5, or 5.0)
        right: 'C' for call or 'P' for put

    Returns:
        Debit spread price (ask_long - bid_short) or None if unavailable
    """
    from ib_insync import Option
    try:
        long_strike = atm
        short_strike = atm + width if right == 'C' else atm - width
        if short_strike <= 0:
            return None

        long_opt = Option(symbol, expiration, long_strike, right, 'SMART')
        short_opt = Option(symbol, expiration, short_strike, right, 'SMART')

        qualified = ib.qualifyContracts(long_opt, short_opt)
        if len(qualified) < 2:
            print(f"  [{symbol}] Could not qualify {right} {width} spread contracts")
            return None

        # Request market data
        long_ticker = ib.reqMktData(long_opt, snapshot=True)
        short_ticker = ib.reqMktData(short_opt, snapshot=True)

        # Wait for data (up to 2 seconds)
        for _ in range(20):
            ib.sleep(0.1)
            if long_ticker.ask and short_ticker.bid is not None:
                break

        ask_long = long_ticker.ask
        bid_short = short_ticker.bid

        ib.cancelMktData(long_opt)
        ib.cancelMktData(short_opt)

        if ask_long and ask_long > 0 and bid_short is not None and bid_short >= 0:
            debit = ask_long - bid_short
            # Cap at spread width (can't exceed max value)
            debit = min(debit, width)
            return round(debit, 2)
        else:
            print(f"  [{symbol}] {right} {width}: No valid quotes (ask={ask_long}, bid={bid_short})")
    except Exception as e:
        print(f"  [{symbol}] Live price fetch error for {right} {width}: {e}")
    return None


def update_live_prices(csv_path: str, dry_run: bool = False):
    """Update CSV limit columns with live market prices from IB.

    This should be run at market open to replace after-hours theo values
    with actual live market prices.
    """
    from ib_insync import IB, util
    util.startLoop()

    if not os.path.exists(csv_path):
        print(f"CSV not found: {csv_path}")
        return

    # Connect to IB
    ib = IB()
    try:
        ib.connect('127.0.0.1', 7497, clientId=102)
        print("Connected to IB")
    except Exception as e:
        print(f"IB connection failed: {e}")
        return

    rows = []
    fieldnames = None
    updated_count = 0

    try:
        with open(csv_path, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames)
            rows = list(reader)

        print(f"Processing {len(rows)} rows...")

        for row in rows:
            symbol = row.get('symbol')
            expiration = row.get('expiration')
            atm = _parse_float(row.get('atm_strike'))

            if not symbol or not expiration or not atm:
                continue

            print(f"[{symbol}] Fetching live prices for exp={expiration}, atm={atm}...")

            # Fetch live prices for each width
            for width, suffix in [(1.0, '1'), (2.5, '2_5'), (5.0, '5')]:
                # CALL spread
                call_live = _fetch_live_spread_price(ib, symbol, expiration, atm, width, 'C')
                if call_live is not None:
                    col = f'call_debit_limit_{suffix}'
                    old_val = row.get(col)
                    row[col] = call_live
                    print(f"  {col}: {old_val} -> {call_live}")
                    updated_count += 1

                # PUT spread
                put_live = _fetch_live_spread_price(ib, symbol, expiration, atm, width, 'P')
                if put_live is not None:
                    col = f'put_debit_limit_{suffix}'
                    old_val = row.get(col)
                    row[col] = put_live
                    print(f"  {col}: {old_val} -> {put_live}")
                    updated_count += 1

            # Small delay between symbols to avoid rate limiting
            time.sleep(0.5)

    finally:
        ib.disconnect()
        print("Disconnected from IB")

    if not dry_run and updated_count > 0:
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nUpdated {updated_count} live prices in {csv_path}")
    elif dry_run:
        print(f"\n[DRY RUN] Would update {updated_count} live prices")
    else:
        print(f"\nNo live prices could be fetched")

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
    # Parse arguments
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if args:
        csv_path = args[0]
    else:
        # Default to today's CSV
        today = datetime.now().strftime("%y_%m_%d")
        csv_path = f"C:\\OptionsHistory\\{today}\\combined_listener_spreads.csv"

    dry_run = '--dry-run' in sys.argv
    live_mode = '--live' in sys.argv

    print(f"CSV path: {csv_path}")
    print(f"Dry run: {dry_run}")
    print(f"Mode: {'LIVE (fetching from IB)' if live_mode else 'THEO (Black-Scholes backfill)'}\n")

    if live_mode:
        update_live_prices(csv_path, dry_run)
    else:
        backfill_csv(csv_path, dry_run)


if __name__ == "__main__":
    main()
