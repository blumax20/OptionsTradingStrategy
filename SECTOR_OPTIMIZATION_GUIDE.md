# Sector vs Global Optimization Guide

## Overview

The `sector_vs_global_optimizer.py` script compares two approaches to optimizing your Adaptive Hugging Combined Debit Spread Strategy:

1. **Sector-Based Optimization**: Optimizes strategy parameters separately for each GICS sector
2. **Global Optimization**: Optimizes a single set of parameters across all tickers

This helps determine whether sector-specific strategies outperform a one-size-fits-all approach.

## Prerequisites

1. **IB Gateway or TWS** running on `127.0.0.1:7497`
2. **Python packages**: `ib_insync`, `pandas`
3. **Candidate list**: A TXT file with tickers in `EXCHANGE:TICKER` format (comma-separated)
4. **Historical data**: Either existing CSV files OR use `--download` flag to fetch from IBKR

## Quick Start

### Option 1: Using Existing CSV Files

If you already have historical price CSVs in your data directory:

```bash
cd "/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Agent Assisted Scripts/OptionsTradingStrategy"

python sector_vs_global_optimizer.py \
    --candidates "/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Stock History and Backtesting Data/25_12_06/Debit Spread Candidate_85f65.txt" \
    --data-dir "/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Stock History and Backtesting Data" \
    --output-dir "./optimization_results" \
    --generations 100 \
    --population 50
```

### Option 2: Auto-Download Missing Data from IBKR

If some tickers don't have CSV files, automatically download them:

```bash
python sector_vs_global_optimizer.py \
    --candidates "/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Stock History and Backtesting Data/25_12_06/Debit Spread Candidate_85f65.txt" \
    --data-dir "/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Stock History and Backtesting Data" \
    --output-dir "./optimization_results" \
    --download \
    --duration "1 Y" \
    --generations 100 \
    --population 50
```

## Command-Line Options

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `--candidates` | Yes | - | Path to candidate tickers file |
| `--data-dir` | Yes | - | Directory containing historical price CSVs |
| `--output-dir` | No | `./optimization_results` | Output directory for results |
| `--generations` | No | 100 | Number of generations for genetic algorithm |
| `--population` | No | 50 | Population size for genetic algorithm |
| `--download` | No | False | Download missing data from IBKR |
| `--duration` | No | `1 Y` | Historical data duration (e.g., "1 Y", "6 M", "2 Y") |

## Workflow Steps

The script automatically:

1. **Parses candidate list** - Extracts tickers from your TXT file
2. **Maps to GICS sectors** - Fetches sector classification from IBKR ContractDetails
3. **Finds CSV files** - Searches for existing historical price data
4. **Downloads missing data** (if `--download` flag used) - Fetches from IBKR for any missing tickers
5. **Organizes by sector** - Creates sector subdirectories with corresponding CSVs
6. **Runs sector optimization** - Calls `genetic_improved_hugging_optimizer.py` per sector
7. **Runs global optimization** - Optimizes across all tickers combined
8. **Generates comparison report** - Shows which approach performs better

## Output Files

All results are saved to `--output-dir` (default: `./optimization_results/`):

### Main Reports
- `sector_vs_global_report.txt` - Human-readable comparison report
- `comparison_results.json` - Machine-readable results with all metrics

### Optimization Outputs
- `sector_optimization_output.txt` - Full output from sector-based optimization
- `global_optimization_output.txt` - Full output from global optimization

### Data Organization
- `sector_data/` - CSVs organized by sector (e.g., `sector_data/Information Technology/AAPL.csv`)
- `global_data/` - All CSVs in one folder for global optimization
- `downloaded_data/` - CSVs downloaded from IBKR (if `--download` used)

## Understanding the Report

The final report (`sector_vs_global_report.txt`) shows:

1. **Sector-by-sector results**: Performance metrics for each GICS sector
   - Average Profit per sector
   - Average Probability of Profit (POP) per sector

2. **Global results**: Performance across all tickers with single parameter set
   - Average Profit
   - Average POP

3. **Winner determination**: Which approach performs better
   - Weighted average comparison
   - Percentage improvement

## Expected CSV Format

The script searches for CSV files matching these patterns:
- `BATS_TICKER_1D.csv`
- `BATS_TICKER, 1D.csv`
- `TICKER, 1D.csv`
- `TICKER_1D.csv`

If using `--download`, CSVs are created with this structure:
```
time,open,high,low,close,volume
2024-01-02,150.5,152.3,149.8,151.2,50000000
```

## GICS Sectors

The script maps tickers to these 11 standard GICS sectors:
- Information Technology
- Financials
- Health Care
- Energy
- Consumer Staples
- Consumer Discretionary
- Industrials
- Materials
- Utilities
- Real Estate
- Communication Services

## Troubleshooting

### "Failed to connect to IBKR"
- Ensure IB Gateway or TWS is running
- Check it's configured for port 7497
- Verify API connections are enabled in settings

### "No CSV found for TICKER"
- Run with `--download` flag to auto-download from IBKR
- Or manually add CSV files to your data directory
- Ensure CSV filenames match expected patterns

### "Insufficient data (X bars)"
- Script requires at least 60 daily bars for technical indicators
- Use `--duration "2 Y"` to download more historical data
- Some tickers may have limited history (IPO date, delisting, etc.)

### Genetic optimization taking too long
- Reduce `--generations` (e.g., 50 instead of 100)
- Reduce `--population` (e.g., 30 instead of 50)
- Reduce number of tickers in candidate list

## Example with Your Files

Using your specific file paths:

```bash
cd "/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Agent Assisted Scripts/OptionsTradingStrategy"

# Quick test with small parameters
python sector_vs_global_optimizer.py \
    --candidates "/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Stock History and Backtesting Data/25_12_06/Debit Spread Candidate_85f65.txt" \
    --data-dir "/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Stock History and Backtesting Data" \
    --output-dir "./optimization_results_test" \
    --download \
    --generations 50 \
    --population 30

# Full optimization (will take longer)
python sector_vs_global_optimizer.py \
    --candidates "/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Stock History and Backtesting Data/25_12_06/Debit Spread Candidate_85f65.txt" \
    --data-dir "/Users/maximilian-alexanderneidhardt/Desktop/Investments/Stocks & Bonds/Stock History and Backtesting Data" \
    --output-dir "./optimization_results_full" \
    --download \
    --generations 100 \
    --population 50
```

## Next Steps After Optimization

1. Review `sector_vs_global_report.txt` to see which approach won
2. If sector-based wins: Use sector-specific parameters in production
3. If global wins: Use single parameter set across all tickers
4. Integrate winning parameters into `DailyCycleManagement.py` or `DebitSpreadSignalLimit.py`
5. Run backtests with `hugging_backtest.py` to validate results

## Performance Notes

- **93 tickers** in your candidate list
- **Sector optimization**: ~10-15 minutes (depends on sector count)
- **Global optimization**: ~5-10 minutes
- **Total runtime**: ~20-30 minutes with default parameters
- **IBKR download**: ~2-3 seconds per ticker (with rate limiting)
