"""
Test whether this IB account allows individual OPT SELL for a held long position.

This version specifically targets NWG's long leg from a held spread position.
It does NOT cancel the order at the end — leave it pending so you can verify in TWS.

IB accepts orders in two stages:
  1. Initial: order goes to Inactive/PreSubmitted immediately
  2. Async: IB's risk engine may then cancel it (Error 201) 1-2s later

This test waits 4s after initial acceptance to detect async cancellations.

Usage:
    .venv\Scripts\python.exe test_individual_option_sell.py
"""
import sys
sys.path.insert(0, "InteractiveBrokersTrader")

from ib_insync import IB, LimitOrder
from ib_config import IB_HOST, IB_PORT


def test_individual_sell():
    ib = IB()
    print(f"Connecting to {IB_HOST}:{IB_PORT} ...")
    ib.connect(IB_HOST, IB_PORT, clientId=199, timeout=10)
    ib.sleep(1.0)

    # Show ALL option positions (both long and short legs)
    all_opts = [
        (p.contract, int(p.position))
        for p in ib.positions()
        if p.contract.secType == "OPT"
    ]

    if not all_opts:
        print("No option positions found.")
        ib.disconnect()
        return

    print(f"\nAll option positions ({len(all_opts)} legs):")
    for i, (c, qty) in enumerate(all_opts):
        side = "LONG" if qty > 0 else "SHORT"
        print(f"  [{i}] {c.symbol} {c.right} {c.strike} "
              f"exp={c.lastTradeDateOrContractMonth} qty={qty} ({side})")

    # Find NWG long leg specifically
    nwg_long = [
        (c, qty)
        for c, qty in all_opts
        if c.symbol.upper() == "NWG" and qty > 0
    ]

    if not nwg_long:
        print("\nNo NWG long option position found.")
        print("Available long positions:")
        long_opts = [(c, qty) for c, qty in all_opts if qty > 0]
        for i, (c, qty) in enumerate(long_opts):
            print(f"  [{i}] {c.symbol} {c.right} {c.strike} qty={qty}")
        if not long_opts:
            print("  (none)")
            ib.disconnect()
            return
        print("\nFalling back to first long position.")
        c, qty = long_opts[0]
    else:
        c, qty = nwg_long[0]
        print(f"\nFound NWG long leg: {c.right} {c.strike} exp={c.lastTradeDateOrContractMonth} qty={qty}")

    print(f"\nTesting individual SELL for: {c.symbol} {c.right} {c.strike} "
          f"exp={c.lastTradeDateOrContractMonth} qty={qty}")

    # Qualify the contract (fills in localSymbol, tradingClass, multiplier)
    qualified = ib.qualifyContracts(c)
    if qualified:
        c = qualified[0]
        print(f"  Qualified: conId={c.conId} localSymbol={c.localSymbol} "
              f"tradingClass={c.tradingClass} exchange={c.exchange}")

    order = LimitOrder("SELL", qty, 0.05)
    order.tif = "DAY"
    order.outsideRth = True
    order.openClose = "C"

    print(f"\nPlacing SELL {qty} x {c.symbol} {c.right} {c.strike} @ $0.05 ...")
    trade = ib.placeOrder(c, order)
    print("Phase 1: checking for immediate rejection (0-3s)...")

    initially_accepted = False
    accepted_at = None

    for i in range(30):
        ib.sleep(0.1)
        errors = [
            e for e in trade.log
            if ("rejected" in str(getattr(e, "message", "")).lower()
                or "trading permissions" in str(getattr(e, "message", "")).lower()
                or "201" in str(getattr(e, "message", "")))
        ]
        if errors:
            t_sec = (i + 1) * 0.1
            print(f"\n❌ Immediate rejection at t={t_sec:.1f}s: {errors[0].message}")
            print("Full log:")
            for entry in trade.log:
                print(f"  {entry}")
            ib.disconnect()
            return "error_immediate"

        st = trade.orderStatus.status
        if st in ("PreSubmitted", "Submitted", "Inactive"):
            accepted_at = (i + 1) * 0.1
            initially_accepted = True
            print(f"  Initial status={st} at t={accepted_at:.1f}s — waiting 4s for async cancel check...")
            break

    if not initially_accepted:
        print(f"\n? No response after 3s — status: {trade.orderStatus.status}")
        print("Full log:")
        for entry in trade.log:
            print(f"  {entry}")
        ib.disconnect()
        return None

    # Phase 2: wait for async cancel (IB's risk engine may reject 1-2s after Inactive)
    print("Phase 2: waiting 4s to detect async cancellation...")
    for i in range(40):
        ib.sleep(0.1)
        errors = [
            e for e in trade.log
            if ("rejected" in str(getattr(e, "message", "")).lower()
                or "trading permissions" in str(getattr(e, "message", "")).lower()
                or "201" in str(getattr(e, "message", "")))
        ]
        if errors:
            t_sec = accepted_at + (i + 1) * 0.1
            print(f"\n❌ Async Error 201 at t={t_sec:.1f}s total: {errors[0].message}")
            print("Full log:")
            for entry in trade.log:
                print(f"  {entry}")
            print("\n→ IB initially accepts then async-cancels individual OPT SELL.")
            print("→ This is an account permissions issue (not a time-of-day issue).")
            print("→ Fix CK (BAG combo fallback) is the correct approach for this account.")
            ib.disconnect()
            return "error_async"

        st = trade.orderStatus.status
        if st.lower() in ("cancelled", "apicancelled"):
            t_sec = accepted_at + (i + 1) * 0.1
            print(f"\n❌ Order cancelled at t={t_sec:.1f}s (status={st}) — no explicit Error 201 in log.")
            print("   IB cancelled without error message — same result: individual SELL rejected.")
            print("Full log:")
            for entry in trade.log:
                print(f"  {entry}")
            print("\n→ Fix CK (BAG combo fallback) is the correct approach for this account.")
            ib.disconnect()
            return "cancelled_silently"

    # Survived 4s without cancellation
    final_st = trade.orderStatus.status
    print(f"\n✓ Order still active after 4s (status={final_st})")
    print("→ Individual OPT SELL WORKS on this account — Fix CK BAG fallback not needed.")
    print("→ Order left pending — check TWS to confirm it appears there.")
    print("   (Cancel it manually in TWS when done.)")
    # NOTE: NOT disconnecting immediately — give the connection a moment so IB
    # has time to persist the order before we drop the session.
    ib.sleep(2.0)
    ib.disconnect()
    return "ok"


if __name__ == "__main__":
    test_individual_sell()
