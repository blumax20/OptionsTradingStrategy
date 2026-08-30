#!/usr/bin/env python
"""MonthlyReview.py - read-only monthly performance & efficiency report.

Usage:
    python MonthlyReview.py [--month 26_08] [--live] [--out]

Sections:
    A. Performance      - daily P/L trajectory + month NetLiq delta (authoritative)
    B. Trade ledger     - fill-confirmed spreads (opened / closed / still held)
    C. Execution funnel - signals -> attempted -> placed -> filled, skip reasons
    D. Opportunity cost - rough what-if for never-executed opens (--live only)

Data sources (never modified):
    C:\\OptionsHistory\\logs\\health_YYYYMMDD_*.txt   (P/L Summary + Current Positions)
    C:\\OptionsHistory\\<YY_MM_DD>\\attempts_<YY_MM_DD>.csv
    C:\\OptionsHistory\\<YY_MM_DD>\\combined_listener_spreads.csv
    (--live) IB via ib_insync, clientId=960, read-only historical price queries.

Measurement notes:
    - Performance = month-over-month NetLiq delta (from IB's own P/L Summary in the
      health reports). Per-trade P/L here is ESTIMATED from submitted limits/avgCost;
      do NOT sum the health report's "Last 20 Orders Closed (est.)" rows - unfilled
      close attempts repeat there (e.g. ISRG appeared 12x for one spread).
    - A spread counts as FILLED only when it appears in a Current Positions snapshot;
      it counts as CLOSED when it later disappears from the snapshots.
"""
import argparse
import csv
import glob
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

OH = r"C:\OptionsHistory"
LOGS = os.path.join(OH, "logs")

SKIP_CATEGORIES = {
    "wide_spread_after_hours": "liquidity_gate",
    "oi_below_threshold": "liquidity_gate",
    "low_oi_both_legs": "liquidity_gate",
    "low_oi_live": "liquidity_gate",
    "skip_open_same_side_position": "by_design",
    "skip_open_reconcile_close_submitted": "by_design",
    "skip_open_opposite_no_close_yet": "by_design",
    "working_order": "by_design",
    "bs_rejected_bag_e201": "account_blocked",
    "no_viable_limit_or_conditions": "not_priceable",
    "qualify_failed": "not_priceable",
}

OPEN_ACTIONS = ("open", "open_call", "open_put")
CLOSE_ACTIONS = ("close", "close_call", "close_put", "force_close", "close_individual_leg")


# ---------------------------------------------------------------- helpers

def read_txt(path):
    """Read a health report, handling both UTF-16LE (BOM) and UTF-8 files."""
    with open(path, "rb") as f:
        b = f.read()
    if b[:2] == b"\xff\xfe":
        return b.decode("utf-16-le", errors="replace")
    return b.decode("utf-8", errors="replace")


def money(s):
    """Parse '(64.35)' / '64.35' / '$64.35' -> float; None on failure."""
    if s is None:
        return None
    s = str(s).strip().replace("$", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def fmt(v, width=9):
    """ASCII currency: negatives in parentheses."""
    if v is None:
        return "-".rjust(width)
    if v < 0:
        return ("({:.2f})".format(-v)).rjust(width)
    return ("{:.2f}".format(v)).rjust(width)


def reason_prefix(reason):
    return (reason or "").split(":")[0]


def parse_positions(txt):
    """Parse the Current Positions section into {spread_key: (avgCostLong, avgCostShort)}.

    spread_key = (symbol, right, exp, longK, shortK).
    Returns None when the section is absent or the report shows an IB outage.
    """
    i = txt.find("--- Current Positions")
    if i < 0:
        return None
    end = txt.find("==== END", i)
    sec = txt[i:end if end > 0 else i + 40000]
    spreads = {}
    header = None
    legs = {}
    for line in sec.splitlines():
        m = re.match(r"^(\w+) (\d{8}) ([CP]) Vertical", line)
        if m:
            header = (m.group(1), m.group(3), m.group(2))
            legs = {}
            continue
        m2 = re.match(
            r"\s+(LONG|SHORT)\s+strike=([\d.]+)\s+exp=(\d{8})\s+right=([CP])"
            r"\s+posQty=(-?[\d.]+)\s+avgCost=([\d.]+)", line)
        if m2 and header:
            legs[m2.group(1)] = (float(m2.group(2)), float(m2.group(6)))
            if "LONG" in legs and "SHORT" in legs:
                key = (header[0], header[1], header[2],
                       legs["LONG"][0], legs["SHORT"][0])
                spreads[key] = (legs["LONG"][1], legs["SHORT"][1])
                legs = {}
    if not spreads and ("not connected" in sec or "positions_error" in txt):
        return None  # IB outage snapshot - unusable
    return spreads


def load_health(month_yyyymm, prev_lookback=12):
    """Return (daily_pl, snapshots, baseline_pl).

    daily_pl:  [(day 'YYYYMMDD', realized, unrealized, day_total, ytd_chg)] last report/day
    snapshots: [('YYYYMMDD_HHMMSS', {spread_key: costs})] all usable snapshots incl. baseline
    baseline_pl: last (day, ytd_chg) strictly before the month, or None
    """
    all_files = sorted(glob.glob(os.path.join(LOGS, "health_*.txt")))
    month_files, prior_files = [], []
    for p in all_files:
        m = re.match(r"health_(\d{8})_(\d{6})", os.path.basename(p))
        if not m:
            continue
        day = m.group(1)
        if day[:6] == month_yyyymm:
            month_files.append((day, m.group(2), p))
        elif day < month_yyyymm + "00":
            prior_files.append((day, m.group(2), p))
    prior_files = prior_files[-prev_lookback:]

    def pl_from(txt):
        g = {}
        for label, key in (("Realized \\(day\\)", "realized"),
                           ("Unrealized \\(open\\)", "unrealized"),
                           ("Day Total", "day_total"),
                           ("YTD change NetLiq", "ytd")):
            m = re.search(label + r"\s*:\s*([^\r\n]+)", txt)
            g[key] = money(m.group(1)) if m else None
        return g

    daily = {}
    snapshots = []
    baseline_pl = None
    baseline_snap = None

    for day, t, p in prior_files:
        txt = read_txt(p)
        g = pl_from(txt)
        if g["ytd"] is not None:
            baseline_pl = (day, g["ytd"])
        pos = parse_positions(txt)
        if pos is not None:
            baseline_snap = (day + "_" + t, pos)

    for day, t, p in month_files:
        txt = read_txt(p)
        g = pl_from(txt)
        if g["ytd"] is not None:
            daily[day] = (day, g["realized"], g["unrealized"], g["day_total"], g["ytd"])
        pos = parse_positions(txt)
        if pos is not None:
            snapshots.append((day + "_" + t, pos))

    if baseline_snap:
        snapshots.insert(0, baseline_snap)
    return [daily[d] for d in sorted(daily)], snapshots, baseline_pl


def load_attempts(month_prefix):
    """All attempts rows for the month. month_prefix like '26_08'."""
    rows = []
    for d in sorted(glob.glob(os.path.join(OH, month_prefix + "_*"))):
        day = os.path.basename(d)
        p = os.path.join(d, "attempts_{}.csv".format(day))
        if not os.path.exists(p):
            continue
        with open(p, newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                r["_day"] = day
                rows.append(r)
    return rows


def load_signals(month_prefix):
    """All listener signal rows for the month."""
    rows = []
    for d in sorted(glob.glob(os.path.join(OH, month_prefix + "_*"))):
        day = os.path.basename(d)
        p = os.path.join(d, "combined_listener_spreads.csv")
        if not os.path.exists(p):
            continue
        with open(p, newline="", encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                r["_day"] = day
                rows.append(r)
    return rows


# ---------------------------------------------------------------- sections

def section_performance(out, daily, baseline_pl, month_label):
    out.append("=== A. PERFORMANCE ({}) ===".format(month_label))
    out.append("")
    out.append("day        realized unrealized  day_total    ytd_chg")
    for day, real, unrl, dtot, ytd in daily:
        out.append("{}  {} {} {} {}".format(
            day, fmt(real), fmt(unrl, 10), fmt(dtot, 10), fmt(ytd, 10)))
    out.append("")
    realized_sum = sum(r for _, r, _, _, _ in daily if r is not None)
    if baseline_pl and daily:
        start = baseline_pl[1]
        end = daily[-1][4]
        delta = end - start
        peak = max(y for *_, y in daily)
        trough = min(y for *_, y in daily)
        out.append("Month NetLiq delta (authoritative): {}  (YTD chg {} -> {})".format(
            fmt(delta, 0).strip(), fmt(start, 0).strip(), fmt(end, 0).strip()))
        out.append("Intramonth peak/trough (YTD chg)  : {} / {}".format(
            fmt(peak, 0).strip(), fmt(trough, 0).strip()))
    out.append("Sum of IB daily Realized           : {}".format(fmt(realized_sum, 0).strip()))
    out.append("")
    out.append("NOTE: NetLiq delta is the performance number. Per-trade P/L below is")
    out.append("estimated from submitted limits/avgCost; unfilled attempts are excluded.")
    out.append("")


def build_ledger(snapshots):
    """Track spreads across snapshots.

    Returns dict spread_key -> {first, last, costs, closed_after} where
    closed_after is the ts of the first snapshot where the spread is absent
    after having been present (None if held through the final snapshot).
    """
    ledger = {}
    for ts, spreads in snapshots:
        for key, costs in spreads.items():
            e = ledger.setdefault(key, {"first": ts, "last": ts, "costs": costs,
                                        "closed_after": None})
            e["last"] = ts
            e["costs"] = costs
    # find close timestamps
    all_ts = [ts for ts, _ in snapshots]
    for key, e in ledger.items():
        later = [ts for ts in all_ts if ts > e["last"]]
        e["closed_after"] = later[0] if later else None
    return ledger


def match_close_limit(attempts, key, close_ts):
    """Best-effort exit price: last close-attempt limit for the symbol near close_ts."""
    sym = key[0]
    close_day = close_ts.split("_")[0]  # YYYYMMDD
    day_folder = "{}_{}_{}".format(close_day[2:4], close_day[4:6], close_day[6:8])
    best = None
    for r in attempts:
        if r.get("symbol") != sym:
            continue
        if r.get("action") not in CLOSE_ACTIONS:
            continue
        if r.get("status") not in ("placed", "submitted"):
            continue
        if r["_day"] > day_folder:
            continue
        lim = money(r.get("limit"))
        if lim is None:
            continue
        best = (r["_day"], lim)
    return best[1] if best else None


def find_entry_ts(attempts_hist, key):
    """Earliest successful open placement matching this spread. Returns 'YYYY-MM-DD...' or None."""
    sym, right, exp, lk, sk = key
    best = None
    for r in attempts_hist:
        if r.get("symbol") != sym or r.get("action") not in OPEN_ACTIONS:
            continue
        if r.get("status") not in ("placed", "submitted"):
            continue
        if "success" not in (r.get("reason") or ""):
            continue
        if (r.get("exp") or "").strip() != exp:
            continue
        rl = money(r.get("longK"))
        if rl is None or lk is None or abs(rl - lk) > 0.01:
            continue
        ts = r.get("ts") or ""
        if ts and (best is None or ts < best):
            best = ts
    return best


def entry_dte(attempts_hist, key):
    """Days-to-expiration at entry, from the matched open placement. None if unknown."""
    ts = find_entry_ts(attempts_hist, key)
    if not ts:
        return None
    try:
        d0 = datetime.strptime(ts[:10], "%Y-%m-%d")
        dx = datetime.strptime(key[2], "%Y%m%d")
        return (dx - d0).days
    except ValueError:
        return None


def dte_bucket(dte):
    if dte is None:
        return "unknown"
    if dte < 30:
        return "<30d"
    if dte <= 45:
        return "30-45d"
    if dte <= 60:
        return "46-60d"
    return ">60d"


def section_ledger(out, ledger, snapshots, attempts, month_start_ts, attempts_hist):
    out.append("=== B. TRADE LEDGER (fill-confirmed via position snapshots) ===")
    out.append("")
    baseline_keys = set(snapshots[0][1].keys()) if snapshots else set()

    opened, closed, still_held = [], [], []
    for key, e in sorted(ledger.items()):
        entry_cost = e["costs"][0] - e["costs"][1]  # dollars (avgCost incl x100)
        item = (key, e, entry_cost)
        if key not in baseline_keys and e["first"] >= month_start_ts:
            opened.append(item)
        if e["closed_after"] is not None:
            closed.append(item)
        else:
            still_held.append(item)

    def label(key):
        return "{:6s} {} {} {:g}/{:g}".format(key[0], key[1], key[2], key[3], key[4])

    out.append("-- Closed this month ({}) --".format(len(closed)))
    wins, losses = [], []
    by_dte = defaultdict(list)
    for key, e, cost in sorted(closed, key=lambda x: x[1]["closed_after"]):
        close_day = e["closed_after"].split("_")[0]
        expired = close_day >= key[2]  # disappeared on/after expiration date
        if expired:
            # No STK residue in snapshots -> expired worthless (ITM expiry would
            # auto-exercise into stock). Exit ~ $0.
            exit_lim, tag = 0.0, " (expired)"
        else:
            exit_lim, tag = match_close_limit(attempts, key, e["closed_after"]), ""
        pl = (exit_lim * 100 - cost) if exit_lim is not None else None
        dte = entry_dte(attempts_hist, key)
        if pl is not None:
            (wins if pl >= 0 else losses).append(pl)
            by_dte[dte_bucket(dte)].append(pl)
        out.append("  {}  entry={}  exit_lim={}  est P/L={}  dte@entry={}{}".format(
            label(key), fmt(cost, 0).strip(),
            "{:.2f}".format(exit_lim) if exit_lim is not None else "?",
            fmt(pl, 0).strip(),
            dte if dte is not None else "?", tag))
    if wins or losses:
        n = len(wins) + len(losses)
        out.append("")
        out.append("  est win rate: {}/{} ({:.0f}%)   avg win: {}   avg loss: {}".format(
            len(wins), n, 100.0 * len(wins) / n,
            fmt(sum(wins) / len(wins), 0).strip() if wins else "-",
            fmt(sum(losses) / len(losses), 0).strip() if losses else "-"))
        gw, gl = sum(wins), -sum(losses)
        if gl > 0:
            out.append("  est profit factor: {:.2f}".format(gw / gl))
    out.append("")

    out.append("-- Closed-trade results by DTE at entry --")
    for b in ("<30d", "30-45d", "46-60d", ">60d", "unknown"):
        pls = by_dte.get(b)
        if not pls:
            continue
        w = sum(1 for p in pls if p >= 0)
        out.append("  {:8s} n={:2d}  wins={:2d} ({:.0f}%)  total={}  avg={}".format(
            b, len(pls), w, 100.0 * w / len(pls),
            fmt(sum(pls), 0).strip(), fmt(sum(pls) / len(pls), 0).strip()))
    out.append("  (dte@entry from the matched open placement in attempts CSVs; 'unknown' =")
    out.append("   opened before the attempts lookback or closed without a matching open row)")
    out.append("")

    out.append("-- Opened this month ({}) --".format(len(opened)))
    for key, e, cost in opened:
        status = "CLOSED" if e["closed_after"] else "held"
        out.append("  {}  entry={}  [{}]".format(label(key), fmt(cost, 0).strip(), status))
    out.append("")

    out.append("-- Still held ({}) --".format(len(still_held)))
    for key, e, cost in still_held:
        out.append("  {}  entry={}  since={}".format(
            label(key), fmt(cost, 0).strip(), e["first"][:8]))
    out.append("")
    return opened, closed


def section_funnel(out, signals, attempts, ledger, month_prefix):
    out.append("=== C. EXECUTION FUNNEL & EFFICIENCY ===")
    out.append("")

    open_signals = [r for r in signals if (r.get("signal_type") or "") in
                    ("CALL_OPEN", "PUT_OPEN")]
    attempted_syms = set(r.get("symbol") for r in attempts
                         if r.get("action") in OPEN_ACTIONS and r.get("symbol"))

    placed_rows = [r for r in attempts
                   if r.get("action") in OPEN_ACTIONS
                   and r.get("status") in ("placed", "submitted")
                   and "success" in (r.get("reason") or "")]

    # fill check: placed spread appears in a later snapshot
    filled_keys, unfilled = set(), []
    seen_placed = set()
    for r in placed_rows:
        sym = r.get("symbol")
        right = "C" if "call" in r.get("action", "") else ("P" if "put" in r.get("action", "") else "?")
        lk, sk = money(r.get("longK")), money(r.get("shortK"))
        exp = (r.get("exp") or "").strip()
        pk = (sym, right, exp, lk, sk)
        if pk in seen_placed:
            continue
        seen_placed.add(pk)
        hit = None
        for key in ledger:
            if key[0] == sym and key[1] == right and (not exp or key[2] == exp):
                if lk is None or abs(key[3] - lk) < 0.01:
                    hit = key
                    break
        if hit:
            filled_keys.add(pk)
        else:
            unfilled.append((r["_day"], sym, right, lk, sk, money(r.get("limit"))))

    out.append("OPEN signals            : {}".format(len(open_signals)))
    out.append("symbols attempted       : {}".format(len(attempted_syms)))
    out.append("spreads placed (unique) : {}".format(len(seen_placed)))
    out.append("spreads filled          : {}".format(len(filled_keys)))
    out.append("placed but never filled : {}".format(len(unfilled)))
    if seen_placed:
        out.append("fill rate (placed)      : {:.0f}%".format(
            100.0 * len(filled_keys) / len(seen_placed)))
    out.append("")

    out.append("-- Skip/error reasons for OPEN attempts (by category) --")
    cats = defaultdict(Counter)
    for r in attempts:
        if r.get("action") in OPEN_ACTIONS and r.get("status") in ("skipped", "error"):
            pref = reason_prefix(r.get("reason"))
            cat = SKIP_CATEGORIES.get(pref, "other")
            cats[cat][pref] += 1
    for cat in ("liquidity_gate", "account_blocked", "not_priceable", "by_design", "other"):
        if cat not in cats:
            continue
        total = sum(cats[cat].values())
        out.append("  {} ({})".format(cat, total))
        for pref, n in cats[cat].most_common():
            out.append("    {:4d}  {}".format(n, pref))
    out.append("")

    if unfilled:
        out.append("-- Placed but expired unfilled --")
        for day, sym, right, lk, sk, lim in sorted(unfilled):
            out.append("  {}  {:6s} {} {}/{}  limit={}".format(
                day, sym, right,
                "{:g}".format(lk) if lk is not None else "?",
                "{:g}".format(sk) if sk is not None else "?",
                "{:.2f}".format(lim) if lim is not None else "?"))
        out.append("")

    # close-side efficiency
    close_attempts = Counter(r.get("symbol") for r in attempts
                             if r.get("action") in CLOSE_ACTIONS
                             and r.get("status") in ("placed", "submitted"))
    confirmed_closes = Counter(k[0] for k, e in ledger.items()
                               if e["closed_after"] is not None)
    grinders = {s: (n, confirmed_closes.get(s, 0))
                for s, n in close_attempts.items() if n >= 3}
    out.append("-- Close-side efficiency (attempts vs confirmed closes) --")
    out.append("  close orders submitted: {}   confirmed spread closes: {}".format(
        sum(close_attempts.values()), sum(confirmed_closes.values())))
    if grinders:
        out.append("  repeat offenders (>=3 close attempts):")
        for s, (n, c) in sorted(grinders.items(), key=lambda x: -x[1][0]):
            out.append("    {:6s} {} attempts -> {} confirmed close(s)".format(s, n, c))
    out.append("")
    return unfilled


def bucket_for_width(width):
    for name, w in (("1", 1.0), ("2_5", 2.5), ("5", 5.0), ("10", 10.0)):
        if abs(width - w) <= 0.6:
            return name
    return None


def connect_ib(out):
    """Connect read-only to IB (clientId 960). Returns ib or None (with a note)."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "InteractiveBrokersTrader"))
    try:
        from ib_insync import IB  # noqa
        from ib_config import IB_HOST, IB_PORT  # noqa
        ib = IB()
        ib.connect(IB_HOST, IB_PORT, clientId=960, timeout=15)
        return ib
    except Exception as e:
        out.append("NOTE: could not connect to IB ({}: {}) - live sections skipped.".format(
            type(e).__name__, e))
        out.append("")
        return None


def daily_close(ib, symbol, duration="2 D"):
    """Daily TRADES bars for a stock; returns {YYYYMMDD: close} (empty on failure)."""
    from ib_insync import Stock  # noqa
    try:
        c = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))[0]
        bars = ib.reqHistoricalData(c, "", duration, "1 day", "TRADES", True)
        return {b.date.strftime("%Y%m%d"): b.close for b in bars}
    except Exception:
        return {}


def section_opportunity(out, signals, attempts, unfilled, ledger, ib):
    out.append("=== D. OPPORTUNITY COST OF NON-EXECUTED OPENS (rough) ===")
    out.append("")
    if ib is None:
        out.append("SKIPPED: requires --live with IBGateway up (current underlying prices).")
        out.append("")
        return

    # Collect candidates: (symbol, side, day, S0, k_long, k_short, width, debit, why)
    cands = {}

    # (a) skipped signals with real skip reasons (not by_design)
    sig_by = {}
    for r in signals:
        st = r.get("signal_type") or ""
        if st in ("CALL_OPEN", "PUT_OPEN"):
            sig_by[(r.get("symbol"), r["_day"], "C" if st == "CALL_OPEN" else "P")] = r
    for r in attempts:
        if r.get("action") not in OPEN_ACTIONS or r.get("status") not in ("skipped", "error"):
            continue
        pref = reason_prefix(r.get("reason"))
        cat = SKIP_CATEGORIES.get(pref, "other")
        if cat == "by_design":
            continue
        sym = r.get("symbol")
        right = "C" if "call" in r.get("action", "") else ("P" if "put" in r.get("action", "") else None)
        if not sym or not right:
            continue
        sig = sig_by.get((sym, r["_day"], right))
        if not sig:
            continue
        s0 = money(sig.get("current_price"))
        atm = money(sig.get("atm_strike"))
        otm = money(sig.get("otm_strike_call" if right == "C" else "otm_strike_put"))
        if s0 is None or atm is None or otm is None:
            continue
        width = abs(otm - atm)
        b = bucket_for_width(width)
        debit = None
        if b:
            side = "call" if right == "C" else "put"
            debit = money(sig.get("{}_debit_theo_{}".format(side, b))) or \
                money(sig.get("{}_debit_limit_{}".format(side, b)))
        if debit is None or debit <= 0:
            continue
        k = (sym, right)
        if k not in cands:  # earliest signal wins (dedupe re-signals)
            cands[k] = (sym, right, r["_day"], atm, otm, width, debit, cat)

    # (b) placed but never filled - use the placed limit as the debit
    for day, sym, right, lk, sk, lim in unfilled:
        if lk is None or sk is None or lim is None or lim <= 0:
            continue
        k = (sym, right)
        if k not in cands:
            cands[k] = (sym, right, day, lk, sk, abs(sk - lk), lim, "unfilled_limit")

    if not cands:
        out.append("No candidates with sufficient data.")
        out.append("")
        return

    # live prices via the shared connection
    prices = {}
    for sym in sorted(set(k[0] for k in cands)):
        px = daily_close(ib, sym)
        if px:
            prices[sym] = px[max(px)]

    totals = defaultdict(float)
    counts = defaultdict(int)
    out.append("sym    R  day       S_now    strikes      debit  est_payoff  est_P/L  why")
    for (sym, right), (s, r_, day, kl, ks, width, debit, why) in sorted(cands.items()):
        snow = prices.get(sym)
        if snow is None:
            out.append("{:6s} {}  {}  no-price".format(sym, right, day))
            continue
        if right == "C":
            payoff = min(max(snow - kl, 0.0), width)
        else:
            payoff = min(max(kl - snow, 0.0), width)
        pl = (payoff - debit) * 100.0
        totals[why] += pl
        counts[why] += 1
        out.append("{:6s} {}  {}  {:7.2f}  {:>5g}/{:<5g}  {:5.2f}  {:9.2f}  {}  {}".format(
            sym, right, day, snow, kl, ks, debit, payoff, fmt(pl, 8), why))
    out.append("")
    out.append("-- What the misses would have been worth (intrinsic-only estimate) --")
    for why in sorted(totals):
        out.append("  {:16s} {:3d} trades  est P/L {}".format(
            why, counts[why], fmt(totals[why], 0).strip()))
    out.append("")
    out.append("NOTE: intrinsic value at today's price, held-to-now; ignores early exits,")
    out.append("theta, and whether closes would have fired. Directional sanity check only.")
    out.append("")


def section_alpha_beta(out, daily, baseline_pl, ib):
    out.append("=== E. ALPHA / BETA vs SPY ===")
    out.append("")
    if ib is None:
        out.append("SKIPPED: requires --live with IBGateway up (SPY price history).")
        out.append("")
        return
    import json
    try:
        with open(os.path.join(OH, "ytd_baseline.json")) as f:
            base = float(json.load(f)["netliq"])
    except Exception as e:
        out.append("SKIPPED: could not read ytd_baseline.json ({}).".format(e))
        out.append("")
        return

    series = []
    if baseline_pl:
        series.append((baseline_pl[0], base + baseline_pl[1]))
    for day, _r, _u, _d, ytd in daily:
        if ytd is not None:
            series.append((day, base + ytd))

    spy = daily_close(ib, "SPY", "90 D")
    pts = [(d, nl) for d, nl in series if d in spy and nl]
    if len(pts) < 6:
        out.append("SKIPPED: not enough overlapping account/SPY days ({}).".format(len(pts)))
        out.append("")
        return

    ra, rm = [], []
    for (d1, n1), (d2, n2) in zip(pts, pts[1:]):
        ra.append(n2 / n1 - 1.0)
        rm.append(spy[d2] / spy[d1] - 1.0)
    n = len(ra)
    mean_a, mean_m = sum(ra) / n, sum(rm) / n
    cov = sum((a - mean_a) * (m - mean_m) for a, m in zip(ra, rm)) / n
    var_m = sum((m - mean_m) ** 2 for m in rm) / n
    sd_a = (sum((a - mean_a) ** 2 for a in ra) / n) ** 0.5
    sd_m = var_m ** 0.5
    beta = cov / var_m if var_m > 0 else None
    corr = cov / (sd_a * sd_m) if sd_a > 0 and sd_m > 0 else None
    r_acct = pts[-1][1] / pts[0][1] - 1.0
    r_spy = spy[pts[-1][0]] / spy[pts[0][0]] - 1.0

    out.append("period                 : {} -> {} ({} daily returns)".format(
        pts[0][0], pts[-1][0], n))
    out.append("account return         : {:+.2f}%".format(100 * r_acct))
    out.append("SPY return             : {:+.2f}%".format(100 * r_spy))
    if beta is not None:
        alpha = r_acct - beta * r_spy
        out.append("beta vs SPY            : {:+.2f}".format(beta))
        out.append("correlation            : {:+.2f}".format(corr if corr is not None else 0))
        out.append("alpha (period, CAPM)   : {:+.2f}%  (= acct return - beta * SPY return)".format(
            100 * alpha))
    out.append("daily vol (acct / SPY) : {:.2f}% / {:.2f}%".format(100 * sd_a, 100 * sd_m))
    out.append("")
    out.append("NOTE: ~20 daily points -> beta/alpha are noisy estimates; treat direction,")
    out.append("not decimals, as the signal. NetLiq from health reports; rf assumed 0.")
    out.append("")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Monthly performance & efficiency review (read-only)")
    ap.add_argument("--month", default=None,
                    help="Month as YY_MM (e.g. 26_08). Default: current month.")
    ap.add_argument("--live", action="store_true",
                    help="Enable IB queries for the opportunity-cost section.")
    ap.add_argument("--out", action="store_true",
                    help="Also write report to C:\\OptionsHistory\\logs\\monthly_review_<YY_MM>.txt")
    args = ap.parse_args()

    month = args.month or datetime.now().strftime("%y_%m")
    if not re.match(r"^\d\d_\d\d$", month):
        print("Bad --month (expect YY_MM, e.g. 26_08)")
        sys.exit(1)
    month_yyyymm = "20" + month.replace("_", "")
    month_label = datetime.strptime(month_yyyymm, "%Y%m").strftime("%B %Y")

    def prev_month(m):
        y, mm = int(m[:2]), int(m[3:])
        mm -= 1
        if mm == 0:
            y, mm = y - 1, 12
        return "{:02d}_{:02d}".format(y, mm)

    daily, snapshots, baseline_pl = load_health(month_yyyymm)
    attempts = load_attempts(month)
    signals = load_signals(month)
    # entry-DTE lookups need opens placed before the review month
    attempts_hist = (attempts + load_attempts(prev_month(month))
                     + load_attempts(prev_month(prev_month(month))))

    out = []
    out.append("==== MONTHLY REVIEW {} (generated {}) ====".format(
        month_label, datetime.now().strftime("%Y-%m-%d %H:%M")))
    out.append("")

    if not daily:
        out.append("No health reports with P/L found for {}.".format(month_label))
    else:
        section_performance(out, daily, baseline_pl, month_label)

    ledger = build_ledger(snapshots)
    month_start_ts = month_yyyymm + "01_000000"
    if snapshots:
        section_ledger(out, ledger, snapshots, attempts, month_start_ts, attempts_hist)
    else:
        out.append("No usable position snapshots found - skipping trade ledger.")
        out.append("")

    unfilled = section_funnel(out, signals, attempts, ledger, month)

    ib = connect_ib(out) if args.live else None
    try:
        section_opportunity(out, signals, attempts, unfilled, ledger, ib)
        section_alpha_beta(out, daily, baseline_pl, ib)
    finally:
        if ib is not None:
            ib.disconnect()

    report = "\n".join(out)
    print(report)
    if args.out:
        p = os.path.join(LOGS, "monthly_review_{}.txt".format(month))
        with open(p, "w", encoding="ascii", errors="replace") as f:
            f.write(report + "\n")
        print("\nReport written to {}".format(p))


if __name__ == "__main__":
    main()
