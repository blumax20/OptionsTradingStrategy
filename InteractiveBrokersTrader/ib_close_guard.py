# ib_close_guard.py
import logging
from typing import Set
from ib_config import IB_HOST, IB_PORT

LOG = logging.getLogger(__name__)

def has_working_auto_close(symbol: str,
                           client_id: int = 884,  # Fix U3: was 883, avoid collision with DCM
                           host: str = IB_HOST,
                           port: int = IB_PORT) -> bool:
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
        working_states: Set[str] = {
            "presubmitted", "submitted", "pendingsubmit", "apipending"
        }

        def _scan(trades) -> bool:
            for tr in trades:
                c = getattr(tr, "contract", None)
                o = getattr(tr, "order", None)
                s = getattr(tr, "orderStatus", None)
                if not (c and o and s):
                    continue
                if (getattr(c, "symbol", "") or "").upper() != sym_u:
                    continue
                # Fix AB6: Only SELL orders are closes; BUY = OPEN and must not block.
                if (getattr(o, "action", "") or "").upper() != "SELL":
                    continue
                # SELL BAG = combo close (Fix AB6). Individual OPT SELL = worthless-leg
                # close path (Fix AV1). Both count as a working close for this symbol.
                if getattr(c, "secType", "") not in ("BAG", "OPT"):
                    continue
                st = (getattr(s, "status", "") or "").lower()
                if st in ("filled", "cancelled", "apicancelled"):
                    continue
                # GTC/DAY orders with outsideRth go Inactive after hours but are still working.
                tif = (getattr(o, "tif", "") or "").upper()
                if (st in working_states) or (st == "inactive" and tif in ("GTC", "DAY")):
                    return True
            return False

        # Fix FH: poll reqAllOpenOrders a few times over ~5s, returning the instant a working
        # close is found. A single 1.5s sleep can miss orders IB hasn't finished syncing to
        # this fresh connection right after an IB Gateway restart — that lag let a duplicate
        # close slip through (EIX/NI, Aug 5). A symbol that already has a close returns fast;
        # only a symbol with no existing close pays the extra polling.
        for dwell in (1.5, 1.5, 1.0, 1.0):
            ib.reqAllOpenOrders()  # Fix U1: see orders from ALL client IDs
            ib.sleep(dwell)
            if _scan(ib.openTrades() or []):
                return True
        return False
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass