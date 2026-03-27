"""
PrewarmApiConnections.py
Fix DG: Pre-warm all IB API clientIds after IBGateway restart.

Connects briefly with each clientId used by the trading system so IBGateway
recognises them all before the trading week begins. On a live IB account,
the first connection from an unrecognised clientId can trigger an approval
dialog — running this once after restart prevents that from happening
mid-trading-day (e.g. at the 3 PM preclose).

Run via PrewarmConnections.cmd after IBGateway restarts (e.g. Sunday 6:30 PM
ColdRestart). The OptionsListener service (clientId=42) is excluded — it
connects automatically when the service starts.

ClientId registry:
  42   listener.py                   (OptionsListener service — excluded)
  101  PlaceAnOrder, cancel funcs
  878  _rth_risk_exits()
  881  flatten-stock
  883  _has_working_close_order()
  884  ib_close_guard.has_working_auto_close()
  885  position filter / _get_theo_limit
  886  _get_theo_limit / related
  887  _working_close_limit_symbols()
  890  _collect_held_orientations()
  892  credit-scan
"""

import sys
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(r"C:\OptionsHistory\logs\prewarm.log", encoding="ascii"),
        logging.StreamHandler(sys.stdout),
    ],
)
LOG = logging.getLogger("prewarm")

# All clientIds used by the system except 42 (listener handles its own reconnect)
CLIENT_IDS = [101, 878, 881, 883, 884, 885, 886, 887, 890, 892]

PAUSE_BETWEEN = 0.8   # seconds between connections
HOLD_DURATION = 0.5   # seconds to hold each connection open


def prewarm(host: str, port: int) -> None:
    try:
        from ib_insync import IB
    except ImportError:
        LOG.error("ib_insync not available — cannot prewarm")
        sys.exit(1)

    LOG.info("Starting prewarm on %s:%d for clientIds: %s", host, port, CLIENT_IDS)
    ok = 0
    fail = 0

    for cid in CLIENT_IDS:
        ib = IB()
        try:
            ib.connect(host, port, clientId=cid, timeout=8)
            ib.sleep(HOLD_DURATION)
            LOG.info("  clientId=%-4d  OK", cid)
            ok += 1
        except Exception as e:
            LOG.warning("  clientId=%-4d  FAILED: %s", cid, e)
            fail += 1
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass
        time.sleep(PAUSE_BETWEEN)

    LOG.info("Prewarm complete: %d OK, %d failed", ok, fail)


if __name__ == "__main__":
    IB_CONFIG = (
        r"C:\Users\Administrator\code\OptionsTradingStrategy\InteractiveBrokersTrader"
    )
    sys.path.insert(0, IB_CONFIG)
    try:
        from ib_config import IB_HOST, IB_PORT
    except ImportError:
        IB_HOST = "127.0.0.1"
        IB_PORT = 7496

    prewarm(IB_HOST, IB_PORT)
