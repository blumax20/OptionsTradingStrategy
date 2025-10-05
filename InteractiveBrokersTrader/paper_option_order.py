
"""
paper_option_order.py
---------------------
A minimal, robust helper for placing paper-trading option orders with IBKR via ib_insync.

Requirements:
    pip install ib-insync

Usage example (paper trading):
    from paper_option_order import place_paper_option_order

    trade = place_paper_option_order(
        ticker="AAPL", 
        right="call",      # "call" or "put"
        quantity=1, 
        days_out=14,       # target expiration distance in days
        order_type="LMT",  # "MKT" or "LMT"
        limit_price=None,  # if None for LMT, mid-price will be used
        client_id=101,     # choose any free client id
        host="127.0.0.1",
        port=7497          # 7497 is TWS paper; 7496 is live by default
    )
    print(trade)

Notes:
- This function connects to your local TWS/IBG instance in paper mode.
- It auto-picks a near ATM strike around the chosen expiry, using IB option chain.
- If available, it will IMPORT HistoryPull.pull_option_data (uploaded here) to log IV/OI context.
- It places a simple single-leg BUY-to-open option order. You can flip to SELL by passing action="SELL".
- It returns the ib_insync Trade object.
"""
from typing import Optional, Literal
from datetime import datetime, timedelta
import logging

try:
    # Prefer ib_insync if present
    from ib_insync import IB, Stock, Option, util, MarketOrder, LimitOrder
except Exception as e:
    raise ImportError("ib_insync is required. Install with: pip install ib-insync") from e

# Try to import HistoryPull to leverage its IV/OI helper if present
try:
    import HistoryPull  # assumes HistoryPull.py is on PYTHONPATH / same dir
    HAVE_HISTORY_PULL = True
except Exception:
    HAVE_HISTORY_PULL = False

log = logging.getLogger("paper_option_order")
if not log.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

Right = Literal["call", "put", "C", "P"]
OrderType = Literal["MKT", "LMT"]

def _normalize_right(right: Right) -> str:
    if right is None:
        raise ValueError("right must be 'call' or 'put'")
    r = right.strip().lower()
    if r in ("call", "c"):
        return "C"
    if r in ("put", "p"):
        return "P"
    raise ValueError("right must be 'call'/'c' or 'put'/'p'")

def _nearest_expiry(target_days_out: int, expirations: list[str]) -> str:
    """Return the YYYYMMDD string from expirations closest to today + target_days_out, but not earlier than today."""
    today = datetime.utcnow().date()
    target_date = today + timedelta(days=max(0, int(target_days_out)))
    # Parse expirations which are "YYYYMMDD"
    parsed = []
    for e in expirations:
        try:
            d = datetime.strptime(e, "%Y%m%d").date()
            if d >= today:
                parsed.append(d)
        except Exception:
            continue
    if not parsed:
        # fall back to any provided date (even if in past)
        for e in expirations:
            try:
                parsed.append(datetime.strptime(e, "%Y%m%d").date())
            except Exception:
                continue
    if not parsed:
        raise RuntimeError("No usable expirations returned by IB for this symbol.")
    # Choose closest to target_date (tie-breaker = earliest)
    best = min(parsed, key=lambda d: (abs((d - target_date).days), d))
    return best.strftime("%Y%m%d")

def _closest_strike(underlying_price: float, strikes: list[float]) -> float:
    if not strikes:
        raise RuntimeError("No strikes returned by IB for this symbol/expiry.")
    return min(strikes, key=lambda s: abs(s - underlying_price))

def place_paper_option_order(
    ticker: str,
    right: Right,
    quantity: int = 1,
    days_out: int = 14,
    order_type: OrderType = "LMT",
    limit_price: Optional[float] = None,
    action: Literal["BUY", "SELL"] = "BUY",
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 123,
):
    """Place a simple single-leg option order in IB paper trading.

    Returns the ib_insync Trade object (or raises on error).
    """
    right_code = _normalize_right(right)
    ib = IB()
    log.info(f"Connecting to IB @ {host}:{port} (clientId={client_id})")
    ib.connect(host, port, clientId=client_id)
    try:
        # 1) Qualify underlying
        stock = Stock(ticker, "SMART", "USD")
        ib.qualifyContracts(stock)
        ticker_data = ib.reqMktData(stock, "", False, False)
        ib.sleep(1.0)  # let data populate
        last = util.nanToNone(ticker_data.last)
        mid = None
        if util.isNaN(ticker_data.bid) or util.isNaN(ticker_data.ask):
            # Mid cannot be computed; fall back to last
            ul_price = last
        else:
            mid = (ticker_data.bid + ticker_data.ask) / 2.0
            ul_price = mid if mid and mid > 0 else last
        if not ul_price:
            raise RuntimeError("Failed to get an underlying price to choose ATM strike.")
        log.info(f"Underlying {ticker} price context -> last={last} mid={mid} chosen={ul_price}")

        # 2) Get option chain
        chains = ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
        if not chains:
            raise RuntimeError("No option chains returned by IB.")
        # choose SMART chain for USD
        best_chain = None
        for c in chains:
            if c.exchange in ("SMART", "BOX", "CBOE", "BEX", "AMEX", "PHLX"):
                best_chain = c
                break
        if best_chain is None:
            best_chain = chains[0]
        expiry = _nearest_expiry(days_out, best_chain.expirations)
        # strikes come as list[float]; pick closest to ul_price
        strike = _closest_strike(ul_price, list(best_chain.strikes))
        log.info(f"Selected expiry {expiry} and ATM strike {strike}")

        # 3) Build option contract
        opt = Option(stock.symbol, expiry, strike, right_code, "SMART", currency="USD")
        ib.qualifyContracts(opt)

        # 4) Determine price if LMT was requested
        if order_type.upper() == "LMT":
            if limit_price is None:
                t = ib.reqMktData(opt, "", False, False)
                ib.sleep(1.0)
                bid = util.nanToNone(t.bid)
                ask = util.nanToNone(t.ask)
                last_opt = util.nanToNone(t.last)
                if bid and ask and bid > 0 and ask > 0:
                    limit_price = round((bid + ask) / 2.0, 2)
                elif last_opt and last_opt > 0:
                    limit_price = round(last_opt, 2)
                else:
                    # final fallback to a small nonzero price to avoid rejection
                    limit_price = 1.00
                log.info(f"Auto mid-price for option -> bid={bid} ask={ask} last={last_opt} -> limit={limit_price}")
            order = LimitOrder(action, quantity, limit_price)
        else:
            order = MarketOrder(action, quantity)

        # 5) Optional: log IV/OI context via HistoryPull if present
        if HAVE_HISTORY_PULL:
            try:
                ctx = HistoryPull.pull_option_data(stock.symbol, width=3)
                log.info(f"HistoryPull context (IV/OI snapshot around ATM): {ctx}")
            except Exception as e:
                log.warning(f"HistoryPull.pull_option_data failed: {e}")

        # 6) Place order
        trade = ib.placeOrder(opt, order)
        log.info(f"Order submitted: {trade.order}")

        # 7) Briefly wait for status update (non-blocking long waits avoided)
        ib.sleep(1.0)
        log.info(f"Order status now: {trade.orderStatus.status}, filled={trade.orderStatus.filled}")
        return trade
    finally:
        ib.disconnect()

if __name__ == "__main__":
    # Tiny CLI for quick testing:
    import argparse
    p = argparse.ArgumentParser(description="Place an IBKR paper option order (single leg).")
    p.add_argument("--ticker", required=True, help="Underlying symbol, e.g., AAPL")
    p.add_argument("--right", required=True, choices=["call", "put", "C", "P"], help="Call or Put")
    p.add_argument("--qty", type=int, default=1, help="Quantity (number of option contracts)")
    p.add_argument("--days_out", type=int, default=14, help="Target expiration distance in days")
    p.add_argument("--order_type", choices=["MKT", "LMT"], default="LMT", help="Order type")
    p.add_argument("--limit", type=float, default=None, help="Limit price (default: mid-price)")
    p.add_argument("--action", choices=["BUY", "SELL"], default="BUY", help="BUY or SELL")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7497, help="7497 paper / 7496 live")
    p.add_argument("--client_id", type=int, default=123)
    args = p.parse_args()
    trade = place_paper_option_order(
        ticker=args.ticker,
        right=args.right,
        quantity=args.qty,
        days_out=args.days_out,
        order_type=args.order_type,
        limit_price=args.limit,
        action=args.action,
        host=args.host,
        port=args.port,
        client_id=args.client_id,
    )
    print("Submitted:", trade.order)
    print("Status:", trade.orderStatus.status, "Filled:", trade.orderStatus.filled)
