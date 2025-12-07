#!/usr/bin/env python3
"""
Sector vs Global Optimization Comparison for Adaptive Hugging Strategy

This script:
1. Reads candidate tickers from a TXT file (exchange:ticker format)
2. Fetches GICS sector classification from IBKR for each ticker
3. Finds or downloads historical price CSVs for each ticker
4. Organizes historical price CSVs by sector
5. Runs genetic optimization PER SECTOR using genetic_improved_hugging_optimizer.py
6. Runs genetic optimization GLOBALLY across ALL tickers
7. Compares results and generates a comprehensive report

Usage:
    # Without downloading (requires existing CSV files)
    python sector_vs_global_optimizer.py \
        --candidates "/path/to/Debit Spread Candidate_xxx.txt" \
        --data-dir "/path/to/Stock History and Backtesting Data" \
        --output-dir "./optimization_results" \
        --generations 100 \
        --population 50

    # With automatic data download from IBKR (for missing tickers)
    python sector_vs_global_optimizer.py \
        --candidates "/path/to/Debit Spread Candidate_xxx.txt" \
        --data-dir "/path/to/Stock History and Backtesting Data" \
        --output-dir "./optimization_results" \
        --download \
        --duration "1 Y" \
        --generations 100 \
        --population 50
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

try:
    from ib_insync import IB, Stock, util
    import pandas as pd
    HAS_IB = True
except ImportError:
    print("Warning: ib_insync not available. Sector mapping will use fallback method.")
    HAS_IB = False
    pd = None


class SectorGlobalOptimizer:
    """Orchestrates sector-based and global genetic optimization"""

    def __init__(self, candidates_file: str, data_dir: str, output_dir: str):
        self.candidates_file = Path(candidates_file)
        self.data_dir = Path(data_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.tickers = []
        self.ticker_to_sector = {}
        self.sector_to_tickers = defaultdict(list)
        self.ticker_to_csv = {}

    def parse_candidates(self) -> List[str]:
        """Parse the candidate file and extract tickers"""
        print(f"📋 Parsing candidates from: {self.candidates_file}")

        with open(self.candidates_file, 'r') as f:
            content = f.read().strip()

        # Split by comma and extract tickers
        raw_tickers = [t.strip() for t in content.split(',')]

        # Extract just the ticker symbol from exchange:ticker format
        tickers = []
        for item in raw_tickers:
            if ':' in item:
                exchange, ticker = item.split(':', 1)
                tickers.append(ticker)
            else:
                tickers.append(item)

        self.tickers = sorted(set(tickers))
        print(f"✅ Found {len(self.tickers)} unique tickers")
        return self.tickers

    def get_sector_from_ibkr(self, ticker: str, ib: IB) -> str:
        """Fetch GICS sector from IBKR ContractDetails"""
        try:
            stock = Stock(ticker, 'SMART', 'USD')
            details = ib.reqContractDetails(stock)

            if details:
                cd = details[0]
                # IBKR provides industry/category, map to GICS-like sectors
                industry = getattr(cd, 'industry', '')
                category = getattr(cd, 'category', '')

                # Map IBKR industry to GICS sector (simplified)
                sector = self._map_ibkr_to_gics(industry, category)
                return sector
        except Exception as e:
            print(f"⚠️  Failed to get sector for {ticker}: {e}")

        return 'Unknown'

    def _map_ibkr_to_gics(self, industry: str, category: str) -> str:
        """Map IBKR industry/category to GICS-like sector names"""
        industry_lower = industry.lower()
        category_lower = category.lower()

        # Technology
        if any(x in industry_lower for x in ['technology', 'software', 'semiconductor', 'computer']):
            return 'Information Technology'

        # Finance
        if any(x in industry_lower for x in ['finance', 'bank', 'insurance', 'investment']):
            return 'Financials'

        # Healthcare
        if any(x in industry_lower for x in ['health', 'pharmaceutical', 'biotech', 'medical']):
            return 'Health Care'

        # Energy
        if any(x in industry_lower for x in ['energy', 'oil', 'gas', 'petroleum']):
            return 'Energy'

        # Consumer
        if any(x in industry_lower for x in ['retail', 'consumer']):
            if 'staples' in category_lower or 'food' in category_lower:
                return 'Consumer Staples'
            else:
                return 'Consumer Discretionary'

        # Industrials
        if any(x in industry_lower for x in ['industrial', 'manufacturing', 'aerospace', 'defense']):
            return 'Industrials'

        # Materials
        if any(x in industry_lower for x in ['materials', 'chemical', 'mining', 'metals']):
            return 'Materials'

        # Utilities
        if 'utilities' in industry_lower or 'utility' in industry_lower:
            return 'Utilities'

        # Real Estate
        if 'real estate' in industry_lower or 'reit' in category_lower:
            return 'Real Estate'

        # Communication Services
        if any(x in industry_lower for x in ['communication', 'media', 'entertainment', 'telecom']):
            return 'Communication Services'

        return 'Unknown'

    def map_tickers_to_sectors(self) -> Dict[str, str]:
        """Map all tickers to their GICS sectors using IBKR"""
        print(f"\n🔍 Fetching sector information from IBKR...")

        if not HAS_IB:
            print("⚠️  ib_insync not available, using ticker-based heuristics")
            for ticker in self.tickers:
                self.ticker_to_sector[ticker] = 'Unknown'
            return self.ticker_to_sector

        ib = IB()
        try:
            ib.connect('127.0.0.1', 7497, clientId=999, timeout=10)

            for i, ticker in enumerate(self.tickers, 1):
                sector = self.get_sector_from_ibkr(ticker, ib)
                self.ticker_to_sector[ticker] = sector
                self.sector_to_tickers[sector].append(ticker)

                if i % 10 == 0:
                    print(f"  Progress: {i}/{len(self.tickers)} tickers processed")

            ib.disconnect()
        except Exception as e:
            print(f"❌ Failed to connect to IBKR: {e}")
            print("   Make sure IB Gateway is running on 127.0.0.1:7497")
            sys.exit(1)

        print(f"✅ Mapped {len(self.ticker_to_sector)} tickers to sectors")
        print(f"   Found {len(self.sector_to_tickers)} unique sectors")

        return self.ticker_to_sector

    def find_price_csvs(self) -> Dict[str, str]:
        """Find historical price CSV files for each ticker"""
        print(f"\n📁 Searching for price CSV files in: {self.data_dir}")

        # Search for CSV files matching ticker patterns
        # Expected format: BATS_TICKER_1D.csv or similar
        for ticker in self.tickers:
            found = False

            # Search patterns
            patterns = [
                f"BATS_{ticker}, 1D.csv",
                f"BATS_{ticker}_1D.csv",
                f"{ticker}, 1D.csv",
                f"{ticker}_1D.csv",
            ]

            # Search in main data dir and subdirectories
            for pattern in patterns:
                matches = list(self.data_dir.rglob(pattern))
                if matches:
                    self.ticker_to_csv[ticker] = str(matches[0])
                    found = True
                    break

            # Also check ticker-specific subdirectories
            if not found:
                ticker_dir = self.data_dir / ticker
                if ticker_dir.exists():
                    csv_files = list(ticker_dir.glob("*.csv"))
                    if csv_files:
                        self.ticker_to_csv[ticker] = str(csv_files[0])
                        found = True

            if not found:
                print(f"⚠️  No CSV found for {ticker}")

        print(f"✅ Found CSV files for {len(self.ticker_to_csv)}/{len(self.tickers)} tickers")
        return self.ticker_to_csv

    def download_missing_data(self, duration="1 Y"):
        """Download historical data from IBKR for tickers missing CSV files"""
        missing_tickers = [t for t in self.tickers if t not in self.ticker_to_csv]

        if not missing_tickers:
            print(f"\n✅ All tickers have CSV data, skipping download")
            return

        print(f"\n📥 Downloading historical data for {len(missing_tickers)} missing tickers...")

        if not HAS_IB:
            print("❌ ib_insync not available, cannot download data")
            return

        ib = IB()
        try:
            ib.connect('127.0.0.1', 7497, clientId=998, timeout=10)

            # Create download directory
            download_dir = self.output_dir / "downloaded_data"
            download_dir.mkdir(exist_ok=True)

            for i, ticker in enumerate(missing_tickers, 1):
                try:
                    print(f"  [{i}/{len(missing_tickers)}] Downloading {ticker}...", end=" ")

                    stock = Stock(ticker, 'SMART', 'USD')
                    ib.qualifyContracts(stock)

                    # Request historical data (1 year of daily bars)
                    bars = ib.reqHistoricalData(
                        stock,
                        endDateTime="",
                        durationStr=duration,
                        barSizeSetting="1 day",
                        whatToShow="TRADES",
                        useRTH=True,
                        formatDate=1
                    )

                    if bars and len(bars) > 60:  # Need at least 60 bars for indicators
                        # Convert to DataFrame
                        df = util.df(bars)[["date", "open", "high", "low", "close", "volume"]]
                        df.columns = ["time", "open", "high", "low", "close", "volume"]

                        # Save to CSV in expected format
                        csv_path = download_dir / f"BATS_{ticker}_1D.csv"
                        df.to_csv(csv_path, index=False)

                        # Add to ticker_to_csv mapping
                        self.ticker_to_csv[ticker] = str(csv_path)

                        print(f"✓ ({len(bars)} bars)")
                    else:
                        print(f"⚠️  Insufficient data ({len(bars) if bars else 0} bars)")

                    # Rate limiting to avoid IBKR pacing violations
                    if i % 10 == 0:
                        ib.sleep(2)

                except Exception as e:
                    print(f"❌ Error: {e}")

            ib.disconnect()
            print(f"✅ Downloaded {len([t for t in missing_tickers if t in self.ticker_to_csv])} CSV files")

        except Exception as e:
            print(f"❌ Failed to connect to IBKR: {e}")
            print("   Make sure IB Gateway is running on 127.0.0.1:7497")

    def organize_data_by_sector(self):
        """Organize CSV files into sector folders for genetic_improved_hugging_optimizer.py"""
        print(f"\n📂 Organizing data by sector...")

        sector_data_dir = self.output_dir / "sector_data"
        sector_data_dir.mkdir(exist_ok=True)

        # Create sector subdirectories and copy CSVs
        for sector, tickers in self.sector_to_tickers.items():
            sector_dir = sector_data_dir / sector
            sector_dir.mkdir(exist_ok=True)

            copied = 0
            for ticker in tickers:
                if ticker in self.ticker_to_csv:
                    src = Path(self.ticker_to_csv[ticker])
                    dst = sector_dir / f"{ticker}.csv"
                    shutil.copy2(src, dst)
                    copied += 1

            print(f"  {sector}: {copied} CSV files")

        return sector_data_dir

    def run_sector_optimization(self, sector_data_dir: Path, generations: int, population: int):
        """Run genetic optimization per sector"""
        print(f"\n🧬 Running SECTOR-BASED optimization...")
        print(f"   Generations: {generations}, Population: {population}")

        optimizer_script = Path(__file__).parent / "genetic_improved_hugging_optimizer.py"

        cmd = [
            sys.executable,
            str(optimizer_script),
            "--folder", str(sector_data_dir),
            "--generations", str(generations),
            "--population", str(population),
        ]

        # Run and capture output
        result = subprocess.run(cmd, capture_output=True, text=True)

        # Save output
        output_file = self.output_dir / "sector_optimization_output.txt"
        with open(output_file, 'w') as f:
            f.write(result.stdout)
            if result.stderr:
                f.write("\n\n=== STDERR ===\n")
                f.write(result.stderr)

        print(f"✅ Sector optimization complete. Output saved to: {output_file}")

        # Parse results
        sector_results = self._parse_sector_results(result.stdout)
        return sector_results

    def run_global_optimization(self, generations: int, population: int):
        """Run genetic optimization across ALL tickers (global approach)"""
        print(f"\n🌐 Running GLOBAL optimization (all tickers)...")
        print(f"   Generations: {generations}, Population: {population}")

        # Collect all CSV paths
        all_csvs = [self.ticker_to_csv[t] for t in self.tickers if t in self.ticker_to_csv]

        if not all_csvs:
            print("❌ No CSV files available for global optimization")
            return None

        # Create a combined folder for global optimization
        global_dir = self.output_dir / "global_data"
        global_dir.mkdir(exist_ok=True)

        for ticker in self.tickers:
            if ticker in self.ticker_to_csv:
                src = Path(self.ticker_to_csv[ticker])
                dst = global_dir / f"{ticker}.csv"
                shutil.copy2(src, dst)

        optimizer_script = Path(__file__).parent / "genetic_improved_hugging_optimizer.py"

        # Run using --folder mode (will combine all CSVs)
        cmd = [
            sys.executable,
            str(optimizer_script),
            "--folder", str(global_dir),
            "--generations", str(generations),
            "--population", str(population),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)

        # Save output
        output_file = self.output_dir / "global_optimization_output.txt"
        with open(output_file, 'w') as f:
            f.write(result.stdout)
            if result.stderr:
                f.write("\n\n=== STDERR ===\n")
                f.write(result.stderr)

        print(f"✅ Global optimization complete. Output saved to: {output_file}")

        # Parse results
        global_results = self._parse_global_results(result.stdout)
        return global_results

    def _parse_sector_results(self, output: str) -> Dict:
        """Parse sector optimization output"""
        results = {}

        # Parse sector-specific results from output
        # Format: "Best parameter set for sector 'SectorName':"
        sectors = re.findall(r"Best parameter set for sector '(.+?)':", output)

        for sector in sectors:
            # Extract parameters and metrics for this sector
            sector_section = output.split(f"Best parameter set for sector '{sector}':")[1]
            next_sector_idx = sector_section.find("Best parameter set for sector")
            if next_sector_idx > 0:
                sector_section = sector_section[:next_sector_idx]

            # Parse metrics
            profit_match = re.search(r"Average total profit across sector: ([\d.]+)", sector_section)
            pop_match = re.search(r"Average probability of profit \(POP\) across sector: ([\d.]+)%", sector_section)

            results[sector] = {
                'avg_profit': float(profit_match.group(1)) if profit_match else 0.0,
                'avg_pop': float(pop_match.group(1)) / 100 if pop_match else 0.0,
            }

        return results

    def _parse_global_results(self, output: str) -> Dict:
        """Parse global optimization output"""
        results = {}

        # Parse global results
        profit_match = re.search(r"Average total profit across sector: ([\d.]+)", output)
        pop_match = re.search(r"Average probability of profit \(POP\) across sector: ([\d.]+)%", output)

        results = {
            'avg_profit': float(profit_match.group(1)) if profit_match else 0.0,
            'avg_pop': float(pop_match.group(1)) / 100 if pop_match else 0.0,
        }

        return results

    def generate_comparison_report(self, sector_results: Dict, global_results: Dict):
        """Generate a comprehensive comparison report"""
        print(f"\n📊 Generating comparison report...")

        report_file = self.output_dir / "sector_vs_global_report.txt"

        with open(report_file, 'w') as f:
            f.write("=" * 80 + "\n")
            f.write("SECTOR vs GLOBAL OPTIMIZATION COMPARISON REPORT\n")
            f.write("Adaptive Hugging Combined Debit Spread Strategy\n")
            f.write("=" * 80 + "\n\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Total Tickers: {len(self.tickers)}\n")
            f.write(f"Sectors: {len(self.sector_to_tickers)}\n\n")

            # Sector-by-sector results
            f.write("-" * 80 + "\n")
            f.write("SECTOR-BASED OPTIMIZATION RESULTS\n")
            f.write("-" * 80 + "\n\n")

            for sector, metrics in sector_results.items():
                ticker_count = len(self.sector_to_tickers.get(sector, []))
                f.write(f"{sector} ({ticker_count} tickers):\n")
                f.write(f"  Average Profit: {metrics['avg_profit']:.4f}\n")
                f.write(f"  Average POP: {metrics['avg_pop']:.2%}\n\n")

            # Global results
            f.write("-" * 80 + "\n")
            f.write("GLOBAL OPTIMIZATION RESULTS (All Tickers Combined)\n")
            f.write("-" * 80 + "\n\n")
            f.write(f"Average Profit: {global_results['avg_profit']:.4f}\n")
            f.write(f"Average POP: {global_results['avg_pop']:.2%}\n\n")

            # Comparison summary
            f.write("-" * 80 + "\n")
            f.write("COMPARISON SUMMARY\n")
            f.write("-" * 80 + "\n\n")

            # Calculate weighted average for sector approach
            sector_weighted_profit = sum(r['avg_profit'] for r in sector_results.values()) / len(sector_results) if sector_results else 0
            sector_weighted_pop = sum(r['avg_pop'] for r in sector_results.values()) / len(sector_results) if sector_results else 0

            f.write(f"Sector Approach (Weighted Avg):\n")
            f.write(f"  Profit: {sector_weighted_profit:.4f}\n")
            f.write(f"  POP: {sector_weighted_pop:.2%}\n\n")

            f.write(f"Global Approach:\n")
            f.write(f"  Profit: {global_results['avg_profit']:.4f}\n")
            f.write(f"  POP: {global_results['avg_pop']:.2%}\n\n")

            # Winner
            if sector_weighted_profit > global_results['avg_profit']:
                f.write("🏆 WINNER: Sector-based optimization\n")
                improvement = ((sector_weighted_profit - global_results['avg_profit']) / global_results['avg_profit']) * 100
                f.write(f"   Improvement: +{improvement:.2f}%\n")
            else:
                f.write("🏆 WINNER: Global optimization\n")
                improvement = ((global_results['avg_profit'] - sector_weighted_profit) / sector_weighted_profit) * 100
                f.write(f"   Improvement: +{improvement:.2f}%\n")

            f.write("\n" + "=" * 80 + "\n")

        print(f"✅ Report saved to: {report_file}")

        # Also save JSON version
        json_file = self.output_dir / "comparison_results.json"
        with open(json_file, 'w') as f:
            json.dump({
                'sector_results': sector_results,
                'global_results': global_results,
                'ticker_to_sector': self.ticker_to_sector,
            }, f, indent=2)

        print(f"✅ JSON results saved to: {json_file}")


def main():
    parser = argparse.ArgumentParser(description="Sector vs Global Genetic Optimization")
    parser.add_argument('--candidates', required=True, help="Path to candidate tickers file (e.g., Debit Spread Candidate_xxx.txt)")
    parser.add_argument('--data-dir', required=True, help="Path to stock history data directory")
    parser.add_argument('--output-dir', default='./optimization_results', help="Output directory for results")
    parser.add_argument('--generations', type=int, default=100, help="Number of generations for genetic algorithm")
    parser.add_argument('--population', type=int, default=50, help="Population size for genetic algorithm")
    parser.add_argument('--download', action='store_true', help="Download missing historical data from IBKR")
    parser.add_argument('--duration', default='1 Y', help="Duration of historical data to download (default: 1 Y)")

    args = parser.parse_args()

    print("=" * 80)
    print("SECTOR vs GLOBAL OPTIMIZATION COMPARISON")
    print("Adaptive Hugging Combined Debit Spread Strategy")
    print("=" * 80 + "\n")

    # Initialize optimizer
    optimizer = SectorGlobalOptimizer(args.candidates, args.data_dir, args.output_dir)

    # Step 1: Parse candidates
    optimizer.parse_candidates()

    # Step 2: Map to sectors
    optimizer.map_tickers_to_sectors()

    # Step 3: Find price CSVs
    optimizer.find_price_csvs()

    # Step 3.5: Download missing data if requested
    if args.download:
        optimizer.download_missing_data(duration=args.duration)

    # Step 4: Organize by sector
    sector_data_dir = optimizer.organize_data_by_sector()

    # Step 5: Run sector-based optimization
    sector_results = optimizer.run_sector_optimization(sector_data_dir, args.generations, args.population)

    # Step 6: Run global optimization
    global_results = optimizer.run_global_optimization(args.generations, args.population)

    # Step 7: Generate comparison report
    optimizer.generate_comparison_report(sector_results, global_results)

    print("\n✅ Optimization complete!")
    print(f"📁 Results saved to: {args.output_dir}")


if __name__ == '__main__':
    main()
