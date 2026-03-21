"""
Test whether live_spread_price() can get bid/ask data for held option positions.
Connects to IB, scans positions, then tests join and mid pricing for each spread.

Usage:
    python test_live_spread_price.py [--timeout 10] [--symbol GLPI]
"""
import argparse
import sys
import time
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "InteractiveBrokersTrader"))

from ib_config import IB_HOST, IB_PORT
from ib_insync import IB, Option
from PlaceAnOrder import live_spread_price

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=10.0, help="Timeout for live_spread_price (default 10s)")
    ap.add_argument("--symbol", type=str, default=None, help="Only test this symbol")
    ap.add_argument("--poll-raw", action="store_true", help="Also poll raw bid/ask ticks directly")
    args = ap.parse_args()

    print(f"Connecting to IB at {IB_HOST}:{IB_PORT} (clientId=199)...")
    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=199)
    ib.sleep(1.0)
    print("Connected.\n")

    # Scan positions for vertical spreads
    positions = ib.positions()
    opts = [(p.contract, p.position, p.avgCost) for p in positions
            if getattr(p.contract, 'secType', '') == 'OPT']

    # Group by symbol+expiration+right
    from collections import defaultdict
    groups = defaultdict(list)
    for c, qty, avg in opts:
        key = (c.symbol, c.lastTradeDateOrContractMonth, c.right)
        groups[key].append((c, qty, avg))

    spreads = []
    for (sym, exp, right), legs in groups.items():
        if args.symbol and sym.upper() != args.symbol.upper():
            continue
        if len(legs) != 2:
            continue
        # Sort by strike — long leg has higher qty (positive)
        longs = [(c, q, a) for c, q, a in legs if q > 0]
        shorts = [(c, q, a) for c, q, a in legs if q < 0]
        if not longs or not shorts:
            continue
        long_c, long_q, long_avg = longs[0]
        short_c, short_q, short_avg = shorts[0]
        longK = float(long_c.strike)
        shortK = float(short_c.strike)
        spreads.append((sym, exp, right, longK, shortK))

    if not spreads:
        print("No vertical spreads found in positions.")
        ib.disconnect()
        return

    print(f"Found {len(spreads)} spread(s) to test:\n")

    for sym, exp, right, longK, shortK in spreads:
        width = abs(longK - shortK)
        print(f"{'='*60}")
        print(f"  {sym} {right} {longK}/{shortK}  exp={exp}  width={width}")
        print(f"{'='*60}")

        if args.poll_raw:
            # Direct raw bid/ask polling via reqMktData
            long_con = Option(sym, exp, longK, right, "SMART")
            short_con = Option(sym, exp, shortK, right, "SMART")
            ib.qualifyContracts(long_con, short_con)

            print(f"  Requesting raw market data for both legs...")
            t_long = ib.reqMktData(long_con, "", False, False)
            t_short = ib.reqMktData(short_con, "", False, False)

            print(f"  Polling for up to 12 seconds...")
            for i in range(120):
                ib.sleep(0.1)
                lb = t_long.bid
                la = t_long.ask
                sb = t_short.bid
                sa = t_short.ask
                # Check if we have real data (not -1 or nan)
                def valid(v):
                    return v is not None and not (isinstance(v, float) and math.isnan(v)) and v != -1
                lb_ok = valid(lb)
                la_ok = valid(la)
                sb_ok = valid(sb)
                sa_ok = valid(sa)
                elapsed = (i + 1) * 0.1
                if lb_ok and la_ok and sb_ok and sa_ok:
                    print(f"  [{elapsed:.1f}s] FULL DATA — long: bid={lb:.4f} ask={la:.4f} | short: bid={sb:.4f} ask={sa:.4f}")
                    join_val = round(lb - sa, 4) if lb_ok and sa_ok else None
                    mid_val = round((lb+la)/2 - (sb+sa)/2, 4) if lb_ok and la_ok and sb_ok and sa_ok else None
                    print(f"           join (bid_long - ask_short) = {join_val}")
                    print(f"           mid  ((bid+ask)/2 each leg) = {mid_val}")
                    break
                elif i % 10 == 9:
                    # Print status every 1s
                    print(f"  [{elapsed:.1f}s] long: bid={lb} ask={la} | short: bid={sb} ask={sa}")
            else:
                print(f"  [12.0s] TIMEOUT — could not get bid/ask for both legs")

            ib.cancelMktData(long_con)
            ib.cancelMktData(short_con)
            print()

        # Now test live_spread_price() for join and mid
        for scheme in ("join", "mid"):
            print(f"  Testing live_spread_price(..., scheme='{scheme}', timeout={args.timeout}s)...")
            t0 = time.time()
            try:
                result = live_spread_price(
                    ib, sym, exp, right, longK, shortK,
                    action="SELL", scheme=scheme,
                    timeout=args.timeout,
                )
                elapsed = time.time() - t0
                print(f"  -> result={result}  [{elapsed:.1f}s]")
            except Exception as e:
                elapsed = time.time() - t0
                print(f"  -> EXCEPTION: {e}  [{elapsed:.1f}s]")
        print()

    ib.disconnect()
    print("Done.")

if __name__ == "__main__":
    main()
