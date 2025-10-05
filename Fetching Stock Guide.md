# Complete Guide: Fetching Stock Data by Sector

## Overview
This guide shows you how to fetch daily stock data for multiple stocks and organize them into CSV files grouped by sector, with the same technical indicators as your sample BOX data.

## Required Packages
```bash
pip install yfinance pandas numpy ta-lib
```

## Step-by-Step Process

### 1. Install Dependencies
```python
import yfinance as yf
import pandas as pd
import numpy as np
import talib
import os
from datetime import datetime
```

### 2. Your Stock List by Sector
Based on your list, here's how they group by sector:

**Technology (2 stocks):**
- TSM (Taiwan Semiconductor)
- NVDA (NVIDIA)

**Financial Services (5 stocks):**
- SFD, GEN, CM, VIRT, AIG

**Utilities (5 stocks):**
- NEE, NI, ES, D, PSN

**Healthcare (2 stocks):**
- ELAN, FMCKJ

**Real Estate (2 stocks):**
- CCI, AMT

**Consumer Defensive (2 stocks):**
- PM, PEP

**Basic Materials (1 stock):**
- PHYS

**Consumer Cyclical (2 stocks):**
- TXRH, NKE

### 3. Data Fetching Function
```python
def fetch_stock_data(symbol, period='1y'):
    """Fetch stock data with technical indicators"""
    try:
        # Get stock data
        ticker = yf.Ticker(symbol)
        data = ticker.history(period=period)
        
        if data.empty:
            return None
        
        # Prepare data structure
        df = pd.DataFrame()
        df['time'] = data.index.astype(int) // 10**9  # Unix timestamp
        df['open'] = data['Open']
        df['high'] = data['High']
        df['low'] = data['Low']
        df['close'] = data['Close']
        df['Volume'] = data['Volume']
        
        # Calculate Bollinger Bands (20-period, 2 std dev)
        df['BB Basis'] = talib.SMA(df['close'], 20)
        df['BB Upper'] = df['BB Basis'] + (2 * talib.STDDEV(df['close'], 20))
        df['BB Lower'] = df['BB Basis'] - (2 * talib.STDDEV(df['close'], 20))
        
        # EMA (50-period)
        df['EMA'] = talib.EMA(df['close'], 50)
        
        # Another Bollinger Band set (different parameters)
        df['Basis'] = talib.SMA(df['close'], 20)
        std_dev = talib.STDDEV(df['close'], 20)
        df['Upper'] = df['Basis'] + (2.5 * std_dev)
        df['Lower'] = df['Basis'] - (2.5 * std_dev)
        
        # Money Flow Index
        df['MF'] = talib.MFI(df['high'], df['low'], df['close'], df['Volume'], 14)
        
        # MACD
        macd, signal, histogram = talib.MACD(df['close'])
        df['Histogram'] = histogram
        df['MACD'] = macd
        df['Signal'] = signal
        
        # Stochastic Oscillator
        df['K'], df['D'] = talib.STOCH(df['high'], df['low'], df['close'])
        
        # ADX
        df['ADX'] = talib.ADX(df['high'], df['low'], df['close'], 14)
        
        # Add symbol identifier
        df['symbol'] = symbol
        
        return df
        
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
        return None
```

### 4. Main Processing Script
```python
def main():
    # Your stock symbols (cleaned)
    stocks = ['TSM', 'SFD', 'NEE', 'ELAN', 'CCI', 'AMT', 'PM', 'PHYS', 
              'GEN', 'CM', 'FMCKJ', 'NI', 'ES', 'D', 'PSN', 'VIRT', 
              'AIG', 'NVDA', 'TXRH', 'PEP', 'NKE']
    
    # Sector mapping
    sectors = {
        'Technology': ['TSM', 'NVDA'],
        'Financial_Services': ['SFD', 'GEN', 'CM', 'VIRT', 'AIG'],
        'Utilities': ['NEE', 'NI', 'ES', 'D', 'PSN'],
        'Healthcare': ['ELAN', 'FMCKJ'],
        'Real_Estate': ['CCI', 'AMT'],
        'Consumer_Defensive': ['PM', 'PEP'],
        'Basic_Materials': ['PHYS'],
        'Consumer_Cyclical': ['TXRH', 'NKE']
    }
    
    # Create output directory
    os.makedirs('stock_data_by_sector', exist_ok=True)
    
    # Process each sector
    for sector_name, sector_stocks in sectors.items():
        print(f"Processing {sector_name}...")
        sector_data = []
        
        # Fetch data for all stocks in this sector
        for symbol in sector_stocks:
            print(f"  Fetching {symbol}...")
            data = fetch_stock_data(symbol, period='1y')  # Adjust period as needed
            
            if data is not None:
                sector_data.append(data)
                print(f"    âœ“ Got {len(data)} days of data")
            else:
                print(f"    âœ— Failed to fetch data")
        
        # Combine and save sector data
        if sector_data:
            combined_df = pd.concat(sector_data, ignore_index=True)
            combined_df = combined_df.sort_values(['symbol', 'time'])
            
            filename = f'stock_data_by_sector/{sector_name}_stocks.csv'
            combined_df.to_csv(filename, index=False)
            print(f"  âœ“ Saved {len(combined_df)} rows to {filename}")
        
    print("Data collection complete!")

if __name__ == "__main__":
    main()
```

### 5. Usage Instructions

1. **Save the script** as `fetch_stock_data.py`
2. **Install dependencies**: `pip install yfinance pandas numpy ta-lib`
3. **Run the script**: `python fetch_stock_data.py`

### 6. Output Structure

The script will create:
```
stock_data_by_sector/
â”œâ”€â”€ Technology_stocks.csv
â”œâ”€â”€ Financial_Services_stocks.csv  
â”œâ”€â”€ Utilities_stocks.csv
â”œâ”€â”€ Healthcare_stocks.csv
â”œâ”€â”€ Real_Estate_stocks.csv
â”œâ”€â”€ Consumer_Defensive_stocks.csv
â”œâ”€â”€ Basic_Materials_stocks.csv
â””â”€â”€ Consumer_Cyclical_stocks.csv
```

### 7. CSV Format

Each CSV will have the same columns as your BOX sample:
- time, open, high, low, close, Volume
- BB Basis, BB Upper, BB Lower
- EMA, Basis, Upper, Lower
- MF, Histogram, MACD, Signal
- K, D, ADX
- symbol (additional column to identify the stock)

### 8. Customization Options

**Time Period**: Change `period='1y'` to:
- `'1d'` (1 day), `'5d'` (5 days), `'1mo'` (1 month)
- `'3mo'` (3 months), `'6mo'` (6 months), `'1y'` (1 year)
- `'2y'` (2 years), `'5y'` (5 years), `'10y'` (10 years)
- `'ytd'` (year to date), `'max'` (all available data)

**Date Range**: Use specific dates:
```python
data = ticker.history(start='2024-01-01', end='2024-12-31')
```

**Indicators**: Modify the technical indicators by changing periods:
- Bollinger Bands: Change `20` to different period
- EMA: Change `50` to different period  
- MFI: Change `14` to different period

### 9. Error Handling

The script includes error handling for:
- Missing data for specific stocks
- API rate limits
- Network connectivity issues
- Invalid symbols

### 10. Performance Tips

- **Batch processing**: Process stocks in smaller batches if you have many
- **Caching**: Save intermediate results to avoid re-fetching
- **Rate limiting**: Add delays between requests if needed

This approach will give you the same rich technical analysis data as your BOX sample, organized by sector for easier analysis and trading strategy development.