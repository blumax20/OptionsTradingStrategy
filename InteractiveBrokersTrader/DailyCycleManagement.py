import subprocess, sys
from pathlib import Path
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import logging
import csv, os, uuid

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
        exists = os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=hdr)
            if not exists:
                w.writeheader()
            w.writerow(row)

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
# Try to use repo-local venv on Windows; otherwise fall back to current interpreter
VENV_PY_WIN = Path(__file__).parents[1] / ".venv" / "Scripts" / "python.exe"

class DailyCycleManagementMixin:

    def _try_close_from_positions(self, sym: str, prefer: str = "MKT") -> bool:
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
        """
        try:
            from ib_insync import IB, Contract, ComboLeg, ContractDetails, MarketOrder, LimitOrder
        except Exception as e:
            LOG.warning("direct-close: ib_insync unavailable: %s", e)
            return False

        ib = IB()
        try:
            ib.connect('127.0.0.1', 7497, clientId=884, timeout=6)
        except Exception as e:
            LOG.warning("direct-close: connect failed: %s", e)
            return False

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
                    order = MarketOrder(action, 1) if (prefer or 'MKT').upper() == 'MKT' else LimitOrder(action, 1, 0.01)
                    tr = ib.placeOrder(bag, order)
                    LOG.info("direct-close: %s %s vertical %s/%s -> %s %s", up, right, low, high, action, (prefer or 'MKT').upper())
                    return True
                except Exception as e:
                    LOG.warning("direct-close: combo place failed for %s %s %s/%s: %s", up, right, low, high, e)
                    return False

            # Try to detect a vertical debit (long) or short vertical for CALLs
            calls = sorted([l for l in opt_legs if l['right'] == 'C'], key=lambda x: x['strike'])
            puts  = sorted([l for l in opt_legs if l['right'] == 'P'], key=lambda x: x['strike'])

            # Detect CALL vertical: long + at lower strike and short - at higher strike
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
            if not submitted:
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

            # If no vertical combo placed, flatten orphan option legs
            if not submitted and opt_legs:
                for leg in opt_legs:
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

    def _has_working_close_order(self, sym: str) -> bool:
        """
        Return True if there is an existing working CLOSE order (SELL combo) for the given symbol.
        This prevents DCM from submitting duplicate CLOSE orders for the same symbol during a run.
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
            up = sym.upper()
            for tr in trades:
                c = getattr(tr, "contract", None)
                o = getattr(tr, "order", None)
                s = getattr(tr, "orderStatus", None)
                if not c or not o or not s:
                    continue
                if (getattr(c, "secType", "") == "BAG"
                    and (getattr(c, "symbol", "") or "").upper() == up
                    and (getattr(o, "action", "") or "").upper() == "SELL"):
                    st = (getattr(s, "status", "") or "").lower()
                    if st not in ("filled", "cancelled", "apicancelled"):
                        return True
            return False
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
        Convert any stubborn CLOSE limit orders to market in the pre-close window.
        First call the host hook if present, then run a safe force-close sweep via PlaceAnOrder.
        """
        if hasattr(self, "convert_unfilled_close_limits_to_market"):
            LOG.info("Pre-close sweep: converting unfilled CLOSE limits to market (host hook)...")
            try:
                self.convert_unfilled_close_limits_to_market(cutoff=PRE_CLOSE_SWEEP)
                LOG.info("Pre-close sweep (host) completed.")
            except Exception as e:
                LOG.exception("Pre-close sweep (host) failed: %s", e)
        else:
            LOG.info("Pre-close sweep host hook not implemented; using fallback via PlaceAnOrder.")

        # Fallback / reinforcement: force-close anything we can map; allow market fallback in PlaceAnOrder.
        # Keep min-limit tiny to avoid blocking; tolerate strike drift via --close-tol.
        self._run_place_an_order([
            "--mode", "force-close",
            "--force-close-side", "both",
            "--min-limit", "0.01",
            "--close-tol", "2.0",
            "--use-live-close", "join"
        ])
        self._summarize_latest_attempts()

    def _after_hours_batch_placement(self) -> None:
        """
        Place end-of-day signals (e.g., from listener CSV) after-hours.
        Run host hook if present, then call PlaceAnOrder to place from-signal orders.
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

        # Always follow with an explicit from-signal run so settings in CSV are honored uniformly.
        LOG.info("After-hours batch placement via PlaceAnOrder (from-signal)...")
        self._run_place_an_order([
            "--mode", "from-signal",
            "--min-limit", "0.05",
            "--bump-to-min",
            "--use-live-open", "join",
            "--quiet"
        ])
        self._summarize_latest_attempts()
    def _diagnostic_open_from_signal(self, method: str = "join", min_limit: float = 0.05, bump_to_min: bool = True) -> None:
        """
        Diagnostic helper to try (re)placing today's OPEN orders directly from the CSV using PlaceAnOrder,
        with a controllable live-limit method ('join' or 'mid'). After the run, summarize latest attempts.
        """
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
        Run host hook if present, then run PlaceAnOrder to force-close based on recent signals.
        """
        if hasattr(self, "enforce_recent_closures"):
            LOG.info("Enforcing recent CLOSE signals (host) for last %d days...", days)
            try:
                self.enforce_recent_closures(days=days)
                LOG.info("Recent CLOSE enforcement (host) completed.")
            except Exception as e:
                LOG.exception("Recent CLOSE enforcement (host) failed: %s", e)
        else:
            LOG.info("Recent CLOSE enforcement host hook not implemented; using PlaceAnOrder fallback.")

        # PlaceAnOrder fallback: search last N days' CLOSE rows and close even with missing ATM/exp (market fallback).
        self._run_place_an_order([
            "--mode", "force-close",
            "--min-limit", "0.05",
            "--use-live-close", "join",
            "--quiet"
        ])
        self._summarize_latest_attempts()

    def _reconcile_positions_with_signals_lookback(self, days: int = 21) -> None:
        """
        Outside RTH, reconcile current positions against the *latest* signal per symbol
        by scanning only the symbols we currently hold. For each held symbol, walk the
        last `days` of CSVs (newest -> oldest) and stop at the first matching row.
        If that row's signal is OPEN (and matches orientation) -> log and skip; if CLOSE or mismatched OPEN -> submit a force-close.
        """
        # 1) Collect currently held option symbols and their orientation
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

        # held_info: dict[symbol: str, sign: int|None]
        held_info: dict[str, int | None] = {}
        try:
            poss = ib.positions()
            # Gather all option legs per symbol with avgCost to compute exposure
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

            # For each symbol, try to infer orientation
            for sym, legs in legs_by_sym.items():
                sign: int | None = None
                # Only consider if at least 2 legs
                if len(legs) >= 2:
                    # Separate by right
                    calls = [(strike, qty, avg) for r, strike, qty, avg in legs if r == "C"]
                    puts  = [(strike, qty, avg) for r, strike, qty, avg in legs if r == "P"]
                    call_debit = False
                    put_debit = False
                    # For CALLs: qty>0 at lower strike, qty<0 at higher strike (vertical debit)
                    for i in range(len(calls)):
                        for j in range(len(calls)):
                            if i == j: continue
                            s1, q1, _ = calls[i]
                            s2, q2, _ = calls[j]
                            if s1 < s2 and q1 > 0 and q2 < 0:
                                call_debit = True
                    # For PUTs: qty>0 at higher strike, qty<0 at lower strike (vertical debit)
                    for i in range(len(puts)):
                        for j in range(len(puts)):
                            if i == j: continue
                            s1, q1, _ = puts[i]
                            s2, q2, _ = puts[j]
                            if s1 > s2 and q1 > 0 and q2 < 0:
                                put_debit = True

                    if call_debit and not put_debit:
                        sign = +1
                    elif put_debit and not call_debit:
                        sign = -1
                    elif call_debit and put_debit:
                        # Both a call debit and a put debit exist (e.g., both sides open at once).
                        # Choose a dominant orientation by notional exposure (|qty| * avgCost * 100).
                        call_notional = sum(abs(q) * (avg if avg > 0 else 1.0) * 100.0 for _, q, avg in calls)
                        put_notional  = sum(abs(q) * (avg if avg > 0 else 1.0) * 100.0 for _, q, avg in puts)
                        if call_notional > put_notional:
                            sign = +1
                        elif put_notional > call_notional:
                            sign = -1
                        else:
                            # still ambiguous
                            sign = None
                    else:
                        sign = None
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

        # 2) For each held symbol, scan newest->oldest CSVs until we find its latest signal
        from zoneinfo import ZoneInfo
        NY_TZ = ZoneInfo("America/New_York")
        now = self._now_ny()

        def _csv_path_for(dt: datetime) -> str:
            folder = dt.astimezone(NY_TZ).strftime("%y_%m_%d")
            return fr"C:\OptionsHistory\{folder}\combined_listener_spreads.csv"

        looked = 0
        submitted = 0
        for sym, cur_sign in sorted(held_info.items()):
            found_row = None
            found_day = None
            # newest -> oldest days
            for d in range(0, max(1, days)):
                dt = now - timedelta(days=d)
                fp = _csv_path_for(dt)
                if not os.path.exists(fp):
                    continue
                try:
                    last_row = None
                    with open(fp, newline='', encoding='utf-8') as fh:
                        rdr = csv.DictReader(fh)
                        for row in rdr:
                            rsym = (row.get('symbol') or '').strip().upper()
                            if rsym != sym:
                                continue
                            last_row = row  # last matching row for the day
                    if last_row is None:
                        continue
                    found_row = last_row
                    found_day = dt.date()
                    break  # stop scanning older days for this symbol
                except Exception as e:
                    LOG.warning("Reconcile: error reading %s for %s: %s", fp, sym, e)
                    continue

            looked += 1
            if not found_row:
                LOG.info("Reconcile: no recent signal found within %d day(s) for %s; skipping.", days, sym)
                continue

            # Extract strategy_position first; fallback to signal_type/signal_side
            sp_raw = (found_row.get('strategy_position') or '').strip()
            side_raw = (found_row.get('signal_type') or found_row.get('signal_side') or '')
            side = side_raw.strip().lower()
            sp = None
            try:
                sp_int = int(sp_raw)
                if sp_int in (1, -1):
                    sp = sp_int
            except Exception:
                sp = None

            is_open = False
            is_close = False
            # Determine open/close intent
            if 'close' in side:
                is_close = True
            elif 'open' in side:
                is_open = True
            elif sp is not None:
                # If side not clear but sp present, treat as open
                is_open = True

            if is_open and sp is not None:
                # If our orientation is known and mismatches the latest OPEN, close.
                if cur_sign is not None and sp != cur_sign:
                    if not hasattr(self, "_submitted_close_syms"):
                        self._submitted_close_syms = set()
                    if sym in self._submitted_close_syms or self._has_working_close_order(sym):
                        LOG.info("Reconcile: skipping CLOSE for %s (already submitted this run or working close order exists).", sym)
                        try:
                            _AttemptLogger.write(symbol=sym, action="close", status="skipped",
                                                 reason="working_close_order",
                                                 exp=found_row.get("expiration",""),
                                                 right=(found_row.get("right","") or found_row.get("signal_right","")),
                                                 source="dcm-reconcile")
                        except Exception:
                            pass
                        continue
                    # Try direct close from current positions first (market), then fallback to PlaceAnOrder
                    if self._try_close_from_positions(sym, prefer='MKT'):
                        if hasattr(self, "_submitted_close_syms"):
                            self._submitted_close_syms.add(sym)
                        submitted += 1
                        LOG.info("Reconcile: direct-close from positions submitted for %s.", sym)
                        try:
                            _AttemptLogger.write(symbol=sym, action="close", status="submitted",
                                                 reason="direct_close_from_positions",
                                                 exp=found_row.get("expiration",""),
                                                 right=(found_row.get("right","") or found_row.get("signal_right","")),
                                                 source="dcm-reconcile")
                        except Exception:
                            pass
                    else:
                        self._run_place_an_order([
                            "--mode", "force-close",
                            "--symbols", sym,
                            "--min-limit", "0.05",
                            "--quiet"
                        ])
                        if hasattr(self, "_submitted_close_syms"):
                            self._submitted_close_syms.add(sym)
                        submitted += 1
                        LOG.info("Reconcile: submitted CLOSE for %s via PlaceAnOrder.", sym)
                        try:
                            _AttemptLogger.write(symbol=sym, action="close", status="submitted",
                                                 reason="reconcile_mismatch",
                                                 exp=found_row.get("expiration",""),
                                                 right=(found_row.get("right","") or found_row.get("signal_right","")),
                                                 source="dcm-reconcile")
                        except Exception:
                            pass
                    continue

                # If our orientation is unknown (sign=None) but we clearly hold option legs,
                # and the latest signal indicates opening the *opposite* side (put vs call),
                # heuristically close to reconcile back to the most recent signal.
                if cur_sign is None:
                    # Heuristic: if we hold both sides or ambiguous legs, and the latest signal is OPEN,
                    # force CLOSE to align with the latest signal's side.
                    if not hasattr(self, "_submitted_close_syms"):
                        self._submitted_close_syms = set()
                    if sym in self._submitted_close_syms or self._has_working_close_order(sym):
                        LOG.info("Reconcile: skipping CLOSE for %s (already submitted this run or working close order exists).", sym)
                        try:
                            _AttemptLogger.write(symbol=sym, action="close", status="skipped",
                                                 reason="working_close_order",
                                                 exp=found_row.get("expiration",""),
                                                 right=(found_row.get("right","") or found_row.get("signal_right","")),
                                                 source="dcm-reconcile")
                        except Exception:
                            pass
                        continue
                    # Try direct close from current positions first (market), then fallback to PlaceAnOrder
                    if self._try_close_from_positions(sym, prefer='MKT'):
                        if hasattr(self, "_submitted_close_syms"):
                            self._submitted_close_syms.add(sym)
                        submitted += 1
                        LOG.info("Reconcile: direct-close from positions submitted for %s.", sym)
                        try:
                            _AttemptLogger.write(symbol=sym, action="close", status="submitted",
                                                 reason="direct_close_from_positions",
                                                 exp=found_row.get("expiration",""),
                                                 right=(found_row.get("right","") or found_row.get("signal_right","")),
                                                 source="dcm-reconcile")
                        except Exception:
                            pass
                    else:
                        self._run_place_an_order([
                            "--mode", "force-close",
                            "--symbols", sym,
                            "--min-limit", "0.05",
                            "--quiet"
                        ])
                        if hasattr(self, "_submitted_close_syms"):
                            self._submitted_close_syms.add(sym)
                        submitted += 1
                        LOG.info("Reconcile: submitted CLOSE for %s via PlaceAnOrder.", sym)
                        try:
                            _AttemptLogger.write(symbol=sym, action="close", status="submitted",
                                                 reason="reconcile_ambiguous",
                                                 exp=found_row.get("expiration",""),
                                                 right=(found_row.get("right","") or found_row.get("signal_right","")),
                                                 source="dcm-reconcile")
                        except Exception:
                            pass
                    continue

                LOG.info("Reconcile: latest OPEN for %s matches current orientation (sign=%s, signal=%s, date=%s); leaving position as-is.",
                         sym, cur_sign, sp, found_day)
                try:
                    _AttemptLogger.write(symbol=sym, action="hold", status="skipped",
                                         reason="latest_open_matches",
                                         exp=found_row.get("expiration",""),
                                         right=(found_row.get("right","") or found_row.get("signal_right","")),
                                         source="dcm-reconcile")
                except Exception:
                    pass
                continue

            if is_close:
                if not hasattr(self, "_submitted_close_syms"):
                    self._submitted_close_syms = set()
                if sym in self._submitted_close_syms or self._has_working_close_order(sym):
                    LOG.info("Reconcile: skipping CLOSE for %s (already submitted this run or working close order exists).", sym)
                    try:
                        _AttemptLogger.write(symbol=sym, action="close", status="skipped",
                                             reason="working_close_order",
                                             exp=found_row.get("expiration",""),
                                             right=(found_row.get("right","") or found_row.get("signal_right","")),
                                             source="dcm-reconcile")
                    except Exception:
                        pass
                    continue
                # Try direct close from current positions first (market), then fallback to PlaceAnOrder
                if self._try_close_from_positions(sym, prefer='MKT'):
                    if hasattr(self, "_submitted_close_syms"):
                        self._submitted_close_syms.add(sym)
                    submitted += 1
                    LOG.info("Reconcile: direct-close from positions submitted for %s.", sym)
                    try:
                        _AttemptLogger.write(symbol=sym, action="close", status="submitted",
                                             reason="direct_close_from_positions",
                                             exp=found_row.get("expiration",""),
                                             right=(found_row.get("right","") or found_row.get("signal_right","")),
                                             source="dcm-reconcile")
                    except Exception:
                        pass
                else:
                    self._run_place_an_order([
                        "--mode", "force-close",
                        "--symbols", sym,
                        "--min-limit", "0.05",
                        "--quiet"
                    ])
                    if hasattr(self, "_submitted_close_syms"):
                        self._submitted_close_syms.add(sym)
                    submitted += 1
                    LOG.info("Reconcile: submitted CLOSE for %s via PlaceAnOrder.", sym)
                    try:
                        _AttemptLogger.write(symbol=sym, action="close", status="submitted",
                                             reason="reconcile_close_signal",
                                             exp=found_row.get("expiration",""),
                                             right=(found_row.get("right","") or found_row.get("signal_right","")),
                                             source="dcm-reconcile")
                    except Exception:
                        pass
                continue

            try:
                _AttemptLogger.write(symbol=sym, action="noop", status="skipped",
                                     reason=f"signal={side}",
                                     exp=found_row.get("expiration",""),
                                     right=(found_row.get("right","") or found_row.get("signal_right","")),
                                     source="dcm-reconcile")
            except Exception:
                pass
            LOG.info("Reconcile: latest signal for %s is '%s' (on %s); no action taken.", sym, side, found_day)

        LOG.info("Reconcile lookback (held-first): evaluated %d held symbol(s); submitted %d CLOSE order(s).", looked, submitted)

    def _rth_risk_exits(self, days_old: int = 7, loss_frac: float = 0.5, gain_frac: float = 0.5) -> None:
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
            if t.bid is not None and t.ask is not None and t.ask > 0 and t.bid >= 0:
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

                # Delegate to PlaceAnOrder to place a CLOSE (it will use a limit close per its internal logic)
                try:
                    self._run_place_an_order([
                        "--mode", "force-close",
                        "--symbols", sym,
                        "--quiet"
                    ])
                    submitted += 1
                    reason = "STOP(>=%.0f%% loss)" % (loss_frac*100) if stop_hit else "TP(>=%.0f%% max profit)" % (gain_frac*100)
                    LOG.info("Risk exits: submitted CLOSE for %s %s vertical %s/%s (age %dd) entry=%.2f curr=%.2f width=%.2f reason=%s",
                             sym, right, strike_low, strike_high, (now - t0).days, entry, curr, width, reason)
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
            # New: cancel open unfilled orders if neither leg has OI > threshold (based on today's CSV)
            try:
                self._rth_liquidity_cleanup()
            except Exception as e:
                LOG.warning("RTH liquidity cleanup skipped due to error: %s", e)
            # New: take-profit / stop-loss exits for older positions (RTH only)
            try:
                self._rth_risk_exits(days_old=7, loss_frac=0.5, gain_frac=0.5)
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