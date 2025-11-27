#!/usr/bin/env python3
"""
Test PlaceAnOrder.csv workflow and theo->real expiry/limit logic (dry run).

- Loads combined_listener_spreads.csv (today's dated folder by default).
- Detects rows using theoretical values.
- Ensures expiration >= 20 DTE by mapping to a real expiry (IB SecDef if available; else simulated Fridays).
- Recomputes debit spread limit via Black–Scholes using updated T (time decay).
- Prints a concise report: old theo limit vs. adjusted limit, chosen expiry, and any issues.

Usage:
  python tests/test_place_and_order.py
  python tests/test_place_and_order.py --csv "/path/to/combined_listener_spreads.csv"
  python tests/test_place_and_order.py --symbols PEP,SONY,COP --min-dte 20 --r 0.03 --iv 0.25

Tip: You can set IB connection via env vars:
  export IB_HOST=127.0.0.1
  export IB_PORTS=7497,7496
"""

from __future__ import annotations
import os, sys, math, argparse, datetime as dt
from typing import Optional, Tuple, List

# --- allow importing PlaceAnOrder helpers without changing PYTHONPATH permanently
REPO_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from InteractiveBrokersTrader.PlaceAnOrder import (  # type: ignore
    combined_csv_path_for_today, find_latest_combined_csv, _clean_symbol,
    best_theoretical_limit
)

import pandas as pd

# ----------- Black–Scholes helpers -----------
from math import log, sqrt, exp
from statistics import fmean

try:
    from mpmath import quad, sqrt as msqrt  # not required; we’ll stick to scipy-less cdf
except Exception:
    pass

# Standard normal CDF (good enough for pricing) – no scipy required
def _phi(x: float) -> float:
    # Abramowitz/Stegun approximation
    # for stability, handle sign
    sign = 1
    if x < 0:
        sign = -1
    x = abs(x)/math.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * x)
    a1,a2,a3,a4,a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    erf = 1.0 - (((((a5*t + a4)*t) + a3)*t + a2)*t + a1)*t*math.exp(-x*x)
    cdf = 0.5*(1.0 + sign*erf)
    return cdf

def _d1_d2(S: float, K: float, r: float, sigma: float, T: float) -> Tuple[float,float]:
    if S <= 0 or K <= 0 or sigma <= 0 or T <= 0:
        return float('nan'), float('nan')
    d1 = (log(S/K) + (r + 0.5*sigma*sigma)*T) / (sigma*sqrt(T))
    d2 = d1 - sigma*sqrt(T)
    return d1, d2

def call_price_bs(S: float, K: float, r: float, sigma: float, T: float) -> float:
    d1, d2 = _d1_d2(S,K,r,sigma,T)
    if math.isnan(d1): return float('nan')
    return S*_phi(d1) - K*exp(-r*T)*_phi(d2)

def put_price_bs(S: float, K: float, r: float, sigma: float, T: float) -> float:
    d1, d2 = _d1_d2(S,K,r,sigma,T)
    if math.isnan(d1): return float('nan')
    return K*exp(-r*T) * _phi(-d2) - S*_phi(-d1)

def debit_vertical(right: str, S: float, longK: float, shortK: float, r: float, sigma: float, T: float) -> float:
    if right.upper() == 'C':
        return call_price_bs(S, longK, r, sigma, T) - call_price_bs(S, shortK, r, sigma, T)
    else:
        return put_price_bs(S, longK, r, sigma, T) - put_price_bs(S, shortK, r, sigma, T)

# ----------- IB optional: expirations via SecDef -----------
def try_ib_expirations(symbol: str) -> List[str]:
    """
    Return sorted list of 'YYYYMMDD' expirations via ib_insync SecDef, or [] on failure.
    """
    try:
        from ib_insync import IB, Stock
        from InteractiveBrokersTrader.listener import _ib_host_and_ports  # reuse helper if present
    except Exception:
        # Fallback: no IB installed or helper not present
        return []

    host, ports = _ib_host_and_ports() if '_ib_host_and_ports' in globals() else ("127.0.0.1", [7497,7496])
    ib = IB()
    last_exc = None
    for p in ports:
        try:
            ib.connect(host, p, clientId=9876)
            break
        except Exception as e:
            last_exc = e
    else:
        return []

    try:
        stock = Stock(symbol, 'SMART', 'USD')
        cdetails = ib.reqContractDetails(stock)
        if not cdetails:
            return []
        conId = cdetails[0].contract.conId
        # Using IB's secDefOptParams:
        params = ib.reqSecDefOptParams(stock.symbol, '', stock.secType, conId)
        expirations = set()
        for prm in params:
            for e in prm.expirations:
                if len(e) == 8 and e.isdigit():
                    expirations.add(e)
        out = sorted(expirations)
        return out
    except Exception:
        return []
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

# ----------- Offline fallback: next N Fridays -----------
def next_fridays(n: int = 32) -> List[str]:
    today = dt.date.today()
    # find next Friday
    days_ahead = (4 - today.weekday()) % 7
    first = today + dt.timedelta(days=days_ahead)
    fridays = [first + dt.timedelta(days=7*i) for i in range(n)]
    return [d.strftime("%Y%m%d") for d in fridays]

def dte(exp_str: str) -> int:
    try:
        d = dt.datetime.strptime(exp_str, "%Y%m%d").date()
        return (d - dt.date.today()).days
    except Exception:
        return -10**9

# ----------- CSV + test logic -----------
def load_csv(path_arg: Optional[str]) -> pd.DataFrame:
    if path_arg:
        return pd.read_csv(path_arg)
    # Prefer today's dated folder; if missing, use most recent
    path_today = combined_csv_path_for_today(None)
    if os.path.exists(path_today):
        return pd.read_csv(path_today)
    latest = find_latest_combined_csv()
    if latest and os.path.exists(latest):
        return pd.read_csv(latest)
    raise FileNotFoundError("Could not locate combined_listener_spreads.csv (today or latest).")

def choose_expiry_ge_min(symbol: str, min_dte: int) -> Tuple[str, str]:
    """
    Return (chosen_expiry, source) where source is 'IB' or 'FRIDAYS'.
    """
    exps = try_ib_expirations(symbol)
    if exps:
        valid = [e for e in exps if dte(e) >= min_dte]
        if valid:
            return min(valid, key=lambda e: dte(e)), "IB"
    # fallback to Fridays
    fr = next_fridays(40)
    valid = [e for e in fr if dte(e) >= min_dte]
    return (min(valid, key=lambda e: dte(e)), "FRIDAYS") if valid else (fr[-1], "FRIDAYS")

def first_non_nan(vals: List[Optional[float]]) -> Optional[float]:
    for v in vals:
        try:
            if v is None:
                continue
            f = float(v)
            if math.isnan(f) or f in (float('inf'), float('-inf')):
                continue
            return f
        except Exception:
            continue
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None, help="Path to combined_listener_spreads.csv (optional)")
    ap.add_argument("--symbols", default=None, help="Comma-separated tickers to include")
    ap.add_argument("--min-dte", type=int, default=20, help="Minimum DTE when choosing a real expiry")
    ap.add_argument("--r", type=float, default=0.03, help="Risk-free rate for BS")
    ap.add_argument("--iv", type=float, default=None, help="Override IV if CSV lacks it; default 0.25")
    ap.add_argument("--target-theo-days", type=int, default=30, help="Assumed theoretical T (days) that CSV theo debits were based on")
    args = ap.parse_args()

    df = load_csv(args.csv)

    # Normalize symbols and filter
    if "symbol" not in df.columns:
        raise ValueError("CSV missing 'symbol' column")
    df["SYMBOL"] = df["symbol"].astype(str).map(lambda s: (_clean_symbol(s) or s).upper())

    only = None
    if args.symbols:
        only = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        df = df[df["SYMBOL"].isin(only)]

    # quick column presence
    needed = ["expiration","atm_strike","otm_strike_call","otm_strike_put","current_price"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        print(f"[WARN] CSV missing columns: {missing} — test will skip pricing where inputs are missing.")

    T_theo = max(args.target_theo_days, 1) / 365.0

    results = []
    for _, row in df.iterrows():
        sym = row["SYMBOL"]
        S = first_non_nan([row.get("current_price"), row.get("last"), row.get("close")])

        # Read ATM/OTM strikes
        atm = first_non_nan([row.get("atm_strike")])
        kC  = first_non_nan([row.get("otm_strike_call")])
        kP  = first_non_nan([row.get("otm_strike_put")])

        # Skip if we cannot price
        if S is None or atm is None or (kC is None and kP is None):
            results.append((sym, "SKIP", "inputs-missing", None))
            continue

        # detect theoretical usage (any theo columns)
        theo_cols = [
            "call_debit_theo_1","call_debit_theo_2_5","call_debit_theo_5",
            "put_debit_theo_1","put_debit_theo_2_5","put_debit_theo_5",
            "_theo_only"
        ]
        theo_used = any(c in df.columns and pd.notna(row.get(c)) for c in theo_cols)

        # choose expiry >= min_dte
        chosen_exp, source = choose_expiry_ge_min(sym, args.min_dte)
        dte_chosen = dte(chosen_exp)
        T_actual = max(dte_chosen, 1) / 365.0

        # IV resolution: prefer CSV ATM implied vols, else override, else 0.25
        iv_csv_candidates = [
            row.get("implied_volatility_atm"),
            row.get("impliedVolatility"),  # sometimes named differently
        ]
        sigma = first_non_nan(iv_csv_candidates)
        if sigma is None:
            sigma = args.iv if args.iv is not None else 0.25

        # Theoretical “old” limits from CSV (for comparison)
        theo_call = first_non_nan([row.get("call_debit_theo_2_5"), row.get("call_debit_theo_1"), row.get("call_debit_theo_5")])
        theo_put  = first_non_nan([row.get("put_debit_theo_2_5"),  row.get("put_debit_theo_1"),  row.get("put_debit_theo_5")])

        # Recompute adjusted limits with updated T using Black–Scholes
        adj_call = adj_put = None
        if kC is not None:
            adj_call = round(debit_vertical('C', S, float(atm), float(kC), args.r, float(sigma), T_actual), 2)
        if kP is not None:
            adj_put = round(debit_vertical('P', S, float(atm), float(kP), args.r, float(sigma), T_actual), 2)

        results.append({
            "symbol": sym,
            "theo_used": bool(theo_used),
            "csv_expiration": str(row.get("expiration")),
            "chosen_expiration": chosen_exp,
            "expiry_source": source,
            "dte_chosen": dte_chosen,
            "S": round(float(S), 4),
            "atm": float(atm),
            "k_call": float(kC) if kC is not None else None,
            "k_put": float(kP) if kP is not None else None,
            "sigma_used": float(sigma),
            "r": float(args.r),
            "T_theo_days": int(args.target_theo_days),
            "T_actual_days": int(max(dte_chosen,1)),
            "theo_call_from_csv": theo_call,
            "theo_put_from_csv": theo_put,
            "adj_call_limit": adj_call,
            "adj_put_limit": adj_put,
        })

    # pretty print
    if not results:
        print("No rows to test.")
        return

    # Tabular output
    out_df = pd.DataFrame(results)
    cols = [
        "symbol","theo_used","csv_expiration","chosen_expiration","expiry_source","dte_chosen",
        "S","atm","k_call","k_put","sigma_used","r","T_theo_days","T_actual_days",
        "theo_call_from_csv","adj_call_limit","theo_put_from_csv","adj_put_limit"
    ]
    cols = [c for c in cols if c in out_df.columns]
    out_df = out_df[cols].sort_values(["symbol"]).reset_index(drop=True)

    # Simple assertions: chosen DTE >= min_dte for all with theo_used
    violations = out_df[(out_df["theo_used"] == True) & (out_df["dte_chosen"] < args.min_dte)]
    if len(violations):
        print("\n[FAIL] Some theo rows mapped to expiry < min DTE:\n")
        print(violations.to_string(index=False))
    else:
        print("\n[OK] All theo rows have chosen expiration ≥ min DTE.\n")

    # Show first 40 rows succinctly
    with pd.option_context('display.max_rows', 40, 'display.width', 150):
        print(out_df.to_string(index=False))

if __name__ == "__main__":
    main()