# Stock Data Fetcher and Organizer by Sector
# This script fetches daily stock data and creates CSV files grouped by sector

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os
import argparse

# You'll need to install these packages:
# pip install yfinance pandas numpy talib

try:
    import yfinance as yf
    import talib
    TALIB_AVAILABLE = True
except ImportError:
    print("Warning: yfinance and/or talib not installed. Install with:")
    print("pip install yfinance talib")
    TALIB_AVAILABLE = False

# Sector folders to create under the dated directory
SECTOR_FOLDERS = [
    'Technology Services',
    'Electronic Technology',
    'Finance',
    'Health Technology',
    'Retail Trade',
    'Consumer Non-Durables',
    'Producer Manufacturing',
    'Energy Minerals',
    'Consumer Services',
    'Consumer Durables',
    'Utilities',
    'Non-Energy Minerals',
    'Industrial Servces',
    'Transportation',
    'Commercial Services',
    'Process Industries',
    'Communications',
    'Health Services',
    'Distribution Services',
    'Miscellaneous',
]

def clean_symbol(symbol):
    """Clean symbol by removing exchange prefix"""
    if ':' in symbol:
        return symbol.split(':')[1]
    return symbol

    

def create_sector_folders(base_root: str, date_fmt: str = "%y_%m_%d"):
    """
    Create a dated folder under base_root and the predefined sector subfolders.
    Returns the absolute path to the dated folder.
    Example final path: {base_root}/25_09_11/Consumer Durables/
    """
    today_str = datetime.now().strftime(date_fmt)
    dated_dir = os.path.join(base_root, today_str)
    os.makedirs(dated_dir, exist_ok=True)
    for sector in SECTOR_FOLDERS:
        os.makedirs(os.path.join(dated_dir, sector), exist_ok=True)
    return dated_dir

def calculate_technical_indicators(df):
    """Calculate technical indicators similar to your CSV"""
    if not TALIB_AVAILABLE:
        print("Warning: TA-Lib not available, using simple calculations")
        # Simple moving averages as fallback
        df['BB_Basis'] = df['close'].rolling(window=20).mean()
        df['BB_Upper'] = df['BB_Basis'] + (2 * df['close'].rolling(window=20).std())
        df['BB_Lower'] = df['BB_Basis'] - (2 * df['close'].rolling(window=20).std())
        df['EMA'] = df['close'].ewm(span=50).mean()
        df['Basis'] = df['close'].rolling(window=20).mean()
        std_dev = df['close'].rolling(window=20).std()
        df['Upper'] = df['Basis'] + (2.5 * std_dev)
        df['Lower'] = df['Basis'] - (2.5 * std_dev)
        
        # Fill other indicators with NaN for now
        indicator_cols = ['MF', 'MACD', 'Signal', 'Histogram', 'K', 'D', 'ADX']
        for col in indicator_cols:
            df[col] = np.nan
        return df
    
    try:
        # Bollinger Bands (20-period, 2 std dev)
        df['BB_Basis'] = talib.SMA(df['close'], timeperiod=20)
        df['BB_Upper'] = df['BB_Basis'] + (2 * talib.STDDEV(df['close'], timeperiod=20))
        df['BB_Lower'] = df['BB_Basis'] - (2 * talib.STDDEV(df['close'], timeperiod=20))
        
        # EMAs
        df['EMA'] = talib.EMA(df['close'], timeperiod=50)
        
        # Another set of bands (appears to be different parameters)
        df['Basis'] = talib.SMA(df['close'], timeperiod=20)
        std_dev = talib.STDDEV(df['close'], timeperiod=20)
        df['Upper'] = df['Basis'] + (2.5 * std_dev)
        df['Lower'] = df['Basis'] - (2.5 * std_dev)
        
        # Money Flow Index
        df['MF'] = talib.MFI(df['high'], df['low'], df['close'], df['volume'], timeperiod=14)
        
        # MACD
        macd, signal, histogram = talib.MACD(df['close'])
        df['MACD'] = macd
        df['Signal'] = signal
        df['Histogram'] = histogram
        
        # Stochastic
        df['K'], df['D'] = talib.STOCH(df['high'], df['low'], df['close'])
        
        # ADX
        df['ADX'] = talib.ADX(df['high'], df['low'], df['close'], timeperiod=14)
        
    except Exception as e:
        print(f"Error calculating indicators: {e}")
        # Fill with NaN if calculation fails
        indicator_cols = ['BB_Basis', 'BB_Upper', 'BB_Lower', 'EMA', 'Basis', 'Upper', 'Lower', 
                         'MF', 'MACD', 'Signal', 'Histogram', 'K', 'D', 'ADX']
        for col in indicator_cols:
            if col not in df.columns:
                df[col] = np.nan
    
    return df

def fetch_stock_data(symbol, period='1y'):
    """Fetch stock data for a given symbol"""
    try:
        if not TALIB_AVAILABLE:
            print(f"Cannot fetch data for {symbol} - yfinance not available")
            return None
            
        ticker = yf.Ticker(symbol)
        data = ticker.history(period=period)
        
        if data.empty:
            print(f"No data found for {symbol}")
            return None
        
        # Rename columns to match your format
        data.columns = data.columns.str.lower()
        
        # Add timestamp column
        data['time'] = (data.index.view('int64') // 10**9).astype(int)
        
        # Rename Volume to match your format
        if 'volume' in data.columns:
            data['Volume'] = data['volume']
        
        # Calculate technical indicators
        data = calculate_technical_indicators(data)
        
        # Reorder columns to match your CSV format
        column_order = ['time', 'open', 'high', 'low', 'close', 'Volume', 
                       'BB_Basis', 'BB_Upper', 'BB_Lower', 'EMA', 'Basis', 
                       'Upper', 'Lower', 'MF', 'Histogram', 'MACD', 'Signal', 
                       'K', 'D', 'ADX']
        
        # Ensure all columns exist
        for col in column_order:
            if col not in data.columns:
                data[col] = np.nan
        
        data = data[column_order]
        data['symbol'] = symbol
        
        return data
        
    except Exception as e:
        print(f"Error fetching data for {symbol}: {e}")
        return None

def main():
    """Main function to fetch data and organize by sector and create dated/sector folders."""
    parser = argparse.ArgumentParser(description="Fetch stock data and organize by sector into dated folders.")
    parser.add_argument(
        "--stock-list",
        default="Debit Spread Candidate.txt",
        help="Path to the text file containing a comma-separated list of stock symbols."
    )
    parser.add_argument(
        "--base-dir",
        default="/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Stock History and Backtesting Data/",
        help="Base directory where the dated folder and sector subfolders will be created."
    )
    parser.add_argument(
        "--period",
        default="1y",
        help="History period for yfinance (e.g., 6mo, 1y, 2y)."
    )
    args = parser.parse_args()

    stock_list_file = args.stock_list
    base_dir = args.base_dir.rstrip("/ ")

    if not os.path.exists(stock_list_file):
        print(f"Stock list file '{stock_list_file}' not found!")
        return

    # Create dated directory and all sector subfolders
    dated_dir = create_sector_folders(base_dir)
    print(f"Created/verified dated directory and sector subfolders under: {dated_dir}")

    with open(stock_list_file, 'r') as f:
        stock_list_text = f.read().strip()

    # Parse and clean stock symbols
    stock_symbols_raw = [s.strip() for s in stock_list_text.split(',') if s.strip()]
    stock_symbols = [clean_symbol(s) for s in stock_symbols_raw]

    print(f"Found {len(stock_symbols)} stocks to process")

    # Get sector mapping (fallbacks to 'Unknown' when not found)
    sector_mapping = {}

    # Group stocks by sector (ensure all predefined sectors are present, even if empty)
    sectors = {sector: [] for sector in SECTOR_FOLDERS}
    sectors['Unknown'] = []  # capture unmapped tickers

    for symbol in stock_symbols:
        sector = sector_mapping.get(symbol, 'Unknown')
        if sector not in sectors:
            # in case mapping returns a sector outside of the predefined list
            sectors[sector] = []
        sectors[sector].append(symbol)

    print(f"\nStocks grouped into {len(sectors)} sectors (including 'Unknown'):")
    for sector, symbols in sectors.items():
        print(f"{sector}: {len(symbols)} stocks")

    # Fetch data for each sector and save into its folder
    for sector, symbols in sectors.items():
        print(f"\n=== Processing {sector} sector ===")
        sector_data = []

        for symbol in symbols:
            print(f"Fetching data for {symbol}...")
            data = fetch_stock_data(symbol, period=args.period)
            if data is not None:
                sector_data.append(data)
                print(f"Got {len(data)} rows of data for {symbol}")
            else:
                print(f"Failed to get data for {symbol}")

        # Ensure sector folder exists (it should, from create_sector_folders)
        sector_folder_path = os.path.join(dated_dir, sector)
        os.makedirs(sector_folder_path, exist_ok=True)

        # Combine and save
        if sector_data:
            combined_data = pd.concat(sector_data, ignore_index=True)
            combined_data = combined_data.sort_values(['symbol', 'time'])

            filename = os.path.join(
                sector_folder_path,
                f"{sector.replace(' ', '_')}_stocks.csv"
            )
            combined_data.to_csv(filename, index=False)
            print(f"Saved {len(combined_data)} rows to {filename}")
        else:
            print(f"No data collected for {sector} sector (no CSV created).")

    # Create a summary file in the dated directory
    summary_file = os.path.join(dated_dir, "sector_summary.txt")
    with open(summary_file, 'w') as f:
        f.write("Stock Data Collection Summary\n")
        f.write("=" * 50 + "\n\n")
        for sector, symbols in sectors.items():
            f.write(f"{sector}:\n")
            for symbol in symbols:
                f.write(f"  - {symbol}\n")
            f.write(f"  Total: {len(symbols)} stocks\n\n")

    print(f"\n=== Data collection complete ===")
    print(f"CSV files (if any) saved under dated directory: '{dated_dir}'")
    print(f"Summary saved to {summary_file}")

if __name__ == "__main__":
    main()
