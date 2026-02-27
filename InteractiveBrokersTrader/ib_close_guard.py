# ib_close_guard.py
import logging
from typing import Set

LOG = logging.getLogger(__name__)

def has_working_auto_close(symbol: str,
                           client_id: int = 884,  # Fix U3: was 883, avoid collision with DCM
                           host: str = "127.0.0.1",
                           port: int = 7497) -> bool:
    """
    Return True if there is already a working combo order (BAG) for this symbol.

    This no longer relies on orderRef/prefix; it just checks:
      - secType == 'BAG'
      - contract.symbol == symbol
      - status is in a working/pre-working state (or inactive+GTC).
    """
    try:
        from ib_insync import IB
    except Exception as e:
        LOG.warning("close-guard: ib_insync unavailable: %s", e)
        return False

    sym_u = (symbol or "").strip().upper()
    if not sym_u:
        return False

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=6)
    except Exception as e:
        LOG.warning("close-guard: connect failed: %s", e)
        return False

    try:
        # Must request all open orders first to see orders from other client IDs
        ib.reqAllOpenOrders()
        ib.sleep(1.5)  # Fix AP: was 0.5 — allow more time for IB to propagate cross-clientId Inactive+DAY orders
        trades = ib.openTrades() or []
        working_states: Set[str] = {
            "presubmitted", "submitted", "pendingsubmit", "apipending"
        }

        for tr in trades:
            c = getattr(tr, "contract", None)
            o = getattr(tr, "order", None)
            s = getattr(tr, "orderStatus", None)
            if not (c and o and s):
                continue

            # Only look at option combos (verticals etc.).
            if getattr(c, "secType", "") != "BAG":
                continue

            if (getattr(c, "symbol", "") or "").upper() != sym_u:
                continue

            # Fix AB6: Only SELL BAG orders are closes (debit spread unwind).
            # BUY BAG = OPEN order; should NOT block close placement.
            act = (getattr(o, "action", "") or "").upper()
            if act != "SELL":
                continue

            st = (getattr(s, "status", "") or "").lower()
            if st in ("filled", "cancelled", "apicancelled"):
                continue

            # GTC but "inactive" after-hours should still count as working/held.
            # DAY orders with outsideRth also go Inactive after hours (Fix AB5/AB6).
            tif = (getattr(o, "tif", "") or "").upper()
            if (st in working_states) or (st == "inactive" and tif in ("GTC", "DAY")):
                return True

        return False
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass