"""
This script calculates the theoretical prices of European call and put options across a range of
strike prices using the Black–Scholes model.  It is designed to work with end‑of‑day price
data exported from a charting or brokerage platform (for example, the provided GMAB CSV
files).

Usage:
    python3 option_values_with_spreads.py

When run, the script prompts for the path to a CSV file containing at least two columns:

    time:  Unix timestamp (seconds) representing the end of each trading session.
    close: Closing price of the underlying asset.

The script automatically converts the timestamps into the America/New_York time zone,
computes a 20‑day annualised realised volatility from the log returns, and finds the
most recent closing price.  It then generates a table of Black–Scholes call and put
option prices for strike prices spaced one dollar apart around the last price (±10 strikes)
for three time‑to‑maturity horizons: 15, 30 and 60 days.  In addition, it calculates
the value of a $1‑wide debit spread using the strike closest to at‑the‑money and the
next strike up for calls and down for puts.  All results are written to an Excel file
with separate sheets for the option prices and the debit spreads.  The risk‑free rate and
dividend yield are assumed constant (4.5 % and 0 %, respectively), but these can be
adjusted in the `main` function.

Example input prompt and output:

    Enter path to CSV file: BATS_GMAB, 1D.csv
    Last close price: 24.38
    Realised volatility (20d ann.): 0.2967
      Strike Call_15d Put_15d Call_30d Put_30d Call_60d Put_60d
         14   10.0613   0.0000   10.1632   0.0000   10.3387   0.0000
         ...

    Debit spread values:
     Maturity Call_Spread Put_Spread
         15d      0.5407      0.4702
         30d      0.6871      0.5803
         60d      0.9084      0.7576

Note: The computed prices are theoretical values under the assumptions of the
Black–Scholes model and may differ from observed market prices.
"""

import math
import os
from typing import Dict, List

import numpy as np
import pandas as pd
from scipy.stats import norm


def black_scholes_price(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str = "call",
) -> float:
    """Compute the Black–Scholes price for a European option.

    Parameters
    ----------
    S : float
        Current price of the underlying asset.
    K : float
        Strike price of the option.
    T : float
        Time to maturity in years.
    r : float
        Annual risk‑free interest rate (continuously compounded).
    sigma : float
        Annualised volatility of the underlying asset.
    option_type : {'call', 'put'}, optional
        Type of the option to price.  Defaults to 'call'.

    Returns
    -------
    float
        The theoretical price of the option.
    """
    if T <= 0 or sigma <= 0:
        intrinsic = max(S - K, 0.0) if option_type == "call" else max(K - S, 0.0)
        return intrinsic
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    if option_type.lower() == "call":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def parse_market_data(csv_path: str) -> Dict[str, float]:
    """Load market data from a CSV file and compute the last close, its date and realised volatility.

    The CSV must contain at least two columns: `time` and `close`.

    Returns a dictionary with keys:
      - 'S': last closing price,
      - 'sigma': annualised 20‑day realised volatility, and
      - 'last_date': date of the last closing price (datetime.date).
    """
    df = pd.read_csv(csv_path)
    if 'time' not in df.columns or 'close' not in df.columns:
        raise ValueError("CSV must contain 'time' and 'close' columns.")
    # Convert timestamps to local date
    df['date'] = (
        pd.to_datetime(df['time'], unit='s')
        .dt.tz_localize('UTC')
        .dt.tz_convert('America/New_York')
        .dt.date
    )
    df.sort_values('date', inplace=True)
    last_date = df['date'].max()
    S = float(df.loc[df['date'] == last_date, 'close'].iloc[0])
    # Compute realised volatility from last 20 log returns
    df['log_ret'] = np.log(df['close']).diff()
    sigma = float(df.loc[df['date'] <= last_date, 'log_ret'].tail(20).std() * math.sqrt(252))
    return {'S': S, 'sigma': sigma, 'last_date': last_date}


def build_option_table(
    S: float,
    sigma: float,
    r: float,
    strike_span: int = 10,
    maturities: List[int] = [15, 30, 60],
) -> pd.DataFrame:
    """Generate a DataFrame of call and put option prices across strike prices and maturities."""
    base_strike = int(round(S))
    strike_start = max(1, base_strike - strike_span)
    strike_end = base_strike + strike_span
    strikes = range(strike_start, strike_end + 1)
    rows: List[Dict[str, float]] = []
    for K in strikes:
        row = {'Strike': K}
        for days in maturities:
            T = days / 365.0
            call_price = black_scholes_price(S, K, T, r, sigma, option_type="call")
            put_price = black_scholes_price(S, K, T, r, sigma, option_type="put")
            row[f'Call_{days}d'] = call_price
            row[f'Put_{days}d'] = put_price
        rows.append(row)
    return pd.DataFrame(rows)


def compute_debit_spreads(
    S: float,
    sigma: float,
    r: float,
    maturities: List[int],
    increments: List[float],
) -> pd.DataFrame:
    """Compute call and put debit spreads for various strike differences at the at‑the‑money strike.

    Parameters
    ----------
    S : float
        Current price of the underlying asset.
    sigma : float
        Volatility used in the option model.
    r : float
        Risk‑free rate.
    maturities : list of int
        List of maturities in days.
    increments : list of float
        List of strike differences (e.g., [0.5, 1, 2.5, 5]) for which to compute debit spreads.

    Returns
    -------
    pd.DataFrame
        A DataFrame with one row per maturity and columns for each call and put spread at each increment.
    """
    base_strike = int(round(S))
    spread_rows: List[Dict[str, float]] = []
    for days in maturities:
        T = days / 365.0
        row: Dict[str, float] = {'Maturity': f'{days}d'}
        for inc in increments:
            # Call spread: long call at base_strike, short call at base_strike + inc
            call_near = black_scholes_price(S, base_strike, T, r, sigma, option_type="call")
            call_far = black_scholes_price(S, base_strike + inc, T, r, sigma, option_type="call")
            call_spread = call_near - call_far
            # Put spread: long put at base_strike, short put at base_strike - inc
            put_near = black_scholes_price(S, base_strike, T, r, sigma, option_type="put")
            put_far = black_scholes_price(S, max(base_strike - inc, 0.01), T, r, sigma, option_type="put")
            put_spread = put_near - put_far
            row[f'Call_Spread_{inc}'] = call_spread
            row[f'Put_Spread_{inc}'] = put_spread
        spread_rows.append(row)
    return pd.DataFrame(spread_rows)


def main() -> None:
    # Ask user for CSV file path (optional)
    csv_path = input("Enter path to CSV file (press Enter to skip): ").strip()
    market_data = None
    if csv_path:
        # Load market data and compute last price, volatility, and last date
        try:
            market_data = parse_market_data(csv_path)
        except FileNotFoundError:
            raise SystemExit(f"File '{csv_path}' not found.")
        except ValueError as exc:
            raise SystemExit(str(exc))

    # Extract data from CSV if provided
    if market_data is not None:
        S_last = market_data['S']
        sigma_realized = market_data['sigma']
        last_date = market_data['last_date']
    else:
        S_last = None
        sigma_realized = None
        # Without CSV, default the valuation date to today in America/New_York
        try:
            # Use pandas to get current date in the user's timezone
            last_date = pd.Timestamp.now(tz='America/New_York').date()
        except Exception:
            # Fallback to naive current date if timezone conversion fails
            import datetime
            last_date = datetime.date.today()
    # Allow user to input a current price; default to last price if provided via CSV
    user_price_str = input(
        "Enter current price (press Enter to use last price in CSV): "
    ).strip()
    if user_price_str:
        try:
            S = float(user_price_str)
        except ValueError:
            # Invalid user input; if CSV is present, fall back to S_last; otherwise error
            if S_last is not None:
                print("Invalid price entered. Using last price from CSV.")
                S = S_last
            else:
                raise SystemExit("Invalid price entered and no CSV price available. Exiting.")
    else:
        # No user input: if CSV is present, use last price; otherwise cannot proceed
        if S_last is not None:
            S = S_last
        else:
            raise SystemExit("No current price provided and no CSV file available to obtain price. Exiting.")

    # Ask user to optionally input an implied volatility; if none provided, use realised volatility (if available)
    user_vol_str = input(
        "Enter implied volatility as a decimal (press Enter to use realised volatility): "
    ).strip()
    if user_vol_str:
        try:
            sigma = float(user_vol_str)
        except ValueError:
            # Invalid vol input; fallback to realised vol if available
            if sigma_realized is not None:
                print("Invalid implied volatility entered. Using realised volatility.")
                sigma = sigma_realized
            else:
                raise SystemExit("Invalid implied volatility entered and no realised volatility available. Exiting.")
    else:
        # No user vol input; require realised volatility from CSV
        if sigma_realized is not None:
            sigma = sigma_realized
        else:
            raise SystemExit("No implied volatility provided and no realised volatility available. Exiting.")

    # Set risk‑free rate (annualised, continuous compounding)
    r = 0.045
    # Define maturities in days and compute actual expiration dates
    from datetime import timedelta
    maturities = [15, 30, 60]
    exp_dates = {days: last_date + timedelta(days=days) for days in maturities}

    # Display summary information to user
    print(f"Date of last price: {last_date}")
    print("Expiration dates:")
    for d in maturities:
        print(f"  {d}d -> {exp_dates[d]}")
    if S_last is not None:
        print(f"Last close price: {S_last}")
    if user_price_str:
        print(f"Using current price: {S}")
    elif S_last is None:
        print(f"Using current price: {S}")
    # Show volatility information
    if sigma_realized is not None:
        print(f"Realised volatility (20d ann.): {sigma_realized:.4f}")
    if user_vol_str:
        print(f"Using implied volatility: {sigma:.4f}")
    else:
        print(f"Using realised volatility: {sigma:.4f}")

    # Build the option price table for the (possibly user‑supplied) S
    option_table = build_option_table(S, sigma, r)
    # Insert additional informational columns
    option_table.insert(1, 'CurrentPrice', S)
    option_table.insert(2, 'Volatility', sigma)
    option_table.insert(3, 'ValuationDate', last_date)
    for d in maturities:
        option_table[f'ExpDate_{d}d'] = exp_dates[d]
    # Compute debit spreads for multiple increments
    increments = [0.5, 1, 2.5, 5]
    spread_df = compute_debit_spreads(S, sigma, r, maturities, increments)

    # Prepare formatted tables for console display (two decimal places)
    formatted_table = option_table.copy()
    for col in formatted_table.columns:
        if col == 'Strike':
            continue
        if col in ['ValuationDate'] or col.startswith('ExpDate'):
            formatted_table[col] = formatted_table[col].astype(str)
        else:
            formatted_table[col] = formatted_table[col].apply(lambda x: f"{x:.2f}")
    formatted_spread_df = spread_df.copy()
    for col in formatted_spread_df.columns:
        if col != 'Maturity':
            formatted_spread_df[col] = formatted_spread_df[col].apply(lambda x: f"{x:.2f}")

    # Print formatted option table and spreads
    print(formatted_table.to_string(index=False))
    print("\nDebit spread values:")
    print(formatted_spread_df.to_string(index=False))

    # Determine stock name from CSV file name for output file title
    if csv_path:
        base_name = os.path.splitext(os.path.basename(csv_path))[0]
        # Replace spaces and commas with underscores for a cleaner filename
        stock_name_safe = base_name.replace(' ', '_').replace(',', '')
    else:
        stock_name_safe = 'CustomInput'
    # Format date string for the output filename (valuation date)
    date_str = last_date.isoformat()
    output_filename = f"{stock_name_safe}_{date_str}_option_values.xlsx"
    output_path = os.path.join(os.path.dirname(csv_path), output_filename) or output_filename

    # Round numeric values in the DataFrame for Excel output to two decimal places
    excel_option_table = option_table.copy()
    for col in excel_option_table.columns:
        if col not in ['Strike', 'ValuationDate'] and not col.startswith('ExpDate'):
            excel_option_table[col] = excel_option_table[col].round(2)
    excel_spread_df = spread_df.copy()
    for col in excel_spread_df.columns:
        if col != 'Maturity':
            excel_spread_df[col] = excel_spread_df[col].round(2)

    # Create a multi‑sheet Excel file with option prices and debit spreads separated by expiration date
    with pd.ExcelWriter(output_path) as writer:
        # Overall tables (complete tables) can still be written for reference in separate sheets
        excel_option_table.to_excel(writer, sheet_name='all_option_prices', index=False)
        excel_spread_df.to_excel(writer, sheet_name='all_debit_spreads', index=False)
        # Write individual sheets for each expiration date
        for d in maturities:
            exp_date = exp_dates[d]
            exp_str = exp_date.isoformat()
            # Option prices for this expiration date
            price_df = pd.DataFrame({
                'Strike': option_table['Strike'],
                'Call': option_table[f'Call_{d}d'].round(2),
                'Put': option_table[f'Put_{d}d'].round(2),
                'CurrentPrice': S,
                'Volatility': sigma,
                'ValuationDate': last_date,
                'Expiration': exp_date,
            })
            # Debit spreads for this expiration date
            spread_row = spread_df[spread_df['Maturity'] == f'{d}d'].iloc[0]
            # Build a compact spreads table
            spread_data = []
            for inc in increments:
                call_sp_val = spread_row[f'Call_Spread_{inc}']
                put_sp_val = spread_row[f'Put_Spread_{inc}']
                spread_data.append({
                    'Increment': inc,
                    'Call_Spread': round(call_sp_val, 2),
                    'Put_Spread': round(put_sp_val, 2),
                })
            spreads_df = pd.DataFrame(spread_data)
            # Write price and spread tables to separate sheets named by expiration date
            price_sheet_name = f"{exp_str}_option_prices"
            spreads_sheet_name = f"{exp_str}_debit_spreads"
            price_df.to_excel(writer, sheet_name=price_sheet_name, index=False)
            spreads_df.to_excel(writer, sheet_name=spreads_sheet_name, index=False)

    print(f"\nResults have been written to {output_path}")


if __name__ == "__main__":
    main()