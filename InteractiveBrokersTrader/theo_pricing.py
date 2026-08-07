# theo_pricing.py
# Pure Black-Scholes theo spread pricing, shared so the enrichment (LiquidityFilter)
# can recompute debit-spread theo values with real per-leg IV.
#
# These functions are copied verbatim from listener.py (which cannot be imported —
# it runs util.startLoop() + IB() at module import). They are pure math with no IB
# dependency. Behavior matches listener.py exactly:
#   - Fix P:  separate ATM (long) and OTM (short) IVs (skew-aware)
#   - Fix Y2: clamp theo >= 0
#   - Fix CY: clamp theo <= 0.75 * width
import math
from typing import Dict


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(S: float, K: float, T: float, r: float, sigma: float, call: bool = True) -> float:
    if T <= 0 or sigma <= 0:
        intrinsic = max(S - K, 0.0) if call else max(K - S, 0.0)
        return intrinsic
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    if call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _theo_spread_debits(S: float, atm: float, T: float, sigma_atm: float,
                        sigma_otm: float | None = None,
                        r: float = 0.045, widths=(1.0, 2.5, 5.0)) -> Dict[str, float]:
    """Calculate theoretical debit spread prices using Black-Scholes.

    Args:
        sigma_atm: IV for ATM (long) leg
        sigma_otm: IV for OTM (short) leg - defaults to sigma_atm if None
    """
    if sigma_otm is None:
        sigma_otm = sigma_atm

    out: Dict[str, float] = {}
    for W in widths:
        call_long = _bs_price(S, atm, T, r, sigma_atm, call=True)
        call_short = _bs_price(S, atm + W, T, r, sigma_otm, call=True)
        put_long  = _bs_price(S, atm, T, r, sigma_atm, call=False)
        put_short = _bs_price(S, max(atm - W, 0.01), T, r, sigma_otm, call=False)
        key = "2_5" if abs(W - 2.5) < 1e-9 else str(int(W))
        # Fix Y2b: clamp to >= 0 (debit spread value cannot be negative)
        # Fix CY: clamp to <= 0.75*W (no-arbitrage + conservative cap; skewed IV can exceed width)
        out[f"call_debit_theo_{key}"] = min(0.75 * W, max(0.0, float(call_long - call_short)))
        out[f"put_debit_theo_{key}"]  = min(0.75 * W, max(0.0, float(put_long - put_short)))
    return out
