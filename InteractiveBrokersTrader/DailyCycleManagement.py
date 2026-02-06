import subprocess, sys
from pathlib import Path
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
import logging
import csv, os, uuid
from ib_close_guard import has_working_auto_close

LOG = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

# Default US equity market hours (RTH). Consider replacing with an exchange calendar lib.
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# Optional “pre-close” sweep to convert stubborn CLOSE limits to market
PRE_CLOSE_SWEEP = time(15, 0)       # 3:00 pm ET
PRE_CLOSE_SWEEP_END = time(15, 30)  # safety window

#
# Note: After-hours placement deliberately ignores OI checks (RTH-only cleanup handles low-liquidity orders).
AFTER_HOURS_PLACEMENT = time(17, 0) # 5:00 pm ET

# Idempotency windows to prevent double-running cycles
DAILY_ANALYSIS_COOLDOWN_HOURS = 2      # don't re-run daily analysis inside this window
WEEKLY_MAINTENANCE_DAY = 6             # Sunday

# Minimum OI required for at least one leg to keep an order during RTH
MIN_OI_FOR_RTH = 100

def _attempts_csv_path() -> str:
    """
    Generate the attempts CSV path under the current date's OptionsHistory folder.
    Example: C:\\OptionsHistory\\25_10_18\\attempts_25_10_18_151230.csv
    """
    from datetime import datetime
    ny_now = datetime.now(NY)
    folder = ny_now.strftime("%y_%m_%d")
    # Use dated folder instead of the logs directory
    root = fr"C:\OptionsHistory\{folder}" if sys.platform.startswith("win") else f"./{folder}"
    os.makedirs(root, exist_ok=True)
    LOG.info("Attempt CSV path resolved to: %s", root)
    stamp = ny_now.strftime("%y_%m_%d_%H%M%S")
    return os.path.join(root, f"attempts_{stamp}.csv")

class _AttemptLogger:
    _active_path: str | None = None

    @classmethod
    def path(cls) -> str:
        if not cls._active_path:
            cls._active_path = _attempts_csv_path()
        return cls._active_path

    @classmethod
    def write(cls, **kw):
        path = cls.path()
        row = {
            "ts":     kw.get("ts") or datetime.now(NY).isoformat(),
            "symbol": kw.get("symbol", ""),
            "action": kw.get("action", ""),     # e.g., close / hold / noop
            "status": kw.get("status", ""),     # submitted / skipped / placed
            "reason": kw.get("reason", ""),     # reconcile_mismatch / reconcile_close_signal / ...
            "exp":    kw.get("exp", ""),
            "right":  kw.get("right", ""),
            "atm":    kw.get("atm", ""),
            "oth":    kw.get("oth", ""),
            "limit":  kw.get("limit", ""),
            "source": kw.get("source", "dcm"),
            "uid":    kw.get("uid", str(uuid.uuid4())[:8]),
        }
        hdr = list(row.keys())
        # Write to primary log file (logs folder or wherever _active_path points)
        exists = os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=hdr)
            if not exists:
                w.writeheader()
            w.writerow(row)

        # Additionally, write to today's dated folder under C:\OptionsHistory\<yy_mm_dd>\attempts_<yy_mm_dd>.csv
        try:
            from datetime import datetime
            import sys
            # Get today's New York date
            ny_now = datetime.now(NY)
            folder = ny_now.strftime("%y_%m_%d")
            root = fr"C:\OptionsHistory\{folder}" if sys.platform.startswith("win") else f"./{folder}"
            os.makedirs(root, exist_ok=True)
            dated_csv = os.path.join(root, f"attempts_{folder}.csv")
            exists_dated = os.path.exists(dated_csv)
            with open(dated_csv, "a", newline="", encoding="utf-8") as fh2:
                w2 = csv.DictWriter(fh2, fieldnames=hdr)
                if not exists_dated:
                    w2.writeheader()
                w2.writerow(row)
        except Exception as e:
            LOG.warning("Failed to write to daily attempts CSV: %s", e)

def _ny_csv_path() -> str:
    """
    Build today's combined listener CSV path using New York date.
    Example: C:\\OptionsHistory\\yy_MM_dd\\combined_listener_spreads.csv
    """
    ny_now = datetime.now(NY)
    folder = ny_now.strftime("%y_%m_%d")
    return fr"C:\OptionsHistory\{folder}\combined_listener_spreads.csv"

# ----- External runner for PlaceAnOrder.py (used as a fallback/orchestration hook) -----
# Resolve script path relative to this file
PLACE_AN_ORDER_PATH = Path(__file__).with_name("PlaceAnOrder.py")
# Liquidity filter/enricher (used to populate OI into today's combined CSV)
LIQUIDITY_FILTER_PATH = Path(__file__).with_name("LiquidityFilter.py")
# Try to use repo-local venv on Windows; otherwise fall back to current interpreter
VENV_PY_WIN = Path(__file__).parents[1] / ".venv" / "Scripts" / "python.exe"

class DailyCycleManagementMixin:
    def _working_close_limit_symbols(self) -> set[str]:
        try:
            from ib_insync import IB
        except Exception:
            return set()
        ib = IB()
        out: set[str] = set()
        try:
            ib.connect('127.0.0.1', 7497, clientId=887, timeout=6)
            for tr in ib.openTrades() or []:
                c = getattr(tr, 'contract', None)
                o = getattr(tr, 'order', None)
                s = getattr(tr, 'orderStatus', None)
                if not (c and o and s):
                    continue
                if getattr(c, 'secType', '') != 'BAG':
                    continue
                if (getattr(o, 'action', '') or '').upper() not in ('SELL','BUY'):
                    continue
                if (getattr(o, 'orderType', '') or '').upper() != 'LMT':
                    continue
                st = (getattr(s, 'status', '') or '').lower()
                if st in ('filled','cancelled','apicancelled'):
                    continue
                sym = (getattr(c, 'symbol', '') or '').upper()
                if sym:
                    out.add(sym)
        except Exception:
            pass
        finally:
            try: ib.disconnect()
            except Exception: pass
        return out

    def _collect_held_orientations(self) -> dict[str, int | None]:
        """
        Return dict[symbol] -> sign (+1 call debit, -1 put debit, None unknown) based on current positions.
        """
        try:
            from ib_insync import IB
        except Exception:
            return {}
        ib = IB()
        try:
            ib.connect('127.0.0.1', 7497, clientId=890, timeout=6)
        except Exception:
            return {}
        signs: dict[str, int | None] = {}
        try:
            poss = ib.positions() or []
            legs_by_sym: dict[str, list[tuple[str, float, float]]] = {}
            for p in poss:
                c = getattr(p, 'contract', None)
                if getattr(c, 'secType', '') != 'OPT':
                    continue
                sym = (getattr(c, 'symbol', '') or '').upper()
                if not sym:
                    continue
                r = (getattr(c, 'right', '') or '').upper()
                k = float(getattr(c, 'strike', 0.0))
                q = float(getattr(p, 'position', 0.0) or 0.0)
                legs_by_sym.setdefault(sym, []).append((r, k, q))
            for s, legs in legs_by_sym.items():
                sign: int | None = None
                calls = sorted([(k, q) for r, k, q in legs if r == 'C'])
                puts  = sorted([(k, q) for r, k, q in legs if r == 'P'])
                call_debit = any(k1 < k2 and q1 > 0 and q2 < 0 for (k1, q1) in calls for (k2, q2) in calls if k1 < k2)
                put_debit  = any(k1 > k2 and q1 > 0 and q2 < 0 for (k1, q1) in puts  for (k2, q2) in puts  if k1 > k2)
                if call_debit and not put_debit:
                    sign = +1
                elif put_debit and not call_debit:
                    sign = -1
                elif call_debit and put_debit:
                    # choose dominant by leg count if ambiguous
                    sign = +1 if len(calls) >= len(puts) else -1
                signs[s] = sign
        except Exception:
            pass
        finally:
            try: ib.disconnect()
            except Exception: pass
        return signs

    def _detect_credit_or_inverted_spreads(self) -> set[str]:
        """
        Scan IB positions for symbols that currently have *credit or inverted* verticals
        (short call vertical, short put vertical, or long/short legs oriented opposite to
        the intended debit shape).
        Returns a set of tickers (uppercased) that should be force-closed via DCM.
        """
        try:
            from ib_insync import IB
        except Exception as e:
            LOG.warning("credit-scan: ib_insync unavailable: %s", e)
            return set()

        ib = IB()
        try:
            ib.connect('127.0.0.1', 7497, clientId=892, timeout=6)
        except Exception as e:
            LOG.warning("credit-scan: connect failed: %s", e)
            return set()

        bad_syms: set[str] = set()
        try:
            poss = ib.positions() or []

            # Group positions by (symbol, expiry, right)
            by_key: dict[tuple[str, str, str], dict[float, float]] = {}
            for p in poss:
                c = getattr(p, "contract", None)
                if getattr(c, "secType", "") != "OPT":
                    continue
                sym = (getattr(c, "symbol", "") or "").upper()
                exp = getattr(c, "lastTradeDateOrContractMonth", "")
                right = (getattr(c, "right", "") or "").upper()  # 'C' or 'P'
                qty = float(getattr(p, "position", 0.0) or 0.0)
                if abs(qty) < 1e-9:
                    continue
                strike = float(getattr(c, "strike", 0.0))
                by_key.setdefault((sym, exp, right), {})[strike] = qty

            for (sym, exp, right), legs in by_key.items():
                # long_strikes: strikes with +qty, short_strikes: strikes with -qty
                long_strikes = [(k, v) for k, v in legs.items() if v > 0]
                short_strikes = [(k, v) for k, v in legs.items() if v < 0]
                if not long_strikes or not short_strikes:
                    continue

                is_bad = False
                for L, qL in long_strikes:
                    for S, qS in short_strikes:
                        # We only look at L>0, S<0 pairs by construction
                        if right == "C":
                            # Proper CALL debit: long lower, short higher  -> L < S
                            # If not (L < S), then shape is credit/inverted
                            if not (L < S):
                                is_bad = True
                                break
                        elif right == "P":
                            # Proper PUT debit: long higher, short lower -> L > S
                            if not (L > S):
                                is_bad = True
                                break
                    if is_bad:
                        break

                if is_bad:
                    bad_syms.add(sym)

        except Exception as e:
            LOG.warning("credit-scan: error while scanning positions: %s", e)
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

        if bad_syms:
            LOG.info("credit-scan: detected credit/inverted verticals in: %s",
                     ", ".join(sorted(bad_syms)))
        else:
            LOG.info("credit-scan: no credit/inverted verticals detected.")

        return bad_syms

    def _latest_signal_is_close(self, sym: str, days: int = 21) -> bool:
        sym = (sym or '').upper()
        now = self._now_ny()
        def _csv_path_for(dt):
            folder = dt.astimezone(NY).strftime('%y_%m_%d')
            return fr"C:\OptionsHistory\{folder}\combined_listener_spreads.csv"
        for d in range(0, max(1, days)):
            fp = _csv_path_for(now - timedelta(days=d))
            if not os.path.exists(fp):
                continue
            try:
                last_row = None
                with open(fp, newline='', encoding='utf-8') as fh:
                    rdr = csv.DictReader(fh)
                    for row in rdr:
                        if (row.get('symbol','') or '').strip().upper() == sym:
                            last_row = row
                if last_row is None:
                    continue
                side_raw = (last_row.get('signal_type') or last_row.get('signal_side') or '').strip().lower()
                return 'close' in side_raw
            except Exception:
                continue
        return False

    def _latest_open_sign(self, sym: str, days: int = 21) -> int | None:
        """
        Return +1 if latest signal is CALL_OPEN, -1 if PUT_OPEN, or strategy_position in {1,-1}.
        None if not found/ambiguous within window.
        """
        sym = (sym or '').upper()
        now = self._now_ny()
        def _csv_path_for(dt):
            folder = dt.astimezone(NY).strftime('%y_%m_%d')
            return fr"C:\OptionsHistory\{folder}\combined_listener_spreads.csv"
        for d in range(0, max(1, days)):
            fp = _csv_path_for(now - timedelta(days=d))
            if not os.path.exists(fp):
                continue
            try:
                last_row = None
                with open(fp, newline='', encoding='utf-8') as fh:
                    rdr = csv.DictReader(fh)
                    for row in rdr:
                        if (row.get('symbol','') or '').strip().upper() == sym:
                            last_row = row
                if last_row is None:
                    continue
                side_raw = (last_row.get('signal_type') or last_row.get('signal_side') or '').strip().lower()
                if 'open' in side_raw:
                    r = (last_row.get('right') or last_row.get('signal_right') or '').strip().upper()
                    if r == 'C': return +1
                    if r == 'P': return -1
                    if 'call' in side_raw: return +1
                    if 'put' in side_raw: return -1
                sp = (last_row.get('strategy_position') or '').strip()
                try:
                    sp_i = int(sp)
                    if sp_i in (1, -1):
                        return sp_i
                except Exception:
                    pass
                if 'close' in side_raw:
                    return None
            except Exception:
                continue
        return None

    def _cancel_symbol_close_orders(self, symbol: str) -> int:
        """
        Cancel all pending/working SELL combo (BAG) orders for the given ticker.
        Returns number of orders cancelled.
        """
        try:
            from ib_insync import IB
        except Exception:
            return 0

        ib = IB()
        try:
            ib.connect('127.0.0.1', 7497, clientId=886, timeout=6)
        except Exception:
            return 0

        try:
            # Refresh local view of open orders/trades
            try:
                ib.reqOpenOrders()
                ib.sleep(0.25)
            except Exception:
                pass

            n_cancel = 0
            sym_u = (symbol or "").upper()

            for tr in ib.trades() or []:
                try:
                    c = getattr(tr, "contract", None)
                    o = getattr(tr, "order", None)
                    s = getattr(tr, "orderStatus", None)
                    if not (c and o and s):
                        continue

                    if getattr(c, "secType", "") != "BAG":
                        continue
                    if (getattr(c, "symbol", "") or "").upper() != sym_u:
                        continue

                    act = (getattr(o, "action", "") or "").upper()
                    status = (getattr(s, "status", "") or "")

                    # Common working/pre-working states
                    if act == "SELL" and status in ("PreSubmitted", "Submitted", "PendingSubmit", "ApiPending", "ApiCancelled", "Inactive"):
                        try:
                            ib.cancelOrder(o)
                            n_cancel += 1
                            LOG.info(
                                "[%s] Cancelled pending CLOSE order (id=%s, status=%s)",
                                sym_u,
                                getattr(o, "orderId", None),
                                status,
                            )
                            try:
                                self._attempt(
                                    symbol=sym_u,
                                    action="cancel_close",
                                    status="placed",
                                    reason="cancelled",
                                    exp=getattr(c, "comboLegsDescrip", ""),
                                    right="?",
                                    source="dcm-preclose",
                                    order_id=getattr(o, "orderId", None),
                                    prev_status=status,
                                )
                            except Exception:
                                pass
                        except Exception as e:
                            LOG.warning(
                                "[%s] Failed to cancel order %s: %s",
                                sym_u,
                                getattr(o, "orderId", None),
                                e,
                            )
                except Exception:
                    continue

            return n_cancel
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    def _submit_close_shared(self, sym: str, csv_exists_today: bool, lookback_days: int, context: str) -> None:
        """
        Shared submission path used by both pre-close (≈15:00) and after-hours reconcile.
        Delegates to PlaceAnOrder first; if no working close order exists afterwards, it records that fact.
        DCM itself does not place option orders directly.
        """
        _prev_phase = getattr(self, "_in_close_phase", False)
        self._in_close_phase = True
        try:
            has_working = self._has_working_close_order(sym)

            # NEW: In preclose, we *replace* stale close limits instead of skipping them
            if context == "preclose" and has_working:
                n_cxl = self._cancel_symbol_close_orders(sym)
                if n_cxl > 0:
                    self._attempt(
                        symbol=sym,
                        action="close",
                        status="placed",
                        reason="preclose_cancel_existing_close",
                        source="dcm-preclose",
                        extra={"cancelled": n_cxl},
                    )
                # we just cancelled them; treat as if no working close exists
                has_working = False

            # For non-preclose flows, keep the old guard: skip if a working close already exists
            if context != "preclose" and has_working:
                self._attempt(
                    symbol=sym,
                    action="close",
                    status="skipped",
                    reason=f"{context}_existing_working_close",
                    source=f"dcm-{context}",
                )
                return

            # Stage 1: delegate using CSV-derived limits (from-signal mode respects CSV limits)
            dated_folder = self._now_ny().strftime("%y_%m_%d")
            LOG.info(f"[{sym}] Stage 1: Attempting CSV-based close from {dated_folder} (context={context})")
            try:
                self._attempt(
                    symbol=sym,
                    action="close",
                    status="queued",
                    reason=f"{context}_delegated_csv_limit",
                    source=f"dcm-{context}",
                )
            except Exception:
                pass
            self._run_place_an_order([
                "--mode","from-signal",  # Changed: use from-signal to respect CSV limits
                "--date", dated_folder,  # Ensure correct dated CSV directory
                "--symbols", sym,
                "--min-limit","0.01" if context == "preclose" else "0.05",
                "--use-live-close","off",  # Don't override CSV limits with live quotes
                "--quantity","50",
                "--quiet"
            ])

            has_working = self._has_working_close_order(sym)
            LOG.info(f"[{sym}] Stage 1 completed: has_working_close_order={has_working}")
            if has_working:
                try:
                    self._attempt(
                        symbol=sym,
                        action="close",
                        status="submitted",
                        reason=f"{context}_delegated_csv_limit_working",
                        source=f"dcm-{context}",
                    )
                except Exception:
                    pass
            else:
                # Stage 2: fallback to force-close with age-based pricing (mid for same-day, join for previous-day)
                scheme = self._determine_live_close_scheme_for_symbol(sym)
                LOG.warning(f"[{sym}] Stage 1 failed to create working order - falling back to Stage 2 (force-close with '{scheme}' scheme)")
                try:
                    self._attempt(
                        symbol=sym,
                        action="close",
                        status="queued",
                        reason=f"{context}_fallback_live_{scheme}",
                        source=f"dcm-{context}",
                    )
                except Exception:
                    pass
                self._run_place_an_order([
                    "--mode","force-close",  # Force-close scans positions directly
                    "--date", dated_folder,  # Ensure correct dated CSV directory
                    "--symbols", sym,
                    "--min-limit","0.01" if context == "preclose" else "0.05",
                    "--use-live-close", scheme,  # Dynamic: 'mid' for same-day, 'join' for previous-day
                    "--quantity","50",
                    "--quiet"
                ])
                if self._has_working_close_order(sym):
                    try:
                        self._attempt(
                            symbol=sym,
                            action="close",
                            status="submitted",
                            reason=f"{context}_delegated_live_mid_working",
                            source=f"dcm-{context}",
                        )
                    except Exception:
                        pass
                else:
                    # No working close even after Stage 2; record but do not place direct orders from DCM.
                    try:
                        self._attempt(
                            symbol=sym,
                            action="close",
                            status="submitted",
                            reason=f"{context}_delegated_no_working_close",
                            source=f"dcm-{context}",
                        )
                    except Exception:
                        pass

            # If the latest signal is CLOSE, also flatten any stock leg to avoid lingering STK exposure
            try:
                if self._latest_signal_is_close(sym, lookback_days):
                    if self._flatten_stock_if_present(sym):
                        self._attempt(
                            symbol=sym,
                            action="close_stock",
                            status="submitted",
                            reason=f"{context}_latest_close_flatten",
                            source=f"dcm-{context}",
                        )
            except Exception:
                # best-effort; do not fail close submission if STK flatten throws
                pass
        finally:
            self._in_close_phase = _prev_phase

    def _csv_paths_by_priority(days: int = 2) -> list[str]:
        """
        Return a list of combined_listener_spreads.csv paths ordered newest->older
        for the last `days` sessions (today = 0). Missing files are skipped.
        """
        paths: list[str] = []
        now = datetime.now(NY)
        for d in range(0, max(1, days)):
            folder = (now - timedelta(days=d)).strftime("%y_%m_%d")
            fp = fr"C:\OptionsHistory\{folder}\combined_listener_spreads.csv"
            if os.path.exists(fp):
                paths.append(fp)
        return paths

    def _load_csv_rows_with_source(days: int = 2) -> list[dict]:
        """
        Load rows from today's and prior-day combined_listener_spreads.csv into a single list.
        Earlier (newer) files are placed first so matching prefers today's data; each row gets
        a '_csv_src' field set to 'today', 'yesterday', etc.
        """
        rows: list[dict] = []
        paths = DailyCycleManagementMixin._csv_paths_by_priority(days=days)
        labels = ["today", "yesterday", "d-2", "d-3", "d-4"]
        for idx, path in enumerate(paths):
            label = labels[idx] if idx < len(labels) else f"d-{idx}"
            try:
                with open(path, newline="", encoding="utf-8") as fh:
                    rdr = csv.DictReader(fh)
                    for r in rdr:
                        r2 = dict(r)
                        r2["_csv_src"] = label
                        rows.append(r2)
            except Exception as e:
                LOG.warning("CSV OI cancel: failed reading %s: %s", path, e)
                continue
        return rows

    # ---- Helpers: expiration parsing and delegated open/close orchestration ----
    def _parse_exp_to_date(exp_str: str):
        """
        Parse option expiration strings in formats YYYYMMDD, YYYY-MM-DD, or YYMMDD to a date.
        Returns a date or None on failure.
        """
        if not exp_str:
            return None
        s = str(exp_str).strip().replace("-", "")
        try:
            if len(s) == 8:
                # YYYYMMDD
                y, m, d = int(s[0:4]), int(s[4:6]), int(s[6:8])
                return date(y, m, d)
            if len(s) == 6:
                # YYMMDD -> assume 20xx
                y, m, d = int("20"+s[0:2]), int(s[2:4]), int(s[4:6])
                return date(y, m, d)
        except Exception:
            return None
        return None

    def _delegate_open_from_recent_csvs(self, min_dte: int = 20, last_n_csvs: int = 2) -> None:
        """
        Delegate OPEN placements to PlaceAnOrder.py using only the most recent CSVs (default: today & yesterday).
        Filters to rows with OPEN signals and expiration DTE >= min_dte. DCM does not talk to broker here.
        """
        # Suppress OPEN delegation if a CLOSE phase is active
        if getattr(self, "_in_close_phase", False):
            LOG.info("Open-delegate suppressed (close phase active).")
            return
        # Do not delegate new OPENs for symbols where we already hold options exposure
        held_signs = self._collect_held_orientations()
        held_syms = set(held_signs.keys())

        # Track symbols for which a CLOSE was already submitted in this run
        submitted_close_syms = getattr(self, "_submitted_close_syms", set())

        now_d = self._now_ny().date()
        paths = DailyCycleManagementMixin._csv_paths_by_priority(days=max(1, last_n_csvs))
        if not paths:
            LOG.info("Open-delegate: no recent CSVs found; skipping.")
            return
        # Map of YY_MM_DD folder string -> set(symbols)
        to_submit: dict[str, set] = {}
        for path in paths:
            try:
                folder = Path(path).parent.name  # YY_MM_DD
                with open(path, newline="", encoding="utf-8") as fh:
                    rdr = csv.DictReader(fh)
                    for r in rdr:
                        sym = (r.get("symbol") or "").strip().upper()
                        if not sym:
                            continue
                        side_raw = (r.get("signal_type") or r.get("signal_side") or "").strip().lower()
                        # Determine signal side: +1 for CALL_OPEN, -1 for PUT_OPEN
                        signal_is_call = "call" in side_raw
                        signal_is_put = "put" in side_raw
                        signal_sign = +1 if signal_is_call else (-1 if signal_is_put else 0)

                        # Check if this is a flip scenario: we hold opposite side and close was submitted
                        held_sign = held_signs.get(sym)  # +1 for call, -1 for put, None if no position
                        is_flip = (sym in submitted_close_syms and
                                   held_sign is not None and
                                   signal_sign != 0 and
                                   held_sign != signal_sign)

                        # If we already hold an options position in this symbol, skip unless it's a flip
                        if sym in held_syms and not is_flip:
                            # Check if it's same-side (would be a duplicate) vs opposite-side (flip)
                            if held_sign == signal_sign:
                                try:
                                    self._attempt(
                                        symbol=sym,
                                        action="open",
                                        status="skipped",
                                        reason="skip_open_same_side_position",
                                        source="dcm-open",
                                    )
                                except Exception:
                                    pass
                                continue
                            elif sym not in submitted_close_syms:
                                # Opposite side but no close submitted yet - skip for now
                                try:
                                    self._attempt(
                                        symbol=sym,
                                        action="open",
                                        status="skipped",
                                        reason="skip_open_opposite_no_close_yet",
                                        source="dcm-open",
                                    )
                                except Exception:
                                    pass
                                continue

                        # If a close was submitted but it's NOT a flip, skip the open
                        if sym in submitted_close_syms and not is_flip:
                            try:
                                self._attempt(
                                    symbol=sym,
                                    action="open",
                                    status="skipped",
                                    reason="skip_open_reconcile_close_submitted",
                                    source="dcm-open",
                                )
                            except Exception:
                                pass
                            continue
                        if "open" not in side_raw:
                            continue
                        exp_s = (r.get("expiration") or r.get("exp") or r.get("lastTradeDateOrContractMonth") or "").strip()
                        e_d = DailyCycleManagementMixin._parse_exp_to_date(exp_s)
                        if not e_d:
                            continue
                        dte = (e_d - now_d).days
                        if dte < min_dte:
                            try:
                                self._attempt(symbol=sym, action="open", status="skipped", reason=f"dte_lt_{min_dte}", exp=exp_s, source="dcm-open")
                            except Exception:
                                pass
                            continue
                        to_submit.setdefault(folder, set()).add(sym)
            except Exception as e:
                LOG.warning("Open-delegate: failed reading %s: %s", path, e)
                continue
        for folder, syms in to_submit.items():
            if not syms:
                continue
            argv = ["--mode","from-signal","--date",folder,"--symbols", ",".join(sorted(syms)),
                    "--min-limit","0.05","--bump-to-min","--use-live-open","join","--quiet"]
            try:
                self._attempt(action="open", status="queued", reason=f"from_csv_{folder}", source="dcm-open")
            except Exception:
                pass
            self._run_place_an_order(argv)
            try:
                self._attempt(action="open", status="submitted", reason=f"from_csv_{folder}", source="dcm-open")
            except Exception:
                pass

    def _diagnose_close_symbols_in_csvs(self, days: int = 7) -> None:
        """
        Log which symbols would be considered CLOSE within the last `days` sessions,
        grouped by CSV day (newest→older). This does not place orders.
        """
        paths = DailyCycleManagementMixin._csv_paths_by_priority(days=max(1, days))
        if not paths:
            LOG.info("Diagnose-close: no CSVs within %d day(s).", days)
            return

        latest_by_sym: dict[str, tuple[dict, str, str]] = {}
        for path in paths:
            label = Path(path).parent.name
            try:
                with open(path, newline="", encoding="utf-8") as fh:
                    rdr = csv.DictReader(fh)
                    for r in rdr:
                        sym = (r.get("symbol") or "").strip().upper()
                        if not sym:
                            continue
                        if sym not in latest_by_sym:
                            latest_by_sym[sym] = (r, label, path)
            except Exception as e:
                LOG.warning("Diagnose-close: failed reading %s: %s", path, e)

        def _is_close(row: dict) -> bool:
            side_raw = (row.get("signal_type") or row.get("signal_side") or "").strip().lower()
            if "close" in side_raw:
                return True
            sp = (row.get("strategy_position") or "").strip()
            try:
                if sp and int(sp) == 0:
                    return True
            except Exception:
                pass
            side_col = (row.get("side") or row.get("trade_side") or "").strip().lower()
            return "close" in side_col

        buckets: dict[str, list[str]] = {}
        for sym, (row, label, _) in latest_by_sym.items():
            if _is_close(row):
                buckets.setdefault(label, []).append(sym)

        if not buckets:
            LOG.info("Diagnose-close: none found in last %d day(s).", days)
            return

        for label in sorted(buckets.keys(), reverse=True):
            LOG.info("Diagnose-close: %s → %d symbols: %s",
                     label, len(buckets[label]), ", ".join(sorted(buckets[label])))

    def _delegate_close_from_csvs_within(self, days: int) -> None:
        """
        Delegate CLOSE placements to PlaceAnOrder.py for symbols whose *latest* row
        within `days` (today + previous sessions) indicates a CLOSE.
        This scans newest→older CSVs once and keeps the first (newest) row per symbol
        across the whole window to avoid double-submitting per day.
        Implementation: only delegates to PlaceAnOrder.py (no direct IB order placement).
        """
        _prev_phase = getattr(self, "_in_close_phase", False)
        self._in_close_phase = True
        try:
            paths = DailyCycleManagementMixin._csv_paths_by_priority(days=max(1, days))
            if not paths:
                LOG.info("Close-delegate: no CSVs within %d day(s); skipping.", days)
                return

            # Newest→older map: sym -> (row, folder_label, file_path)
            latest_by_sym: dict[str, tuple[dict, str, str]] = {}
            for path in paths:
                label = Path(path).parent.name  # YY_MM_DD
                try:
                    with open(path, newline="", encoding="utf-8") as fh:
                        rdr = csv.DictReader(fh)
                        for r in rdr:
                            sym = (r.get("symbol") or "").strip().upper()
                            if not sym:
                                continue
                            # Keep the first time we see a symbol while iterating newest→older => newest row wins
                            if sym not in latest_by_sym:
                                latest_by_sym[sym] = (r, label, path)
                except Exception as e:
                    LOG.warning("Close-delegate: failed reading %s: %s", path, e)
                    continue

            def _is_close(row: dict) -> bool:
                side_raw = (row.get("signal_type") or row.get("signal_side") or "").strip().lower()
                if "close" in side_raw:
                    return True
                # Fallbacks: treat strategy_position == 0 as a close-like signal if present
                sp = (row.get("strategy_position") or "").strip()
                try:
                    if sp and int(sp) == 0:
                        return True
                except Exception:
                    pass
                # Some listener variants encode as 'sell,CLOSE' in a generic 'side' column
                side_col = (row.get("side") or row.get("trade_side") or "").strip().lower()
                return "close" in side_col

            # Pick only those symbols whose newest row within the window is CLOSE
            pick: list[str] = []
            per_day_counts: dict[str, int] = {}
            for sym, (row, label, _) in latest_by_sym.items():
                if _is_close(row):
                    pick.append(sym)
                    per_day_counts[label] = per_day_counts.get(label, 0) + 1

            if not pick:
                LOG.info("Close-delegate: no CLOSE symbols found in the last %d day(s).", days)
                return

            # Log diagnostic breakdown by CSV day so you can verify older sessions are included
            try:
                breakdown = ", ".join(f"{d}:{n}" for d, n in sorted(per_day_counts.items(), reverse=True))
                LOG.info("Close-delegate: %d CLOSE symbol(s) across %d day(s) [%s].",
                        len(pick), len(paths), breakdown)
            except Exception:
                pass

            # --- Stage 1: per-day CSV -> from-signal CLOSE limits (no live) ---
            # Build per-day lists so PlaceAnOrder reads the correct CSV ('--date' specifies folder)
            by_day: dict[str, list[str]] = {}
            for sym, (row, day_lbl, _) in latest_by_sym.items():
                if sym in pick:
                    by_day.setdefault(day_lbl, []).append(sym)

            for day_lbl, syms in sorted(by_day.items(), reverse=True):
                if not syms:
                    continue
                filtered_syms: list[str] = []
                for s in sorted(set(syms)):
                    if self._has_working_close_order(s):
                        try:
                            self._attempt(
                                symbol=s,
                                action="close",
                                status="skipped",
                                reason=f"close_within_{days}d_existing_working_close_stage1",
                                source="dcm-close",
                            )
                        except Exception:
                            pass
                    else:
                        filtered_syms.append(s)
                if not filtered_syms:
                    continue
                argv_csv = [
                    "--mode", "from-signal",
                    "--date", day_lbl,
                    "--symbols", ",".join(filtered_syms),
                    "--min-limit", "0.05",
                    "--use-live-close", "off",
                    "--quantity","1",
                    "--quiet"
                ]
                try:
                    self._attempt(
                        action="close",
                        status="queued",
                        reason=f"close_from_csv_limits_{day_lbl}",
                        source="dcm-close",
                        symbol=",".join(filtered_syms),
                    )
                except Exception:
                    pass
                self._run_place_an_order(argv_csv)

            # --- Stage 1.5: live-mid fallback via PlaceAnOrder for any symbols still lacking a working close ---
            still_open: list[str] = []
            for s in sorted(set(pick)):
                if not self._has_working_close_order(s):
                    still_open.append(s)

            if still_open:
                syms_joined = ",".join(still_open)
                argv_mid = [
                    "--mode", "force-close",
                    "--symbols", syms_joined,
                    "--min-limit", "0.05",
                    "--use-live-close", "mid",
                    "--quantity","50",
                    "--quiet"
                ]
                try:
                    self._attempt(
                        action="close",
                        status="queued",
                        reason=f"close_live_mid_fallback_within_{days}d",
                        source="dcm-close",
                        symbol=syms_joined,
                    )
                except Exception:
                    pass
                self._run_place_an_order(argv_mid)

            # --- Stage 2: final market fallback for anything still without a working close (positions-based in PlaceAnOrder) ---
            still_open2: list[str] = []
            for s in sorted(set(pick)):
                if not self._has_working_close_order(s):
                    still_open2.append(s)
            if still_open2:
                syms_joined2 = ",".join(still_open2)
                argv_mkt = [
                    "--mode", "force-close",
                    "--symbols", syms_joined2,
                    "--min-limit", "0.05",
                    "--use-live-close", "off",
                    "--quantity","50",
                    "--quiet"
                ]
                try:
                    self._attempt(
                        action="close",
                        status="queued",
                        reason=f"close_market_fallback_within_{days}d",
                        source="dcm-close",
                        symbol=syms_joined2,
                    )
                except Exception:
                    pass
                self._run_place_an_order(argv_mkt)

            # Flatten STK legs if newest signal is CLOSE (delegated CLOSE was just submitted)
            try:
                for s in sorted(set(pick)):
                    try:
                        if self._latest_signal_is_close(s, days):
                            if self._flatten_stock_if_present(s):
                                self._attempt(symbol=s,
                                            action="close_stock",
                                            status="submitted",
                                            reason=f"close_within_{days}d_flatten",
                                            source="dcm-close")
                    except Exception:
                        pass
            except Exception:
                pass

            try:
                self._attempt(action="close", status="submitted",
                            reason=f"close_within_{days}d", source="dcm-close",
                            symbol=",".join(sorted(set(pick))))
            except Exception:
                pass
        finally:
            self._in_close_phase = _prev_phase

    def _get_position_open_date(self, conId: int) -> date | None:
        """
        Query IB execution history and return the date when this contract was opened.
        Returns None if no open execution found within last 30 days.
        """
        try:
            from ib_insync import IB, ExecutionFilter, util
            from datetime import timedelta, date
            from zoneinfo import ZoneInfo

            # Connect to IB
            ib = IB()
            try:
                ib.connect('127.0.0.1', 7497, clientId=885, timeout=6)
            except Exception as e:
                LOG.debug("Position age check: connect failed: %s", e)
                return None

            try:
                # Get executions from last 30 days
                now = self._now_ny()
                since = (now.astimezone(ZoneInfo("UTC")) - timedelta(days=30)).strftime("%Y%m%d-%H:%M:%S")
                fills = ib.reqExecutions(ExecutionFilter(time=since)) or []

                # Find earliest OPEN execution for this conId
                open_times = []
                for f in fills:
                    if f.contract.conId != conId:
                        continue
                    # Filter to OPEN transactions only (not closes)
                    if getattr(f.execution, "openClose", "") != "O":
                        continue
                    try:
                        t_utc = util.parseIBDatetime(getattr(f.execution, "time", ""))
                        if t_utc:
                            t_ny = t_utc.astimezone(ZoneInfo("America/New_York"))
                            open_times.append(t_ny)
                    except Exception:
                        pass

                # Return earliest open date
                if open_times:
                    earliest = min(open_times)
                    return earliest.date()
                return None

            finally:
                try:
                    ib.disconnect()
                except Exception:
                    pass

        except Exception as e:
            LOG.debug("Failed to get position open date for conId %s: %s", conId, e)
            return None

    def _is_previous_trading_day_position(self, open_date: date | None) -> bool:
        """
        Check if position was opened on a PREVIOUS trading day (not today).

        Returns:
            True if opened previous trading day or earlier
            False if opened today OR open_date is None (unknown age)

        Note: Currently only handles weekends. To add holiday support, integrate
        pandas_market_calendars.get_calendar('NYSE').
        """
        if not open_date:
            # Unknown age: treat as same-day (safer, avoids unwanted market orders)
            return False

        today = self._now_ny().date()

        # Same calendar date → same-day position
        if open_date == today:
            return False

        # Check if opened on previous trading day or earlier
        # Walk backwards from today until we hit a trading day
        from datetime import timedelta
        prev_trading_date = today - timedelta(days=1)

        # Skip weekends
        while prev_trading_date.weekday() >= 5:  # 5=Sat, 6=Sun
            prev_trading_date -= timedelta(days=1)

        # Position is from previous trading day or earlier
        return open_date <= prev_trading_date

    def _determine_live_close_scheme_for_symbol(self, symbol: str) -> str:
        """
        Determine which live-close pricing scheme to use based on position age.

        Returns:
            'mid' for same-day positions (conservative)
            'join' for previous-day positions (aggressive)
        """
        try:
            from ib_insync import IB

            # Connect to IB to check position age
            ib = IB()
            try:
                ib.connect('127.0.0.1', 7497, clientId=886, timeout=6)
            except Exception as e:
                LOG.debug("Live-close scheme: connect failed, defaulting to 'mid': %s", e)
                return 'mid'  # Conservative default

            try:
                # Get positions for this symbol
                positions = ib.positions()
                option_positions = [
                    p for p in positions
                    if getattr(p.contract, 'symbol', '').upper() == symbol.upper()
                    and getattr(p.contract, 'secType', '') == 'OPT'
                ]

                if not option_positions:
                    LOG.debug("Live-close scheme: no positions found for %s, using 'mid'", symbol)
                    return 'mid'

                # Get earliest open date across all legs
                open_dates = []
                for p in option_positions:
                    conId = p.contract.conId
                    open_date = self._get_position_open_date(conId)
                    if open_date:
                        open_dates.append(open_date)

                if not open_dates:
                    LOG.info("Live-close scheme: unknown age for %s, using 'mid' (conservative)", symbol)
                    return 'mid'

                earliest_open_date = min(open_dates)

                # Check if previous trading day
                is_prev_day = self._is_previous_trading_day_position(earliest_open_date)

                if is_prev_day:
                    LOG.info("Live-close scheme: %s opened %s (previous trading day), using 'join' (aggressive)",
                             symbol, earliest_open_date)
                    return 'join'
                else:
                    LOG.info("Live-close scheme: %s opened %s (same day), using 'mid' (conservative)",
                             symbol, earliest_open_date)
                    return 'mid'

            finally:
                try:
                    ib.disconnect()
                except Exception:
                    pass

        except Exception as e:
            LOG.warning("Live-close scheme determination failed for %s: %s, defaulting to 'mid'", symbol, e)
            return 'mid'  # Conservative default

    def _try_close_from_positions(self, sym: str, prefer: str = "LMT", side: str | None = None) -> bool:
        """
        Best-effort direct close using current IB positions for `sym`.
        - If a long vertical debit is detected (CALL or PUT), submit a combo close as a BAG:
            * legs: BUY long leg back? (No) — For closing a long vertical we submit an overall SELL order and specify
              legs as BUY (lower/call or higher/put) and SELL (higher/call or lower/put) matching the original open.
              This mirrors IB's combo behavior as used by PlaceAnOrder.
        - If a short vertical is detected, submit an overall BUY.
        - If no clean vertical is found but orphan option legs exist, flatten each leg with a market order.
        - If a stock position is present for `sym`, flatten it (SELL if long, BUY if short).
        Returns True if at least one order was submitted.

        When prefer='LMT', attempts to read theoretical limit from CSV; falls back to MKT if unavailable.
        """
        try:
            from ib_insync import IB, Contract, ComboLeg, ContractDetails, MarketOrder, LimitOrder
        except Exception as e:
            LOG.warning("direct-close: ib_insync unavailable: %s", e)
            return False

        ib = IB()
        # Use a mostly-unique clientId per invocation to avoid "client id already in use"
        try:
            import random
            client_id = 880 + random.randint(0, 99)
        except Exception:
            client_id = 884  # fallback

        try:
            ib.connect('127.0.0.1', 7497, clientId=client_id, timeout=6)
        except Exception as e:
            LOG.warning("direct-close: connect failed (clientId=%s): %s", client_id, e)
            return False

        # Helper to get theoretical close limit from CSV
        def _get_theo_limit(right: str, width: float) -> float | None:
            """Look up theoretical limit from today's CSV for the symbol/right/width."""
            try:
                csv_path = _ny_csv_path()
                if not csv_path or not os.path.exists(csv_path):
                    LOG.debug("direct-close: CSV not found at %s", csv_path)
                    return None
                import pandas as pd
                df = pd.read_csv(csv_path)
                if "symbol" not in df.columns:
                    return None
                df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
                rows = df[df["symbol"] == up]
                if rows.empty:
                    return None
                row = rows.iloc[-1]  # Use latest row for symbol
                # Determine width bucket
                if abs(width - 1.0) < 0.5:
                    bucket = "1"
                elif abs(width - 2.5) < 0.75:
                    bucket = "2_5"
                else:
                    bucket = "5"
                # Try limit columns first, then theo
                prefix = "call_debit" if right.upper().startswith("C") else "put_debit"
                LOG.debug("[%s] _get_theo_limit: right=%s, width=%s, bucket=%s, prefix=%s", up, right, width, bucket, prefix)
                for kind in ("limit", "theo"):
                    col = f"{prefix}_{kind}_{bucket}"
                    LOG.debug("[%s] _get_theo_limit: trying column %s, exists=%s", up, col, col in df.columns)
                    if col in df.columns:
                        try:
                            v = row[col]
                            LOG.debug("[%s] _get_theo_limit: %s raw value=%s, notna=%s", up, col, v, pd.notna(v))
                            if pd.notna(v):
                                v = float(v)
                                LOG.debug("[%s] _get_theo_limit: %s float value=%s, valid=%s", up, col, v, v > 0)
                                if v > 0:
                                    LOG.info("direct-close: found theo limit for %s %s width=%s: %s from col %s", up, right, width, v, col)
                                    return round(v, 2)
                        except Exception as ex:
                            LOG.debug("direct-close: failed to parse %s for %s: %s", col, up, ex)
                LOG.debug("direct-close: no theo limit found for %s %s width=%s bucket=%s", up, right, width, bucket)
                return None
            except Exception as e:
                LOG.debug("direct-close: failed to get theo limit for %s %s: %s", up, right, e)
                return None

        submitted = False
        up = (sym or '').upper()
        try:
            # Collect all open legs for symbol (OPT and STK)
            poss = ib.positions() or []
            opt_legs = []   # list of dicts for OPT legs
            stk_leg  = None # tuple(qty, contract) for stock
            for p in poss:
                c = p.contract
                q = float(p.position or 0.0)
                if abs(q) < 1e-9:
                    continue
                if getattr(c, 'symbol', '').upper() != up:
                    continue
                if getattr(c, 'secType', '') == 'OPT':
                    opt_legs.append({
                        'conId': c.conId,
                        'right': getattr(c, 'right', '').upper(),
                        'strike': float(getattr(c, 'strike', 0.0)),
                        'qty': q
                    })
                elif getattr(c, 'secType', '') == 'STK':
                    stk_leg = (q, c)

            # Helper to place a combo close
            def _place_combo(right: str, low: float, high: float, longConId: int, shortConId: int, is_long_vertical: bool) -> bool:
                # Guard: Check for existing working orders before placing
                if has_working_auto_close(up):
                    LOG.info("direct-close: existing working CLOSE found for %s - skipping duplicate order placement", up)
                    return False  # Skip placing order, already have working order

                try:
                    bag = Contract()
                    bag.symbol = up
                    bag.secType = 'BAG'
                    bag.exchange = 'SMART'
                    bag.currency = 'USD'
                    bag.comboLegs = [
                        ComboLeg(conId=longConId, ratio=1, action='BUY', exchange='SMART'),
                        ComboLeg(conId=shortConId, ratio=1, action='SELL', exchange='SMART')
                    ]
                    action = 'SELL' if is_long_vertical else 'BUY'

                    # Determine order type: try LMT with theo value, fall back to MKT (if allowed)
                    width = abs(high - low)
                    theo_limit = _get_theo_limit(right, width) if prefer.upper() == 'LMT' else None

                    if theo_limit is not None:
                        # Have CSV limit: use it
                        order = LimitOrder(action, 1, theo_limit)
                        order_desc = f"LMT @ {theo_limit:.2f}"
                    else:
                        # No CSV limit: check position age before allowing market fallback
                        # Get open dates for both legs
                        open_date_long = self._get_position_open_date(longConId)
                        open_date_short = self._get_position_open_date(shortConId)

                        # Use earliest open date across legs (most conservative)
                        if open_date_long and open_date_short:
                            position_open_date = min(open_date_long, open_date_short)
                        else:
                            position_open_date = open_date_long or open_date_short

                        # Check if this is a previous trading day position
                        is_prev_day = self._is_previous_trading_day_position(position_open_date)

                        if is_prev_day:
                            # Previous trading day: allow market fallback
                            order = MarketOrder(action, 1)
                            order_desc = f"MKT (opened {position_open_date})"
                            LOG.info("direct-close: allowing market fallback for %s %s (opened %s)", up, right, position_open_date)
                        else:
                            # Same-day OR unknown age: limit orders only
                            age_desc = f"opened {position_open_date}" if position_open_date else "unknown age"
                            LOG.info("direct-close: skipping market fallback for %s %s vertical %s/%s (%s, no CSV limit)",
                                     up, right, low, high, age_desc)
                            return False  # Skip this order, don't place anything

                    tr = ib.placeOrder(bag, order)
                    LOG.info("direct-close: %s %s vertical %s/%s -> %s %s", up, right, low, high, action, order_desc)
                    return True
                except Exception as e:
                    LOG.warning("direct-close: combo place failed for %s %s %s/%s: %s", up, right, low, high, e)
                    return False

            # Try to detect a vertical debit (long) or short vertical for CALLs
            calls = sorted([l for l in opt_legs if l['right'] == 'C'], key=lambda x: x['strike'])
            puts  = sorted([l for l in opt_legs if l['right'] == 'P'], key=lambda x: x['strike'])

            # Detect CALL vertical: long + at lower strike and short - at higher strike
            if side is None or side.lower() == "call":
                for i in range(len(calls)):
                    for j in range(i+1, len(calls)):
                        lo, hi = calls[i], calls[j]
                        if lo['qty'] > 0 and hi['qty'] < 0:
                            if _place_combo('CALL', lo['strike'], hi['strike'], lo['conId'], hi['conId'], True):
                                submitted = True
                                break
                        elif lo['qty'] < 0 and hi['qty'] > 0:
                            # short call vertical (credit) -> close with BUY
                            if _place_combo('CALL', lo['strike'], hi['strike'], hi['conId'], lo['conId'], False):
                                submitted = True
                                break
                    if submitted:
                        break

            # Detect PUT vertical: long + at higher strike and short - at lower strike
            if not submitted and (side is None or side.lower() == "put"):
                for i in range(len(puts)):
                    for j in range(i+1, len(puts)):
                        lo, hi = puts[i], puts[j]  # lo has lower strike, hi higher strike
                        # long put vertical: + at higher (hi), - at lower (lo)
                        if hi['qty'] > 0 and lo['qty'] < 0:
                            if _place_combo('PUT', lo['strike'], hi['strike'], hi['conId'], lo['conId'], True):
                                submitted = True
                                break
                        elif hi['qty'] < 0 and lo['qty'] > 0:
                            # short put vertical -> close with BUY
                            if _place_combo('PUT', lo['strike'], hi['strike'], lo['conId'], hi['conId'], False):
                                submitted = True
                                break
                    if submitted:
                        break

            # If no vertical combo placed, flatten orphan option legs (optionally side-filtered)
            if not submitted and opt_legs:
                for leg in opt_legs:
                    if side is not None and leg["right"] != side.upper()[0]:
                        # Skip legs on the other side when we are asked to close only CALL or only PUT
                        continue
                    try:
                        c = Contract(conId=leg['conId'])
                        action = 'SELL' if leg['qty'] > 0 else 'BUY'
                        order = MarketOrder(action, int(abs(leg['qty'])))
                        ib.placeOrder(c, order)
                        submitted = True
                        LOG.info("direct-close: flattened single leg %s %s @ strike %.2f via %s", up, leg['right'], leg['strike'], action)
                    except Exception as e:
                        LOG.warning("direct-close: failed to flatten leg %s %s %.2f: %s", up, leg['right'], leg['strike'], e)

            # Flatten stock position if present
            if stk_leg:
                qty, c = stk_leg
                try:
                    action = 'SELL' if qty > 0 else 'BUY'
                    order = MarketOrder(action, int(abs(qty)))
                    ib.placeOrder(c, order)
                    submitted = True
                    LOG.info("direct-close: flattened stock position %s qty=%s via %s", up, qty, action)
                except Exception as e:
                    LOG.warning("direct-close: failed to flatten stock %s: %s", up, e)

            return submitted
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass
    """
    Mixin that provides:
      - robust market session checks (open/after-close) in America/New_York
      - idempotency guards to avoid re-running the same cycle too frequently
      - structured orchestration for daily and weekly cycles

    This assumes the host class implements the following methods:
      - scan_sector_candidates(sectors: list[str]) -> None
      - filter_candidates_by_criteria() -> list[str]
      - update_historical_data(tickers: list[str]) -> None
      - organize_sector_data() -> dict
      - optimize_sector_strategy(sector_data: dict) -> dict
      - select_top_performers(sector_data: dict, n: int) -> dict|list
      - generate_trade_signals(top_candidates) -> list|dict
      - prepare_next_day_orders(signals) -> None
      - execute_pending_orders() -> None
      - manage_existing_positions() -> None
      - retry_failed_orders() -> None
      - update_all_historical_data() -> None
      - reoptimize_all_sectors() -> None
      - remove_high_iv_candidates() -> None
      - rebalance_sector_exposure() -> None
      - convert_unfilled_close_limits_to_market(cutoff: time = PRE_CLOSE_SWEEP) -> None
      - place_end_of_day_signals() -> None
      - enforce_recent_closures(days: int = 7) -> None

    Optional hooks:
      - pre_daily_analysis() / post_daily_analysis()
      - pre_market_open() / post_market_open()
      - pre_weekly_maintenance() / post_weekly_maintenance()
      - (optional) convert_unfilled_close_limits_to_market(cutoff=PRE_CLOSE_SWEEP)
      - (optional) place_end_of_day_signals()
      - (optional) enforce_recent_closures(days=7)
    """

    # simple in-memory run guards; replace with persistent store if running in multiple processes
    _last_daily_analysis_at: datetime | None = None
    _last_weekly_maintenance_at: datetime | None = None

    def _python_executable(self) -> str:
        """
        Choose the Python interpreter for launching PlaceAnOrder.py.
        Prefers the repo-local Windows venv if it exists; otherwise uses the current interpreter.
        """
        try:
            if sys.platform.startswith("win") and VENV_PY_WIN.exists():
                return str(VENV_PY_WIN)
        except Exception:
            pass
        return sys.executable

    def _run_liquidity_filter_for_folder(self, folder_yy_mm_dd: str, only_rth: bool = True, timeout: int = 600) -> None:
        """
        Launch LiquidityFilter.py for a specific C:\OptionsHistory\<yy_mm_dd> folder.
        """
        python = self._python_executable()
        script = str(LIQUIDITY_FILTER_PATH)
        argv = [python, script, "--day-dir", fr"C:\OptionsHistory\{folder_yy_mm_dd}"]
        if only_rth:
            argv.append("--only-rth")
        LOG.info("Launching LiquidityFilter for %s%s", folder_yy_mm_dd, " (only_rth)" if only_rth else "")
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            if proc.stdout:
                LOG.info("[LiquidityFilter stdout]\n%s", proc.stdout.strip())
            if proc.stderr:
                LOG.warning("[LiquidityFilter stderr]\n%s", proc.stderr.strip())
            if proc.returncode != 0:
                LOG.error("LiquidityFilter exited with code %s for %s", proc.returncode, folder_yy_mm_dd)
        except subprocess.TimeoutExpired:
            LOG.error("LiquidityFilter timed out after %ss for %s", timeout, folder_yy_mm_dd)
        except FileNotFoundError:
            LOG.error("LiquidityFilter.py not found at %s", script)
        except Exception as e:
            LOG.exception("Failed to launch LiquidityFilter for %s: %s", folder_yy_mm_dd, e)

    def _prev_trading_day_folder(self, ref_dt: datetime | None = None) -> str:
        """
        Return YY_MM_DD string for the previous *trading* day (skips Sat/Sun).
        If ref_dt is Monday, this returns the prior Friday.
        """
        dt = (ref_dt or self._now_ny()).date()
        from datetime import timedelta
        d = dt - timedelta(days=1)
        # Skip weekend days
        while d.weekday() >= 5:  # 5=Sat, 6=Sun
            d -= timedelta(days=1)
        return d.strftime("%y_%m_%d")

    def _enrich_today_and_prev_trading_day(self, only_rth: bool = True) -> None:
        """
        Run LiquidityFilter for today's folder and the previous trading day's folder.
        """
        try:
            today_folder = self._now_ny().strftime("%y_%m_%d")
        except Exception:
            from datetime import datetime
            today_folder = datetime.now(NY).strftime("%y_%m_%d")
        prev_folder = self._prev_trading_day_folder()
        self._run_liquidity_filter_for_folder(today_folder, only_rth=only_rth)
        # Avoid duplicate run if prev == today (shouldn't happen, but safe)
        if prev_folder != today_folder:
            self._run_liquidity_filter_for_folder(prev_folder, only_rth=only_rth)

    def _run_liquidity_filter(self, only_rth: bool = True, timeout: int = 600) -> None:
        """
        Best-effort launcher for LiquidityFilter.py to enrich today's combined CSV with OI (and other fields).
        When `only_rth` is True, the script will no-op outside RTH.
        """
        try:
            ny_now = self._now_ny()
        except Exception:
            from datetime import datetime
            ny_now = datetime.now(NY)
        folder = ny_now.strftime("%y_%m_%d")
        self._run_liquidity_filter_for_folder(folder, only_rth=only_rth, timeout=timeout)

    def _populate_missing_strikes_for_folder(self, folder_yy_mm_dd: str, timeout: int = 120) -> int:
        """
        Launch LiquidityFilter.py --populate-strikes for a specific folder.
        Returns the number of rows updated (0 if none or on error).
        """
        python = self._python_executable()
        script = str(LIQUIDITY_FILTER_PATH)
        argv = [python, script, "--day-dir", fr"C:\OptionsHistory\{folder_yy_mm_dd}", "--populate-strikes"]
        LOG.info("Populating missing strikes for %s", folder_yy_mm_dd)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            if proc.stdout:
                LOG.info("[populate-strikes stdout]\n%s", proc.stdout.strip())
            if proc.stderr:
                LOG.warning("[populate-strikes stderr]\n%s", proc.stderr.strip())
            if proc.returncode != 0:
                LOG.error("populate-strikes exited with code %s for %s", proc.returncode, folder_yy_mm_dd)
                return 0
            # Parse output to get count
            import re
            match = re.search(r'updated (\d+) rows', proc.stdout or "")
            return int(match.group(1)) if match else 0
        except subprocess.TimeoutExpired:
            LOG.error("populate-strikes timed out after %ss for %s", timeout, folder_yy_mm_dd)
        except FileNotFoundError:
            LOG.error("LiquidityFilter.py not found at %s", script)
        except Exception as e:
            LOG.exception("Failed to populate strikes for %s: %s", folder_yy_mm_dd, e)
        return 0

    def _populate_missing_strikes_today_and_prev(self) -> int:
        """
        Populate missing strikes in today's and previous trading day's CSVs.
        Returns total rows updated.
        """
        try:
            today_folder = self._now_ny().strftime("%y_%m_%d")
        except Exception:
            from datetime import datetime
            today_folder = datetime.now(NY).strftime("%y_%m_%d")
        prev_folder = self._prev_trading_day_folder()
        total = self._populate_missing_strikes_for_folder(today_folder)
        if prev_folder != today_folder:
            total += self._populate_missing_strikes_for_folder(prev_folder)
        return total

    def _has_working_close_order(self, sym: str) -> bool:
        """
        Return True if there is an existing working CLOSE order (SELL combo) for the given symbol.
        Treats active working states as working, and also treats 'Inactive' orders as working
        after hours (both GTC and DAY orders pause when market closes).
        """
        try:
            from ib_insync import IB
        except Exception:
            return False

        ib = IB()
        try:
            ib.connect('127.0.0.1', 7497, clientId=883, timeout=6)
        except Exception:
            return False

        try:
            trades = ib.openTrades() or []
            up = (sym or "").upper()
            # states that indicate an order is still alive/working
            working_states = {"presubmitted", "submitted", "pendingsubmit", "apipending"}

            # Check if we're after hours (market closed)
            from datetime import time
            is_after_hours = False
            try:
                t = self._now_ny().time()
                # After 4:00 PM or before 9:30 AM ET
                if (t >= time(16, 0)) or (t < time(9, 30)):
                    is_after_hours = True
            except Exception:
                pass

            for tr in trades:
                c = getattr(tr, "contract", None)
                o = getattr(tr, "order", None)
                s = getattr(tr, "orderStatus", None)
                if not c or not o or not s:
                    continue
                if getattr(c, "secType", "") != "BAG":
                    continue
                if (getattr(c, "symbol", "") or "").upper() != up:
                    continue
                if (getattr(o, "action", "") or "").upper() != "SELL":
                    continue

                st = (getattr(s, "status", "") or "").lower()
                if st in ("filled", "cancelled", "apicancelled"):
                    continue

                # Consider inactive orders as "working/held" after hours
                # (both GTC and DAY orders pause when market is closed)
                is_gtc = (getattr(o, "tif", "") or "").upper() == "GTC"
                if st in working_states:
                    return True
                if st == "inactive":
                    # GTC is always working when inactive
                    if is_gtc:
                        return True
                    # DAY orders are also working after hours (they'll activate at market open)
                    if is_after_hours:
                        return True
            return False
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    def _flatten_stock_if_present(self, sym: str) -> bool:
        """
        If there is an open STOCK (STK) position for `sym`, flatten it with a MARKET order.
        Returns True if a stock order was submitted.
        """
        try:
            from ib_insync import IB, Contract, MarketOrder
        except Exception as e:
            LOG.warning("flatten-stock: ib_insync unavailable: %s", e)
            return False

        ib = IB()
        try:
            ib.connect('127.0.0.1', 7497, clientId=881, timeout=6)
        except Exception as e:
            LOG.warning("flatten-stock: connect failed: %s", e)
            return False

        submitted = False
        up = (sym or "").upper()
        try:
            for p in ib.positions() or []:
                c = getattr(p, "contract", None)
                if getattr(c, "secType", "") != "STK":
                    continue
                if (getattr(c, "symbol", "") or "").upper() != up:
                    continue
                qty = float(getattr(p, "position", 0.0) or 0.0)
                if abs(qty) < 1e-9:
                    continue
                try:
                    action = "SELL" if qty > 0 else "BUY"
                    ord_qty = int(abs(qty))
                    order = MarketOrder(action, ord_qty)
                    ib.placeOrder(c, order)
                    submitted = True
                    LOG.info("flatten-stock: submitted %s %s x%d via MKT (weekly/reconcile path)", up, action, ord_qty)
                    try:
                        _AttemptLogger.write(symbol=up, action="close_stock", status="submitted",
                                             reason="reconcile_close_signal",
                                             exp="", right="STK", source="dcm-reconcile")
                    except Exception:
                        pass
                except Exception as e:
                    LOG.warning("flatten-stock: failed to place stock close for %s: %s", up, e)
            return submitted
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    def _run_place_an_order(self, argv: list[str], timeout: int = 900) -> None:
        """
        Best-effort launcher for PlaceAnOrder.py with given args.
        Captures stdout/stderr into the log for diagnostics.
        """
        python = self._python_executable()
        script = str(PLACE_AN_ORDER_PATH)
        cmd = [python, script] + argv
        LOG.info("Launching PlaceAnOrder: %s", " ".join(argv))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if proc.stdout:
                LOG.info("[PlaceAnOrder stdout]\n%s", proc.stdout.strip())
            if proc.stderr:
                LOG.warning("[PlaceAnOrder stderr]\n%s", proc.stderr.strip())
            if proc.returncode != 0:
                LOG.error("PlaceAnOrder exited with code %s", proc.returncode)
        except subprocess.TimeoutExpired:
            LOG.error("PlaceAnOrder timed out after %ss with args: %s", timeout, " ".join(argv))
        except FileNotFoundError:
            LOG.error("PlaceAnOrder.py not found at %s", script)
        except Exception as e:
            LOG.exception("Failed to launch PlaceAnOrder: %s", e)

    def submit_opens_via_place_anorder(self,
                                       symbols: list[str] | set[str],
                                       date: str | None = None,
                                       use_live_open: str = "join",
                                       min_limit: float = 0.05,
                                       bump_to_min: bool = True,
                                       quiet: bool = True) -> None:
        """
        Convenience: delegate OPEN placement for a set of symbols to PlaceAnOrder.py.
        - If `date` is provided, runs on that YY_MM_DD folder; otherwise PlaceAnOrder uses today's folder.
        - Uses --mode from-signal so the CSV's signal_type governs side (CALL/PUT).
        """
        if not symbols:
            return
        
        #Guard: do not delegate new OPENs for symbols where we already hold any options exposure.
        try:
            held_signs = self._collect_held_orientations()
            held_syms = set(held_signs.keys())
        except Exception:
            held_syms = set()

        filtered: list[str] = []
        for s in symbols:
            sym_u = (s or "").strip().upper()
            if not sym_u:
                continue
            if sym_u in held_syms:
                # Skip opens for symbols where we already have any OPT position; log for diagnostics.
                try:
                    self._attempt(symbol=sym_u, action="open", status="skipped",
                                    reason="skip_open_any_position_dcm", source="dcm")
                except Exception:
                    pass
                continue
            filtered.append(sym_u)

        if not filtered:
            # Nothing left to submit after filtering held symbols.
            return
        # Normalize & sort (use the filtered list, not the original symbols)
        syms = sorted(set(filtered))
        argv = ["--mode", "from-signal",
                "--symbols", ",".join(syms),
                "--min-limit", f"{min_limit:.2f}",
                "--use-live-open", (use_live_open or "off")]
        if bump_to_min:
            argv.append("--bump-to-min")
        if date:
            argv += ["--date", date]
        if quiet:
            argv.append("--quiet")

        try:
            _AttemptLogger.write(action="open", status="queued",
                                 reason="dcm_submit_opens", source="dcm", symbol=",".join(syms))
        except Exception:
            pass

        self._run_place_an_order(argv)

        try:
            _AttemptLogger.write(action="open", status="submitted",
                                 reason="dcm_submit_opens", source="dcm", symbol=",".join(syms))
        except Exception:
            pass

    def submit_closes_via_place_anorder(self,
                                        symbols: list[str] | set[str],
                                        use_live_close: str = "join",
                                        min_limit: float = 0.05,
                                        quiet: bool = True,
                                        fallback_individual_legs: bool = False) -> None:
        """
        Convenience: delegate CLOSE placement (force-close) for a set of symbols to PlaceAnOrder.py.
        This path ignores today's CSV contents and will inspect IB positions in PlaceAnOrder.

        If fallback_individual_legs=True, will close individual legs if one leg is worthless (< min_limit).
        """
        if not symbols:
            return
        syms = sorted({(s or "").strip().upper() for s in symbols if (s or "").strip()})
        argv = ["--mode", "force-close",
                "--symbols", ",".join(syms),
                "--min-limit", f"{min_limit:.2f}",
                "--use-live-close", (use_live_close or "off"),
                "--quantity","50"]
        if fallback_individual_legs:
            argv.append("--fallback-individual-legs")
        if quiet:
            argv.append("--quiet")

        try:
            _AttemptLogger.write(action="close", status="queued",
                                 reason="dcm_submit_closes", source="dcm", symbol=",".join(syms))
        except Exception:
            pass

        self._run_place_an_order(argv)

        try:
            _AttemptLogger.write(action="close", status="submitted",
                                 reason="dcm_submit_closes", source="dcm", symbol=",".join(syms))
        except Exception:
            pass

    def submit_open_for(self, symbol: str, **kw) -> None:
        self.submit_opens_via_place_anorder([symbol], **kw)

    def submit_close_for(self, symbol: str, **kw) -> None:
        self.submit_closes_via_place_anorder([symbol], **kw)

    def _safe_host_call(self, name: str, *args, **kwargs):
        """
        Safely call an optional host-implemented method.
        If missing, log and skip. If it raises, log the exception and skip.
        Returns the method's return value or None on failure/missing.
        """
        if not hasattr(self, name):
            LOG.warning("Host method '%s' not implemented; skipping.", name)
            return None
        try:
            return getattr(self, name)(*args, **kwargs)
        except Exception as e:
            LOG.exception("Host method '%s' raised an exception: %s", name, e)
            return None

    def _attempt(self, **kw):
        """Best-effort attempts logger wrapper."""
        try:
            _AttemptLogger.write(**kw)
        except Exception:
            pass

    def _warn_missing_sector_data(self, context: str = "Analysis"):
        """
        Log the standardized message requested by the user when sector data is missing.
        """
        LOG.error("Sector data is missing. Recommend running HistoricalDataCollector.py; unable to optimize. [%s]", context)

    # ---------- Time & Session Utilities ----------
    def _now_ny(self) -> datetime:
        return datetime.now(NY)

    def _is_trading_day(self, when: datetime | None = None) -> bool:
        """Very simple weekday check. Consider plugging in an exchange/holiday calendar."""
        dt = when or self._now_ny()
        # Monday(0) .. Friday(5) -> trading days; Sunday(6) is not
        return dt.weekday() < 5

    def is_market_open(self, when: datetime | None = None) -> bool:
        dt = when or self._now_ny()
        if not self._is_trading_day(dt):
            return False
        t = dt.time()
        return (t >= MARKET_OPEN) and (t < MARKET_CLOSE)

    def is_after_market_close(self, when: datetime | None = None) -> bool:
        dt = when or self._now_ny()
        if not self._is_trading_day(dt):
            # allow running after close on non-trading days for nightly jobs if desired
            return True
        return dt.time() >= MARKET_CLOSE

    def _is_between(self, t: time, start: time, end: time) -> bool:
        """Return True if time t is within [start, end)."""
        return (t >= start) and (t < end)

    def is_pre_close_window(self, when: datetime | None = None) -> bool:
        dt = when or self._now_ny()
        if not self._is_trading_day(dt):
            return False
        return self._is_between(dt.time(), PRE_CLOSE_SWEEP, min(PRE_CLOSE_SWEEP_END, MARKET_CLOSE))

    def is_after_hours_placement(self, when: datetime | None = None) -> bool:
        dt = when or self._now_ny()
        # Allow after-hours placement on any day post close
        return dt.time() >= AFTER_HOURS_PLACEMENT

    # ---------- Idempotency Guards ----------
    def _can_run_daily_analysis(self) -> bool:
        now = self._now_ny()
        if self._last_daily_analysis_at is None:
            return True
        return (now - self._last_daily_analysis_at) >= timedelta(hours=DAILY_ANALYSIS_COOLDOWN_HOURS)

    def _mark_daily_analysis(self) -> None:
        self._last_daily_analysis_at = self._now_ny()

    def _can_run_weekly_maintenance(self) -> bool:
        now = self._now_ny()
        # only on the configured weekday
        if now.weekday() != WEEKLY_MAINTENANCE_DAY:
            return False
        if self._last_weekly_maintenance_at is None:
            return True
        # at most once per calendar day
        return self._last_weekly_maintenance_at.date() != now.date()

    def _mark_weekly_maintenance(self) -> None:
        self._last_weekly_maintenance_at = self._now_ny()

    # ---------- Mid/End-of-day Helpers (safe, optional) ----------
    def _pre_close_market_conversion(self) -> None:
        """
        Pre-close behavior (≈ 15:00 ET):
        - Identify symbols to be closed because the latest signal is CLOSE or the latest OPEN mismatches our held orientation.
        - Include any symbols that already have working CLOSE LMT combo orders.
        - Delegate close; if no working close remains, force a positions-based MKT close.
        """
        lookback_days = 21
        # today's CSV presence (affects logging only)
        csv_today_path = _ny_csv_path()
        csv_exists_today = os.path.exists(csv_today_path)
        if not csv_exists_today:
            LOG.warning("Pre-close: today's combined CSV missing at %s; proceeding with positions-based fallback as needed.", csv_today_path)

        # gather sources
        work_syms = self._working_close_limit_symbols()
        held_signs = self._collect_held_orientations()
        held_syms  = set(held_signs.keys())

        # decide candidates: (A) any with latest CLOSE, (B) orientation mismatch vs latest OPEN, plus (C) working LMTs
        close_candidates = set(work_syms)
        non_candidates = []
        for s in sorted(held_syms | work_syms):
            try:
                if self._latest_signal_is_close(s, lookback_days):
                    close_candidates.add(s); continue
                open_sign = self._latest_open_sign(s, lookback_days)
                cur_sign  = held_signs.get(s)
                if (open_sign is not None) and (cur_sign is not None) and (open_sign != cur_sign):
                    close_candidates.add(s)
                else:
                    non_candidates.append(s)
            except Exception as e:
                LOG.warning("Pre-close: evaluation failed for %s: %s", s, e)

        if non_candidates:
            LOG.info("Pre-close: skipping (no close/mismatch): %s", ", ".join(sorted(non_candidates)))
        # Add any credit/inverted verticals to the close candidate set
        try:
            credit_syms = self._detect_credit_or_inverted_spreads()
            if credit_syms:
                LOG.info("Pre-close: adding credit/inverted symbols to close candidates: %s",
                         ", ".join(sorted(credit_syms)))
                close_candidates |= credit_syms
        except Exception as e:
            LOG.warning("Pre-close: credit-scan failed; continuing without it: %s", e)
        if not close_candidates:
            LOG.info("Pre-close: no symbols to convert/place.")
            try:
                self._attempt(action="preclose", status="skipped", reason="no_candidates", source="dcm-preclose")
                self._summarize_latest_attempts()
            except Exception:
                pass
            return
        for sym in sorted(close_candidates):
            self._submit_close_shared(sym, csv_exists_today, lookback_days, context="preclose")
        try:
            self._summarize_latest_attempts()
        except Exception as e:
            LOG.warning("Attempts summary failed: %s", e)

    def _after_hours_batch_placement(self) -> None:
        """
        After-hours orchestration that *only* delegates to PlaceAnOrder.py.
        Policy: DCM never talks to the broker for opens; it filters signals and calls PlaceAnOrder.
        - Use last 2 CSVs; only OPENs with DTE >= 20.
        - Weekly CSV-based CLOSE sweeps only on Sundays, using a 21-day window.
        """
        ran_host = False
        if hasattr(self, "place_end_of_day_signals"):
            LOG.info("After-hours batch placement (host hook) starting...")
            try:
                self.place_end_of_day_signals()
                LOG.info("After-hours batch placement (host) completed.")
                ran_host = True
            except Exception as e:
                LOG.exception("After-hours batch placement (host) failed: %s", e)

        # Delegate OPENs from most recent CSVs under policy
        self._delegate_open_from_recent_csvs(min_dte=20, last_n_csvs=2)

        # Delegate CLOSEs from recent signals only on Sunday (America/New_York), using a 21-day window
        try:
            ny_today = self._now_ny()
        except Exception:
            from datetime import datetime as _dt
            ny_today = _dt.now(NY)

        if ny_today.weekday() == 6:  # Sunday = 6 (Mon=0)
            LOG.info("After-hours: running weekly CSV-based CLOSE sweeps for last 21 days (Sunday).")
            self._delegate_close_from_csvs_within(days=21)
        else:
            LOG.info("After-hours: skipping weekly CSV-based CLOSE sweeps (not Sunday).")
        # NEW: after-hours credit/inverted sweep (daily)
        try:
            credit_syms = self._detect_credit_or_inverted_spreads()
            if credit_syms:
                LOG.info("After-hours: enforcing credit/inverted cleanup for: %s",
                         ", ".join(sorted(credit_syms)))
                # Use positions-based force-close path in PlaceAnOrder.py
                # Enable fallback to close individual legs if one is worthless
                self.submit_closes_via_place_anorder(
                    symbols=credit_syms,
                    use_live_close="join",
                    min_limit=0.05,
                    quiet=True,
                    fallback_individual_legs=True,  # Close individual legs if combo fails
                )
        except Exception as e:
            LOG.warning("After-hours: credit-scan/cleanup failed: %s", e)

        self._summarize_latest_attempts()

    def _diagnostic_open_from_signal(self, method: str = "join", min_limit: float = 0.05, bump_to_min: bool = True) -> None:
        """
        Diagnostic helper to try (re)placing today's OPEN orders directly from the CSV using PlaceAnOrder,
        with a controllable live-limit method ('join' or 'mid'). After the run, summarize latest attempts.
        """
        # First, populate any missing strikes in today's and previous day's CSVs
        try:
            updated = self._populate_missing_strikes_today_and_prev()
            if updated:
                LOG.info("Pre-open: populated %d rows with missing strikes", updated)
        except Exception as e:
            LOG.warning("Pre-open: failed to populate missing strikes: %s", e)

        argv = [
            "--mode", "from-signal",
            "--min-limit", f"{min_limit:.2f}",
            "--use-live-open", method,
            "--quiet"
        ]
        if bump_to_min:
            argv.append("--bump-to-min")
        try:
            LOG.info("Diagnostic open-from-signal: using %s limits, min_limit=%.2f, bump_to_min=%s", method, min_limit, bump_to_min)
            self._run_place_an_order(argv)
        finally:
            try:
                self._summarize_latest_attempts()
            except Exception as e:
                LOG.warning("Attempts summary failed: %s", e)

    def _summarize_latest_attempts(self) -> None:
        """
        Read the most recent attempts_*.csv from C:\\OptionsHistory\\logs and log a concise summary:
        total placed, and grouped counts for non-placed by 'reason'. Also log last 20 not-placed rows.
        """
        try:
            # heartbeat row to ensure attempts csv exists on days with no placements
            try: _AttemptLogger.write(action="heartbeat", status="ok", reason="attempts_summary", source="dcm")
            except Exception: pass
            import glob, csv, os
            # Search today's dated folder first, then logs as a fallback
            ny_today  = datetime.now(NY).strftime("%y_%m_%d")
            root_today = fr"C:\OptionsHistory\{ny_today}" if sys.platform.startswith("win") else f"./{ny_today}"
            root_logs  = r"C:\OptionsHistory\logs" if sys.platform.startswith("win") else "./logs"

            paths = []
            for root_dir in (root_today, root_logs):
                if os.path.exists(root_dir):
                    paths.extend(glob.glob(os.path.join(root_dir, "attempts_*.csv")))
            # Prefer most recently modified
            paths = sorted(paths, key=os.path.getmtime, reverse=True)

            # If the active in-memory path exists, put it first
            try:
                active = getattr(_AttemptLogger, "_active_path", None)
                if active and os.path.exists(active):
                    paths.insert(0, active)
            except Exception:
                pass

            if not paths:
                LOG.info("Attempts summary: no attempts_*.csv found in %s or %s", root_today, root_logs)
                return

            latest = paths[0]
            placed = 0
            non = {}
            rows = []
            with open(latest, newline="", encoding="utf-8") as fh:
                rdr = csv.DictReader(fh)
                for r in rdr:
                    rows.append(r)
            for r in rows:
                st = (r.get("status") or "").strip().lower()
                if st == "placed":
                    placed += 1
                else:
                    reason = (r.get("reason") or "").strip() or "(unknown)"
                    non[reason] = non.get(reason, 0) + 1
            LOG.info("Attempts summary (%s): placed=%d, not-placed=%d", latest, placed, len(rows) - placed)
            if non:
                LOG.info("Not-placed by reason:")
                for k, v in sorted(non.items(), key=lambda kv: kv[1], reverse=True):
                    LOG.info("  %-32s : %d", k, v)
            # Log last 20 not-placed lines for quick inspection
            not_placed = [r for r in rows if (r.get("status") or "").strip().lower() != "placed"]
            tail = not_placed[-20:]
            if tail:
                LOG.info("Last %d not-placed rows:", len(tail))
                for r in tail:
                    LOG.info("  ts=%s sym=%s action=%s exp=%s right=%s limit=%s reason=%s",
                             r.get("ts",""), r.get("symbol",""), r.get("action",""),
                             r.get("exp",""), r.get("right",""), r.get("limit",""), r.get("reason",""))
        except Exception as e:
            LOG.warning("Attempts summary error: %s", e)

    def _enforce_recent_closes(self, days: int = 7) -> None:
        """
        Ensure any CLOSE signals from the last `days` are enforced (no lingering exposure).
        Intended for short lookbacks (e.g., 7 days) so that if a CLOSE signal wasn't fully filled
        on the next trading day, it is re-attempted — but only where no existing CLOSE order is working.
        Run host hook if present; otherwise delegate via the same guarded CSV-based path used for
        close sweeps, which honors _has_working_close_order per symbol.
        """
        if hasattr(self, "enforce_recent_closures"):
            LOG.info("Enforcing recent CLOSE signals (host) for last %d days...", days)
            try:
                self.enforce_recent_closures(days=days)
                LOG.info("Recent CLOSE enforcement (host) completed.")
            except Exception as e:
                LOG.exception("Recent CLOSE enforcement (host) failed: %s", e)
        else:
            LOG.info("Recent CLOSE enforcement host hook not implemented; using DCM CSV-based fallback.")
            # Use the guarded CSV-based delegate, which skips symbols that already have working CLOSE orders.
            try:
                self._delegate_close_from_csvs_within(days=days)
            except Exception as e:
                LOG.exception("Recent CLOSE enforcement (fallback) failed: %s", e)

        # Always summarize attempts after enforcement
        try:
            self._summarize_latest_attempts()
        except Exception as e:
            LOG.warning("Attempts summary error after recent close enforcement: %s", e)

    def _reconcile_positions_with_signals_lookback(self, days: int = 21) -> None:
        """
        Outside RTH, reconcile current positions against the *latest* signal per symbol
        by scanning only the symbols we currently hold. For each held symbol, walk the
        last `days` of CSVs (newest -> oldest) and stop at the first matching row.
        If that row's signal is OPEN (and matches orientation) -> log and skip; if CLOSE or mismatched OPEN -> submit a force-close.
        Enhanced: Detect side-specific (call/put) debit spreads for partial reconciliation.
        """
        # 1) Collect currently held option symbols and their orientation AND side info
        try:
            from ib_insync import IB
        except Exception as e:
            LOG.warning("Reconcile: ib_insync unavailable; skipping IB position check: %s", e)
            return

        ib = IB()
        try:
            ib.connect('127.0.0.1', 7497, clientId=882, timeout=6)
        except Exception as e:
            LOG.warning("Reconcile: could not connect to IB: %s", e)
            return

        held_info: dict[str, int | None] = {}
        side_info: dict[str, dict[str, bool]] = {}
        try:
            poss = ib.positions()
            legs_by_sym: dict[str, list[tuple[str, float, float, float]]] = {}
            for p in poss:
                c = p.contract
                if getattr(c, 'secType', '') != 'OPT':
                    continue
                q = float(p.position or 0.0)
                if abs(q) < 1e-9:
                    continue
                sym = (getattr(c, 'symbol', '') or '').upper()
                right = (getattr(c, 'right', '') or '').upper()   # 'C' or 'P'
                strike = float(getattr(c, 'strike', 0.0))
                avg = float(p.avgCost or 0.0)
                legs_by_sym.setdefault(sym, []).append((right, strike, q, avg))
            for sym, legs in legs_by_sym.items():
                # Default: no clear net vertical orientation
                sign: int | None = None
                has_call_vert = False
                has_put_vert = False

                if len(legs) >= 2:
                    # Split legs by right
                    calls = [(strike, qty, avg) for r, strike, qty, avg in legs if r == "C"]
                    puts  = [(strike, qty, avg) for r, strike, qty, avg in legs if r == "P"]

                    # Helper: detect ANY vertical (debit or credit) for a given side and compute a coarse net sign
                    def _detect_vertical(side_legs, is_call: bool) -> tuple[bool, int | None]:
                        """
                        Return (has_vertical, side_sign) where:
                          - has_vertical is True if we find at least one pair of strikes with opposite-signed qty.
                          - side_sign is +1 for net long vertical, -1 for net short vertical, or None if ambiguous.
                        We do NOT enforce a particular strike ordering here; debit vs credit is not important
                        for deciding which *side* (call vs put) exists and needs to be flipped.
                        """
                        if len(side_legs) < 2:
                            return False, None

                        has_vert_local = False
                        # Track simple notionals by long/short legs to infer overall orientation
                        long_notional = 0.0
                        short_notional = 0.0

                        for i in range(len(side_legs)):
                            s1, q1, avg1 = side_legs[i]
                            for j in range(len(side_legs)):
                                if i == j:
                                    continue
                                s2, q2, avg2 = side_legs[j]
                                # Require opposite-signed quantities and different strikes
                                if s1 == s2 or q1 == 0 or q2 == 0 or (q1 > 0 and q2 > 0) or (q1 < 0 and q2 < 0):
                                    continue
                                has_vert_local = True

                        # Coarse orientation: sum notional by sign across all legs of this side
                        for s, q, avg in side_legs:
                            notional = abs(q) * (avg if avg > 0 else 1.0) * 100.0
                            if q > 0:
                                long_notional += notional
                            elif q < 0:
                                short_notional += notional

                        side_sign: int | None = None
                        if long_notional > short_notional:
                            side_sign = +1
                        elif short_notional > long_notional:
                            side_sign = -1

                        return has_vert_local, side_sign

                    # Detect call and put verticals independently
                    has_call_vert, call_sign = _detect_vertical(calls, is_call=True)
                    has_put_vert, put_sign   = _detect_vertical(puts,  is_call=False)

                    # Decide overall sign based on which side dominates by notional, if any
                    if has_call_vert and not has_put_vert:
                        sign = call_sign
                    elif has_put_vert and not has_call_vert:
                        sign = put_sign
                    elif has_call_vert and has_put_vert:
                        # If both sides exist, compare total notional to see which dominates
                        call_notional = sum(abs(q) * (avg if avg > 0 else 1.0) * 100.0 for _, q, avg in calls)
                        put_notional  = sum(abs(q) * (avg if avg > 0 else 1.0) * 100.0 for _, q, avg in puts)
                        if call_notional > put_notional:
                            sign = +1
                        elif put_notional > call_notional:
                            sign = -1
                        else:
                            sign = None

                # Persist side-level information so reconcile logic can flip only the opposite side when needed.
                side_info[sym] = {
                    "call_vert": bool(has_call_vert),
                    "put_vert":  bool(has_put_vert),
                }
                held_info[sym] = sign
        except Exception as e:
            LOG.warning("Reconcile: error fetching positions: %s", e)
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

        if not held_info:
            LOG.info("Reconcile: no open option positions detected.")
            return

        # 2) Use only the single latest signal per held symbol within `days`, then compare to current orientation.
        import csv as _csv, os as _os
        from datetime import datetime as _dt

        def _parse_ts_ny_safe(s):
            try:
                s = (s or "").strip()
                if not s:
                    return None
                # common formats from listener
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
                    try:
                        return _dt.strptime(s, fmt)
                    except Exception:
                        continue
            except Exception:
                pass
            return None

        rows = DailyCycleManagementMixin._load_csv_rows_with_source(days=max(1, days)) or []
        def _daylbl_to_dt(lbl):
            try:
                return _dt.strptime((lbl or "").strip(), "%y_%m_%d")
            except Exception:
                return _dt.min

        latest_row_by_sym: dict[str, dict] = {}
        for r in rows:
            sym = (str(r.get("symbol", "")).strip().upper())
            if not sym or sym not in held_info:
                continue
            ts = _parse_ts_ny_safe(r.get("timestamp_ny"))
            day_lbl = str(r.get("_csv_src") or "").strip()
            order_key = (ts or _daylbl_to_dt(day_lbl), _daylbl_to_dt(day_lbl))
            prev = latest_row_by_sym.get(sym)
            if prev is None or order_key > prev["okey"]:
                latest_row_by_sym[sym] = {"row": r, "okey": order_key}

        looked = 0
        submitted = 0

        for sym, cur_sign in sorted(held_info.items()):
            looked += 1
            if sym not in latest_row_by_sym:
                LOG.info("Reconcile: no recent signal row for %s within %dd; leaving as-is.", sym, days)
                try:
                    _AttemptLogger.write(symbol=sym, action="hold", status="skipped",
                                         reason="no_recent_signal", exp="", right="", source="dcm-reconcile")
                except Exception:
                    pass
                continue

            r = latest_row_by_sym[sym]["row"]
            side_raw = (str(r.get("signal_type") or r.get("signal_side") or r.get("side") or "")).strip().upper()
            if not side_raw:
                try:
                    sp = r.get("strategy_position")
                    side_raw = "CLOSE" if (sp is not None and int(sp) == 0) else side_raw
                except Exception:
                    pass

            if "CLOSE" in side_raw:
                latest_is_close = True
                latest_open_sign = None
            elif "CALL" in side_raw and "OPEN" in side_raw:
                latest_is_close = False
                latest_open_sign = +1
            elif "PUT" in side_raw and "OPEN" in side_raw:
                latest_is_close = False
                latest_open_sign = -1
            else:
                LOG.info("Reconcile: unknown signal encoding for %s; skipping. row_side=%s", sym, side_raw)
                try:
                    _AttemptLogger.write(symbol=sym, action="hold", status="skipped",
                                         reason="unknown_signal_encoding", exp="", right="", source="dcm-reconcile")
                except Exception:
                    pass
                continue

            should_close = False
            reason = None
            side_to_close: str | None = None

            if latest_is_close:
                # Latest signal explicitly says "close" -> close both sides
                should_close = True
                reason = "reconcile_close_signal"
                side_to_close = None  # both
            else:
                # Flip logic: if latest OPEN is CALL_OPEN but we still have put vertical(s), close PUT side only.
                has_call_vert = bool(side_info.get(sym, {}).get("call_vert", False))
                has_put_vert  = bool(side_info.get(sym, {}).get("put_vert", False))

                if latest_open_sign == +1 and has_put_vert:
                    should_close = True
                    reason = "reconcile_flip_put_to_call"
                    side_to_close = "put"
                elif latest_open_sign == -1 and has_call_vert:
                    should_close = True
                    reason = "reconcile_flip_call_to_put"
                    side_to_close = "call"
                elif cur_sign is not None and latest_open_sign is not None and cur_sign != latest_open_sign:
                    # Net orientation mismatch (e.g., still net short puts vs latest CALL_OPEN)
                    should_close = True
                    reason = "reconcile_mismatch"
                    side_to_close = None  # both
                else:
                    LOG.info("Reconcile: latest signal for %s is OPEN and matches current orientation; holding.", sym)
                    try:
                        _AttemptLogger.write(symbol=sym, action="hold", status="skipped",
                                             reason="latest_open_matches", exp="", right="", source="dcm-reconcile")
                    except Exception:
                        pass

            if should_close:
                if not hasattr(self, "_submitted_close_syms"):
                    self._submitted_close_syms = set()
                if sym in self._submitted_close_syms or self._has_working_close_order(sym):
                    LOG.info("Reconcile: skipping CLOSE for %s (already submitted/working).", sym)
                    try:
                        _AttemptLogger.write(symbol=sym, action="close", status="skipped",
                                            reason="working_close_order", exp="", right="", source="dcm-reconcile")
                    except Exception:
                        pass
                    continue

                _prev_phase = getattr(self, "_in_close_phase", False)
                self._in_close_phase = True
                try:
                    # Use positions-based close so we can handle both long and short verticals (credits and debits).
                    LOG.info("Reconcile: positions-based CLOSE for %s side=%s reason=%s",
                            sym, (side_to_close or "both"), reason)
                    try_side = side_to_close  # "call", "put", or None
                    ok = self._try_close_from_positions(sym, prefer="LMT", side=try_side)

                    if not ok and try_side is not None:
                        # Fallback: if we couldn't close the requested side, try closing any verticals in this symbol.
                        LOG.info("Reconcile: fallback CLOSE for %s (any side) after side=%s failure.", sym, try_side)
                        ok = self._try_close_from_positions(sym, prefer="LMT", side=None)

                    if ok:
                        self._submitted_close_syms.add(sym)
                        submitted += 1
                        try:
                            _AttemptLogger.write(symbol=sym, action="close", status="placed",
                                                reason=reason, exp="", right=(try_side.upper() if try_side else ""),
                                                source="dcm-reconcile")
                        except Exception:
                            pass
                    else:
                        LOG.info("Reconcile: no verticals closed for %s in positions-based CLOSE.", sym)
                        try:
                            _AttemptLogger.write(symbol=sym, action="close", status="skipped",
                                                reason="no_vertical_in_positions", exp="",
                                                right=(try_side.upper() if try_side else ""), source="dcm-reconcile")
                        except Exception:
                            pass

                    if latest_is_close:
                        try:
                            if self._flatten_stock_if_present(sym):
                                LOG.info("Reconcile: flattened stock for %s based on latest CLOSE signal.", sym)
                        except Exception as _e_stk:
                            LOG.warning("Reconcile: stock flatten failed for %s: %s", sym, _e_stk)
                finally:
                    self._in_close_phase = _prev_phase

        LOG.info("Reconcile lookback (held-first): evaluated %d held symbol(s); submitted %d CLOSE order(s).", looked, submitted)

    def _rth_risk_exits(self, days_old: int = 2, loss_frac: float = 0.5, gain_frac: float = 0.5) -> None:
        """
        During Regular Trading Hours, scan currently-held vertical debit spreads that are older than `days_old`
        and close them with a limit order if either:
          * Loss >= loss_frac (e.g., 50%) of entry debit, or
          * Profit >= gain_frac (e.g., 50%) of potential profit (width - entry debit).

        We infer entry price from OPEN executions per leg and compute current net using mid quotes per leg.
        If thresholds are met, we delegate the actual close placement to PlaceAnOrder (--mode force-close).
        """
        try:
            from ib_insync import IB, Contract, Ticker, util, ExecutionFilter
        except Exception as e:
            LOG.warning("Risk exits: ib_insync unavailable: %s", e)
            return

        NY_TZ = ZoneInfo("America/New_York")
        now = self._now_ny()

        ib = IB()
        try:
            ib.connect('127.0.0.1', 7497, clientId=878, timeout=6)
        except Exception as e:
            LOG.warning("Risk exits: could not connect to IB: %s", e)
            return

        def _mid(t: Ticker) -> float | None:
            # Handle worthless options: if both bid and ask are 0, treat as 0 value
            if t.bid is not None and t.ask is not None:
                if t.bid == 0 and t.ask == 0:
                    return 0.0  # Worthless option
                if t.ask > 0 and t.bid >= 0:
                    return (t.bid + t.ask) / 2
            return t.last if t.last is not None else None

        # Build current positions per symbol, split by rights and strikes to detect vertical debits
        try:
            poss = ib.positions()
        except Exception as e:
            LOG.warning("Risk exits: positions() error: %s", e)
            try: ib.disconnect()
            except: pass
            return

        # Collect OPT legs per symbol with their contract ids, avgCost and qty
        legs_by_sym: dict[str, list[dict]] = {}
        for p in poss:
            c = p.contract
            if getattr(c, "secType", "") != "OPT":
                continue
            q = float(p.position or 0.0)
            if abs(q) < 1e-9:
                continue
            legs_by_sym.setdefault(c.symbol.upper(), []).append({
                "conId": c.conId,
                "right": getattr(c, "right", "").upper(),
                "strike": float(getattr(c, "strike", 0.0)),
                "avgCost": float(p.avgCost or 0.0),
                "qty": q,
                "contract": c
            })

        if not legs_by_sym:
            LOG.info("Risk exits: no open option legs to evaluate.")
            try: ib.disconnect()
            except: pass
            return

        # Pull executions for last 30 days to infer OPEN entry prices and age
        since = (now.astimezone(ZoneInfo("UTC")) - timedelta(days=30)).strftime("%Y%m%d-%H:%M:%S")
        try:
            fills = ib.reqExecutions(ExecutionFilter(time=since)) or []
        except Exception as e:
            LOG.warning("Risk exits: reqExecutions error: %s", e)
            fills = []

        # Build exec info by conId for OPEN legs
        open_execs: dict[int, list[dict]] = {}
        for f in fills:
            c = f.contract
            e = f.execution
            if getattr(c, "secType", "") != "OPT":
                continue
            if getattr(e, "openClose", "") != "O":
                continue
            try:
                t_utc = util.parseIBDatetime(getattr(e, "time", ""))
            except Exception:
                t_utc = None
            open_execs.setdefault(c.conId, []).append({
                "side": e.side,    # BUY/SELL
                "shares": float(getattr(e, "shares", 0.0) or 0.0),
                "price": float(getattr(e, "price", 0.0) or 0.0),
                "t": t_utc
            })

        def _avg_open_price(conId: int) -> tuple[float | None, datetime | None]:
            rows = open_execs.get(conId, [])
            if not rows:
                return None, None
            # volume-weighted average open price and earliest time
            tot_px = 0.0
            tot_sh = 0.0
            t0 = None
            for r in rows:
                sh = abs(r["shares"])
                tot_sh += sh
                tot_px += r["price"] * sh
                if r["t"] and (t0 is None or r["t"] < t0):
                    t0 = r["t"]
            return ((tot_px / tot_sh) if tot_sh else None), (t0.astimezone(NY_TZ) if t0 else None)

        submitted = 0
        checked = 0

        for sym, legs in sorted(legs_by_sym.items()):
            # look for a vertical debit (CALL debit or PUT debit) with 2+ legs
            calls = [(l["strike"], l) for l in legs if l["right"] == "C"]
            puts  = [(l["strike"], l) for l in legs if l["right"] == "P"]

            def _process_vertical(strike_low: float, long_leg: dict, strike_high: float, short_leg: dict, right: str):
                nonlocal submitted, checked
                checked += 1

                # Determine age from earliest OPEN exec across the two legs
                long_avg, long_t0 = _avg_open_price(long_leg["conId"])
                short_avg, short_t0 = _avg_open_price(short_leg["conId"])
                t0 = min([t for t in [long_t0, short_t0] if t is not None], default=None)
                if t0 is None or (now - t0) < timedelta(days=days_old):
                    return  # ignore if too new or no exec time

                # Entry net (debit) = long_avg - short_avg
                if long_avg is None or short_avg is None:
                    return
                entry = max(0.0, (long_avg - short_avg))

                # Current net using mid prices
                try:
                    # fully qualify contracts to request market data
                    long_c = Contract(conId=long_leg["conId"])
                    short_c = Contract(conId=short_leg["conId"])
                    # reqContractDetails to expand (safer for MD)
                    lcd = ib.reqContractDetails(long_c)
                    scd = ib.reqContractDetails(short_c)
                    if not lcd or not scd:
                        return
                    long_c = lcd[0].contract
                    short_c = scd[0].contract
                    tl = ib.reqMktData(long_c, snapshot=False)
                    ts = ib.reqMktData(short_c, snapshot=False)
                    # brief poll
                    for _ in range(8):
                        ib.sleep(0.2)
                        if _mid(tl) is not None and _mid(ts) is not None:
                            break
                    curr = None
                    ml = _mid(tl)
                    ms = _mid(ts)
                    if ml is not None and ms is not None:
                        curr = max(0.0, ml - ms)
                    if curr is None:
                        LOG.debug("Risk exits: skipping %s %s - no valid market data (ml=%s, ms=%s)", sym, right, ml, ms)
                        return
                except Exception as e:
                    LOG.warning("Risk exits: MD error for %s %s strikes(%s,%s): %s", sym, right, strike_low, strike_high, e)
                    return

                width = abs(strike_high - strike_low)
                # stop-loss: current <= (1 - loss_frac) * entry
                stop_hit = curr <= (1.0 - loss_frac) * entry
                # take-profit: current >= entry + gain_frac * (width - entry)
                tp_hit = curr >= entry + gain_frac * max(0.0, (width - entry))

                if not (stop_hit or tp_hit):
                    return

                # Build the same human-readable reason used in DailyCycle.log
                reason = "STOP(>=%.0f%% loss)" % (loss_frac*100) if stop_hit else "TP(>=%.0f%% max profit)" % (gain_frac*100)

                # Also append to DCM attempts CSV so we can see that this CLOSE is TP/SL-driven
                try:
                    _AttemptLogger.write(
                        symbol=sym,
                        action="close",
                        status="queued",
                        reason=f"rth_risk_exit:{reason}",
                        exp="",                    # we don't strictly need exp here
                        right=(right[0].upper() if right else ""),
                        atm=strike_low,
                        oth=strike_high,
                        source="dcm-risk-exit",
                    )
                except Exception:
                    # Best-effort; don't block risk exits if attempts logging fails
                    pass

                # Delegate to PlaceAnOrder to place a LIMIT CLOSE using join pricing
                # The 3pm preclose cycle will convert to market if unfilled
                # The fix in commit 174d769 ensures we won't cancel existing limit orders when placing limit orders
                try:
                    self._run_place_an_order([
                        "--mode", "force-close",
                        "--symbols", sym,
                        "--quantity","50",
                        "--use-live-close", "join",  # Use join pricing for limit orders instead of market
                        "--quiet"
                    ])
                    submitted += 1
                    LOG.info(
                        "Risk exits: submitted LIMIT CLOSE (join) for %s %s vertical %s/%s (age %dd) entry=%.2f curr=%.2f width=%.2f reason=%s",
                        sym, right, strike_low, strike_high, (now - t0).days, entry, curr, width, reason
                    )
                except Exception as e:
                    LOG.warning("Risk exits: failed to submit CLOSE for %s: %s", sym, e)

            # Check for call debit: +qty at lower strike, -qty at higher strike
            if len(calls) >= 2:
                # find any pair that looks like + at lower and - at higher
                for s1, l1 in calls:
                    for s2, l2 in calls:
                        if s1 >= s2: 
                            continue
                        if l1["qty"] > 0 and l2["qty"] < 0:
                            _process_vertical(s1, l1, s2, l2, "CALL")

            # Check for put debit: +qty at higher strike, -qty at lower strike
            if len(puts) >= 2:
                for s1, l1 in puts:
                    for s2, l2 in puts:
                        if s1 <= s2:
                            continue
                        if l1["qty"] > 0 and l2["qty"] < 0:
                            _process_vertical(s2, l2, s1, l1, "PUT")

        LOG.info("Risk exits: evaluated %d candidate vertical(s); submitted %d CLOSE order(s).", checked, submitted)
        try:
            ib.disconnect()
        except Exception:
            pass

    def _rth_liquidity_cleanup(self) -> None:
        """
        During Regular Trading Hours, remove/cancel open unfilled vertical combo orders where NEITHER leg has
        live open interest (from IB market data) greater than MIN_OI_FOR_RTH. This uses generic tick 101
        (OPTION_OPEN_INTEREST) to fetch current OI for each leg.
        """
        # Ensure today's and previous trading day's CSVs have fresh OI before any OI-based cleanup logic
        try:
            self._enrich_today_and_prev_trading_day(only_rth=True)
        except Exception:
            LOG.warning("RTH cleanup: failed to run LiquidityFilter pre-hook (today+prev); continuing with live OI checks.")
        try:
            from ib_insync import IB, Contract, ContractDetails, Ticker
        except Exception as e:
            LOG.warning("ib_insync unavailable for RTH liquidity cleanup: %s", e)
            return

        # Connect to IB and inspect open trades
        ib = IB()
        try:
            ib.connect('127.0.0.1', 7497, clientId=879, timeout=6)
        except Exception as e:
            LOG.warning("RTH cleanup: could not connect to IB: %s", e)
            return

        # Helper: resolve an option contract from leg conId
        def _resolve_opt(conId: int):
            try:
                cds = ib.reqContractDetails(Contract(conId=conId))
                oc = cds[0].contract if cds else None
                return oc if oc and getattr(oc, 'secType', '') == 'OPT' else None
            except Exception:
                return None

        # Helper: fetch live open interest for a given option contract (generic tick 101)
        def _live_oi(opt_contract: Contract) -> int | None:
            try:
                tkr: Ticker = ib.reqMktData(opt_contract, genericTickList='101', snapshot=False, regulatorySnapshot=False)
                # give the feed a brief moment to populate; then poll a couple of times
                for _ in range(6):
                    ib.sleep(0.2)
                    if getattr(tkr, 'optionOpenInterest', None) is not None:
                        return int(tkr.optionOpenInterest)
                return None
            except Exception:
                return None

        try:
            trades = ib.openTrades()
            if not trades:
                LOG.info("RTH cleanup: no open trades to evaluate.")
                return

            cancelled = 0
            for tr in trades:
                c = tr.contract
                s = tr.orderStatus
                o = tr.order

                # Only consider active, unfilled COMBO (BAG) orders
                if getattr(c, 'secType', '') != 'BAG':
                    continue
                status = (getattr(s, 'status', '') or '').lower()
                if status in ('filled', 'cancelled', 'apicancelled'):
                    continue

                legs = getattr(c, 'comboLegs', None) or []
                if len(legs) < 2:
                    continue

                # Resolve legs -> option contracts
                leg_opts = []
                for leg in legs:
                    conId = getattr(leg, 'conId', None)
                    if not conId:
                        leg_opts = []
                        break
                    oc = _resolve_opt(conId)
                    if not oc:
                        leg_opts = []
                        break
                    leg_opts.append(oc)
                if len(leg_opts) < 2:
                    continue

                # Assume vertical: common sym/exp/right and >=2 distinct strikes
                syms   = { oc.symbol for oc in leg_opts }
                exps   = { getattr(oc, 'lastTradeDateOrContractMonth', '') for oc in leg_opts }
                rights = { getattr(oc, 'right', '') for oc in leg_opts }
                strikes= sorted({ float(getattr(oc, 'strike', 0.0)) for oc in leg_opts })
                if len(syms) != 1 or len(exps) != 1 or len(rights) != 1 or len(strikes) < 2:
                    continue
                sym = next(iter(syms)); exp = next(iter(exps)); right = next(iter(rights))

                # Pull live OI for each leg
                oi_values = []
                for oc in leg_opts:
                    oi = _live_oi(oc)
                    oi_values.append(oi if oi is not None else -1)

                # If BOTH legs have OI <= threshold (or unknown), cancel
                leg1_ok = oi_values[0] is not None and oi_values[0] > MIN_OI_FOR_RTH
                leg2_ok = oi_values[1] is not None and oi_values[1] > MIN_OI_FOR_RTH
                if not (leg1_ok or leg2_ok):
                    try:
                        ib.cancelOrder(o)
                        cancelled += 1
                        lmt = getattr(o, 'lmtPrice', None)
                        net = ("MKT" if (getattr(o, 'orderType', '').upper() == 'MKT') else (f"LMT {lmt:.2f}" if lmt not in (None, 0) else "-"))
                        LOG.info("RTH cleanup: cancelled low-OI order %s %s %s strikes=%s OI=%s (threshold>%d) spread=%s",
                                 sym, exp, right, strikes, oi_values, MIN_OI_FOR_RTH, net)
                    except Exception as e:
                        LOG.warning("RTH cleanup: failed to cancel order for %s %s %s: %s", sym, exp, right, e)
            LOG.info("RTH cleanup completed. Cancelled %d low-liquidity open order(s).", cancelled)
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    def _cancel_low_oi_working_orders_from_csv(self, threshold: int = MIN_OI_FOR_RTH, lookback_days: int = 2) -> None:
        """
        9:35am RTH guard: cancel *working* combo orders (BAG) where BOTH legs have OI < threshold
        using the combined_listener_spreads.csv from today, with automatic fallback to the prior day.
        This avoids keeping thin orders intraday while still allowing after-hours placement without
        an OI guard. We only cancel when we can positively read OI for both legs from the CSV.
        Preference order for OI data: today first, then yesterday (… up to `lookback_days`).
        """
        # Populate OI in combined CSVs first (today and previous trading day)
        try:
            self._enrich_today_and_prev_trading_day(only_rth=True)
        except Exception:
            LOG.warning("CSV OI cancel: failed to run LiquidityFilter pre-hook (today+prev); CSV may lack OI columns.")

        # Import IB dependencies
        try:
            from ib_insync import IB, Contract
        except Exception as e:
            LOG.warning("CSV OI cancel: ib_insync unavailable: %s", e)
            return

        # Load rows from today (preferred) and prior day(s)
        rows = DailyCycleManagementMixin._load_csv_rows_with_source(days=max(1, lookback_days))
        if not rows:
            LOG.info("CSV OI cancel: no combined CSV rows available (today/prior-day missing); skipping.")
            return

        def _coerce_float(v):
            try:
                if v is None:
                    return None
                s = str(v).strip()
                if not s:
                    return None
                return float(s)
            except Exception:
                return None

        def _find_csv_oi(symbol: str, right: str, exp: str, k_atm: float, k_oth: float) -> tuple[int | None, int | None, str | None]:
            """
            Scan rows newest->older and return (oi_atm, oi_oth, src_label) on first confident match,
            else (None, None, None).
            """
            sym_u = (symbol or "").strip().upper()
            r_u = (right or "").strip().upper()

            cand_keys = {
                "symbol": ("symbol",),
                "right": ("right", "signal_right"),
                "exp": ("expiration", "exp", "lastTradeDateOrContractMonth"),
                "atm_strike": ("atm", "k_atm", "strike_atm", "s_atm", "low_strike", "lower_strike"),
                "oth_strike": ("oth", "k_oth", "strike_oth", "s_oth", "high_strike", "upper_strike"),
                "oi_atm": ("oi_atm", "atm_oi", "oi_call_atm", "oi_put_atm", "open_interest_atm", "oi1"),
                "oi_oth": ("oi_oth", "oth_oi", "oi_call_oth", "oi_put_oth", "open_interest_oth", "oi2"),
            }

            def _get(row, keys):
                for k in keys:
                    if k in row:
                        return row[k]
                return None

            def _same_strike(a, b):
                try:
                    return abs(float(a) - float(b)) < 1e-9
                except Exception:
                    return False

            for row in rows:  # rows already ordered newest->older
                rsym = (_get(row, cand_keys["symbol"]) or "").strip().upper()
                if rsym != sym_u:
                    continue
                rr = (_get(row, cand_keys["right"]) or "").strip().upper()
                if rr and rr != r_u:
                    continue
                rexp = (_get(row, cand_keys["exp"]) or "").strip()
                if rexp and exp and rexp != exp:
                    continue
                ra = _get(row, cand_keys["atm_strike"])
                ro = _get(row, cand_keys["oth_strike"])
                if ra is None or ro is None:
                    continue
                if not (_same_strike(ra, k_atm) and _same_strike(ro, k_oth)):
                    continue

                oi_a = _coerce_float(_get(row, cand_keys["oi_atm"]))
                oi_o = _coerce_float(_get(row, cand_keys["oi_oth"]))
                if oi_a is None or oi_o is None:
                    # Not definitive; keep searching older rows
                    continue
                try:
                    return (int(oi_a), int(oi_o), row.get("_csv_src"))
                except Exception:
                    return (None, None, None)

            return (None, None, None)

        ib = IB()
        try:
            ib.connect('127.0.0.1', 7497, clientId=887, timeout=6)
        except Exception as e:
            LOG.warning("CSV OI cancel: could not connect to IB: %s", e)
            return

        cancelled = 0
        try:
            trades = ib.openTrades() or []
            if not trades:
                LOG.info("CSV OI cancel: no open trades.")
                return

            for tr in trades:
                c = getattr(tr, "contract", None)
                s = getattr(tr, "orderStatus", None)
                o = getattr(tr, "order", None)
                if not c or not s or not o:
                    continue

                # Skip CLOSE orders (action=SELL) - let them fill naturally or get converted at 3pm preclose
                # CLOSE orders have lower liquidity risk since they're exiting positions
                # They will be handled by the 3pm preclose market conversion if still unfilled
                order_action = (getattr(o, "action", "") or "").upper()
                if order_action == "SELL":
                    continue

                if getattr(c, "secType", "") != "BAG":
                    continue
                st = (getattr(s, "status", "") or "").lower()
                if st in ("filled", "cancelled", "apicancelled"):
                    continue
                legs = getattr(c, "comboLegs", None) or []
                if len(legs) < 2:
                    continue

                # Resolve the two option legs to obtain symbol/right/exp/strike
                leg_opts = []
                try:
                    for leg in legs[:2]:
                        cds = ib.reqContractDetails(Contract(conId=getattr(leg, "conId", 0)))
                        oc = cds[0].contract if cds else None
                        if not oc or getattr(oc, "secType", "") != "OPT":
                            leg_opts = []
                            break
                        leg_opts.append(oc)
                except Exception:
                    leg_opts = []
                if len(leg_opts) < 2:
                    continue

                sym = leg_opts[0].symbol
                exp = getattr(leg_opts[0], "lastTradeDateOrContractMonth", "")
                right = getattr(leg_opts[0], "right", "")

                k1 = float(getattr(leg_opts[0], "strike", 0.0))
                k2 = float(getattr(leg_opts[1], "strike", 0.0))

                # Normalize to (atm, oth) following vertical convention:
                # CALL: atm=min, oth=max; PUT: atm=max, oth=min
                if (right or "").upper() == "P":
                    atm, oth = (max(k1, k2), min(k1, k2))
                else:
                    atm, oth = (min(k1, k2), max(k1, k2))

                oi_atm, oi_oth, src = _find_csv_oi(sym, right, exp, atm, oth)
                if oi_atm is None or oi_oth is None:
                    # no CSV OI — do not cancel
                    continue

                if oi_atm < threshold and oi_oth < threshold:
                    try:
                        ib.cancelOrder(o)
                        cancelled += 1
                        LOG.info("CSV OI cancel [%s]: cancelled %s %s %s atm/oth=%s/%s OI=%s/%s<thr(%d)",
                                 (src or "unknown"), sym, exp, right, atm, oth, oi_atm, oi_oth, threshold)
                    except Exception as e:
                        LOG.warning("CSV OI cancel: cancel failed for %s %s %s: %s", sym, exp, right, e)

            LOG.info("CSV OI cancel completed. Cancelled %d low-OI working order(s).", cancelled)
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass
    def run_rth_open_cleanup(self, lookback_days: int = 2) -> None:
        """
        Call this once shortly after the market opens (~09:35 ET).
        Ensures OI is enriched for today and previous trading day, then cancels low-OI working orders
        using both CSV-based and live-OI checks.
        The `lookback_days` parameter controls how many recent combined_listener_spreads.csv files are scanned when canceling low‑OI working orders from CSV (default: 2).
        """
        now = self._now_ny()
        if not self._is_trading_day(now):
            LOG.info("09:35 cleanup skipped: not a trading day.")
            return
        t = now.time()
        if t < time(9, 35):
            LOG.info("09:35 cleanup skipped: current time %s is before 09:35 ET.", t.strftime("%H:%M"))
            return
        # Enrich and cleanup
        self._enrich_today_and_prev_trading_day(only_rth=True)
        self._cancel_low_oi_working_orders_from_csv(threshold=MIN_OI_FOR_RTH, lookback_days=lookback_days)
        self._rth_liquidity_cleanup()
        try:
            self._summarize_latest_attempts()
        except Exception as e:
            LOG.warning("Attempts summary failed after 09:35 cleanup: %s", e)

    # ---------- Orchestration ----------
    def daily_trading_cycle(self) -> None:
        """Execute daily trading cycle with guards and logging."""
        now = self._now_ny()

        # Optional diagnostic: force an immediate from-signal OPEN placement using join limits
        # if DCM_DIAG_OPEN=1 is set in the environment (useful for interactive/testing sessions).
        if (os.environ.get("DCM_DIAG_OPEN") or "").strip() == "1":
            try:
                self._diagnostic_open_from_signal(method="join", min_limit=0.05, bump_to_min=True)
            except Exception as e:
                LOG.warning("Diagnostic open-from-signal step skipped due to error: %s", e)

        # Cycle A: Pre-close sweep (convert lingering CLOSE limits to market)
        if self.is_pre_close_window(now):
            LOG.info("Pre-close window detected (%s); running market-conversion sweep.", now)
            self._pre_close_market_conversion()
            return

        # Cycle B: After market close analysis
        if self.is_after_market_close(now):
            if not self._can_run_daily_analysis():
                LOG.info("Daily analysis recently executed; skipping to respect cooldown.")
                # Still allow after-hours placement window later
            else:
                LOG.info("Starting daily analysis cycle...")
                try:
                    # Optional hook
                    if hasattr(self, "pre_daily_analysis"):
                        self.pre_daily_analysis()

                    # 1. Scan for new candidates
                    sectors = ['TECH', 'FINANCE', 'HEALTHCARE', 'ENERGY', 'CONSUMER']
                    self._safe_host_call("scan_sector_candidates", sectors)

                    # 2. Filter by liquidity and IV
                    filtered_candidates = self._safe_host_call("filter_candidates_by_criteria") or []
                    LOG.info("Filtered candidates: %s", len(filtered_candidates) if filtered_candidates else 0)

                    # 3. Collect and organize historical data
                    self._safe_host_call("update_historical_data", filtered_candidates)
                    sector_data = self._safe_host_call("organize_sector_data") or {}
                    if not sector_data:
                        self._warn_missing_sector_data("Daily analysis")

                    # 4. Optimize strategies per sector
                    optimized_params = self._safe_host_call("optimize_sector_strategy", sector_data)
                    if optimized_params is None:
                        self._warn_missing_sector_data("Optimize strategies")

                    # 5. Select top 5 performers per sector
                    top_candidates = self._safe_host_call("select_top_performers", sector_data, 5) or []

                    # 6. Generate signals
                    signals = self._safe_host_call("generate_trade_signals", top_candidates) or []

                    # 7. Prepare orders for next day
                    self._safe_host_call("prepare_next_day_orders", signals)

                    # Optional hook
                    if hasattr(self, "post_daily_analysis"):
                        self.post_daily_analysis(signals=signals, top_candidates=top_candidates)

                    self._mark_daily_analysis()
                    LOG.info("Daily analysis cycle completed.")
                except Exception as e:
                    LOG.exception("Daily analysis cycle failed: %s", e)
                    # Decide: alert, retry, or mark degraded state
                    return

            # New: outside RTH reconciliation against latest CLOSE signals (21-day lookback)
            try:
                self._reconcile_positions_with_signals_lookback(days=21)
            except Exception as e:
                LOG.warning("Reconcile step skipped due to error: %s", e)

            # Optional: After-hours batch placement + recent closes enforcement
            if self.is_after_hours_placement(now):
                LOG.info("After-hours placement window (%s): enforcing recent closes + placing from-signal.", now)
                self._enforce_recent_closes(days=7)
                self._after_hours_batch_placement()
                try:
                    self._diagnostic_open_from_signal(method="join", min_limit=0.05, bump_to_min=True)
                except Exception as e:
                    LOG.warning("Diagnostic open-from-signal (after-hours) failed: %s", e)
            else:
                LOG.info("Not yet in after-hours placement window at %s; skipping placement.", now)
            return

        # Cycle C: Market open execution
        if self.is_market_open(now):
            LOG.info("Market open cycle starting...")
            # 9:35 RTH guard: cancel working orders where both legs have low OI per today's/yesterday's CSV
            try:
                tnow = now.time()
                if tnow >= time(9, 35):
                    self._cancel_low_oi_working_orders_from_csv(threshold=MIN_OI_FOR_RTH, lookback_days=2)
            except Exception as e:
                LOG.warning("CSV OI cancel step skipped due to error: %s", e)
            # New: cancel open unfilled orders if neither leg has OI > threshold (based on today's CSV)
            try:
                self._rth_liquidity_cleanup()
            except Exception as e:
                LOG.warning("RTH liquidity cleanup skipped due to error: %s", e)
            # New: take-profit / stop-loss exits for older positions (RTH only)
            try:
                self._rth_risk_exits(days_old=2, loss_frac=0.5, gain_frac=0.5)
            except Exception as e:
                LOG.warning("Risk exits skipped due to error: %s", e)
            try:
                if hasattr(self, "pre_market_open"):
                    self.pre_market_open()

                # Execute prepared orders
                self._safe_host_call("execute_pending_orders")

                # Manage existing positions
                self._safe_host_call("manage_existing_positions")

                # Handle failed orders
                self._safe_host_call("retry_failed_orders")

                if hasattr(self, "post_market_open"):
                    self.post_market_open()
                LOG.info("Market open cycle completed.")
            except Exception as e:
                LOG.exception("Market open cycle failed: %s", e)
                return
        else:
            # Outside RTH but not yet past MARKET_CLOSE (e.g., weekends before 17:00).
            LOG.info("Outside RTH and not after close; running reconcile lookback at %s", now)
            try:
                self._reconcile_positions_with_signals_lookback(days=21)
            except Exception as e:
                LOG.warning("Reconcile step skipped due to error: %s", e)
            # Optional weekend diagnostic if DCM_DIAG_OPEN=1: try placing from-signal with join quotes
            if (os.environ.get("DCM_DIAG_OPEN") or "").strip() == "1":
                try:
                    self._diagnostic_open_from_signal(method="join", min_limit=0.05, bump_to_min=True)
                except Exception as e:
                    LOG.warning("Diagnostic open-from-signal (weekend) failed: %s", e)
            try:
                self._summarize_latest_attempts()
            except Exception:
                pass

    def weekly_maintenance(self) -> None:
        """Weekly strategy maintenance with guard to run once per Sunday."""
        now = self._now_ny()
        if now.weekday() != WEEKLY_MAINTENANCE_DAY:
            return

        if not self._can_run_weekly_maintenance():
            LOG.info("Weekly maintenance already executed today; skipping.")
            return

        LOG.info("Running weekly maintenance...")
        try:
            if hasattr(self, "pre_weekly_maintenance"):
                self.pre_weekly_maintenance()

            # Update historical data
            self._safe_host_call("update_all_historical_data")

            # Re-optimise strategies
            reopt = self._safe_host_call("reoptimize_all_sectors")
            if reopt is None:
                self._warn_missing_sector_data("Weekly re-optimise")

            # Remove high IV candidates
            self._safe_host_call("remove_high_iv_candidates")

            # Rebalance sector allocations
            self._safe_host_call("rebalance_sector_exposure")

            if hasattr(self, "post_weekly_maintenance"):
                self.post_weekly_maintenance()

            self._mark_weekly_maintenance()
            LOG.info("Weekly maintenance completed.")
        except Exception as e:
            LOG.exception("Weekly maintenance failed: %s", e)
            return


# ----- Runnable entry point for scheduled after-hours placement -----
if __name__ == "__main__":
    import os, sys, logging
    from pathlib import Path
    # Configure logging to console and a persistent log on Windows
    log_dir = Path(r"C:\OptionsHistory\logs") if sys.platform.startswith("win") else Path("./logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    log_path = log_dir / "DailyCycle.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_path), mode="a", encoding="utf-8")
        ]
    )

    class _Runner(DailyCycleManagementMixin):
        """
        Force daily analysis to be eligible, but otherwise use the normal session logic.
        On weekends or outside RTH this will naturally take the after-close path (including reconcile).
        """
        def __init__(self):
            # Reset the active attempts log path on each run to avoid stale logs
            _AttemptLogger._active_path = None
            super().__init__()
        def _can_run_daily_analysis(self) -> bool:  # always allow analysis eligibility
            return True

    LOG.info("DailyCycleManagement runner starting (analysis enabled; normal session logic)...")
    try:
        r = _Runner()
        # Reset the active attempts log path before each run to avoid stale logs
        _AttemptLogger._active_path = None
        r.daily_trading_cycle()
        LOG.info("DailyCycleManagement runner completed.")
        sys.exit(0)
    except Exception:
        LOG.exception("DailyCycleManagement runner failed.")
        sys.exit(1)
if __name__ == "__main__":
    """
    Minimal CLI so this module can be run directly (the PS menu calls this file).
    Safe by default; actions are opt-in via flags.
    """
    import argparse, os, sys, logging

    parser = argparse.ArgumentParser(description="DailyCycleManagement runner")
    parser.add_argument("--place-opens", action="store_true",
                        help="Place OPEN orders from the most recent CSVs (today & yesterday).")
    parser.add_argument("--reconcile", action="store_true",
                        help="(Outside RTH) Reconcile held positions vs latest signals (close/flip).")
    parser.add_argument("--preclose", action="store_true",
                        help="Pre-close sweep (≈15:00 ET): convert CLOSE LMTs & mismatches.")
    parser.add_argument("--after-hours", action="store_true",
                        help="After-hours batch: delegate opens and enforce CLOSE signals.")
    parser.add_argument("--enforce-closes", type=int, default=0,
                        help="If >0: enforce recent CLOSE signals for last N days (fallback path).")
    parser.add_argument("--verbose", action="store_true", help="Enable INFO logging.")
    args = parser.parse_args()

    # Basic logger
    logging.basicConfig(level=(logging.INFO if args.verbose else logging.WARNING),
                        format="%(asctime)s [%(levelname)s] %(message)s")

    class _Host(DailyCycleManagementMixin):
        pass

    host = _Host()

    try:
        if args.place_opens:
            # Today & yesterday, DTE ≥ 20; delegated to PlaceAnOrder (robust pricing/idempotency)
            host._delegate_open_from_recent_csvs(min_dte=20, last_n_csvs=1)

        if args.preclose:
            # Time guard: only allow preclose during 14:55-15:10 ET
            now_ny = host._now_ny()
            hh, mm = now_ny.hour, now_ny.minute
            in_preclose_window = (hh == 14 and mm >= 55) or (hh == 15 and mm <= 10)
            if not in_preclose_window:
                LOG.error(f"--preclose blocked: current time {now_ny.strftime('%H:%M')} ET is outside 14:55-15:10 window")
                sys.exit(1)
            host._pre_close_market_conversion()

        if args.after_hours:
            host._after_hours_batch_placement()

        if args.reconcile:
            host._reconcile_positions_with_signals_lookback(days=21)

        if args.enforce_closes and args.enforce_closes > 0:
            host._enforce_recent_closes(days=args.enforce_closes)

        # Always summarize at the end
        host._summarize_latest_attempts()
    except Exception as e:
        LOG.exception("DailyCycleManagement top-level error: %s", e)
        sys.exit(2)