import subprocess, sys
from pathlib import Path
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import logging
import csv, os

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
            "--verbose"
        ])

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
            "--quiet"
        ])

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
            "--verbose",
            "--quiet"
        ])

    def _reconcile_positions_with_signals_lookback(self, days: int = 21) -> None:
        """
        Outside RTH, reconcile current positions against the *latest* signal per symbol
        by scanning only the symbols we currently hold. For each held symbol, walk the
        last `days` of CSVs (newest -> oldest) and stop at the first matching row.
        If that row's signal is OPEN -> log and skip; if CLOSE -> submit a force-close
        via PlaceAnOrder. This minimizes work and keeps the decision relevant.
        """
        # 1) Collect currently held option symbols
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

        held_syms: set[str] = set()
        try:
            poss = ib.positions()
            for p in poss:
                c = p.contract
                if getattr(c, 'secType', '') != 'OPT':
                    continue
                q = float(p.position or 0.0)
                if abs(q) < 1e-9:
                    continue
                held_syms.add((getattr(c, 'symbol', '') or '').upper())
        except Exception as e:
            LOG.warning("Reconcile: error fetching positions: %s", e)
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

        if not held_syms:
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
        for sym in sorted(held_syms):
            found_side = None
            found_day = None
            # newest -> oldest days
            for d in range(0, max(1, days)):
                dt = now - timedelta(days=d)
                fp = _csv_path_for(dt)
                if not os.path.exists(fp):
                    continue
                try:
                    # choose the last occurrence for the day if multiple rows exist
                    last_row = None
                    with open(fp, newline='', encoding='utf-8') as fh:
                        rdr = csv.DictReader(fh)
                        for row in rdr:
                            rsym = (row.get('symbol') or '').strip().upper()
                            if rsym != sym:
                                continue
                            last_row = row  # keep walking; last win per day
                    if last_row is None:
                        continue
                    # we found the latest-for-day; interpret its side
                    side_raw = (last_row.get('signal_type') or last_row.get('signal_side') or last_row.get('strategy_position') or '')
                    side = side_raw.strip().lower()
                    found_side = side
                    found_day = dt.date()
                    break  # stop scanning older days for this symbol
                except Exception as e:
                    LOG.warning("Reconcile: error reading %s for %s: %s", fp, sym, e)
                    continue

            looked += 1
            if not found_side:
                LOG.info("Reconcile: no recent signal found within %d day(s) for %s; skipping.", days, sym)
                continue

            if 'open' in found_side:
                LOG.info("Reconcile: latest signal for %s is OPEN (on %s); leaving position as-is.", sym, found_day)
                continue

            if 'close' in found_side:
                try:
                    self._run_place_an_order([
                        "--mode", "force-close",
                        "--symbols", sym,
                        "--min-limit", "0.05",
                        "--quiet"
                    ])
                    submitted += 1
                    LOG.info("Reconcile: submitted CLOSE for %s based on latest CLOSE signal (on %s).", sym, found_day)
                except Exception as e:
                    LOG.warning("Reconcile: failed to submit CLOSE for %s: %s", sym, e)
                continue

            LOG.info("Reconcile: latest signal for %s is '%s' (on %s); no action taken.", sym, found_side, found_day)

        LOG.info("Reconcile lookback (held-first): evaluated %d held symbol(s); submitted %d CLOSE order(s).", looked, submitted)

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
                    self.scan_sector_candidates(sectors)

                    # 2. Filter by liquidity and IV
                    filtered_candidates = self.filter_candidates_by_criteria()
                    LOG.info("Filtered candidates: %s", len(filtered_candidates) if filtered_candidates else 0)

                    # 3. Collect and organize historical data
                    self.update_historical_data(filtered_candidates)
                    sector_data = self.organize_sector_data()

                    # 4. Optimize strategies per sector
                    optimized_params = self.optimize_sector_strategy(sector_data)
                    _ = optimized_params  # keep for future use/logging

                    # 5. Select top 5 performers per sector
                    top_candidates = self.select_top_performers(sector_data, 5)

                    # 6. Generate signals
                    signals = self.generate_trade_signals(top_candidates)

                    # 7. Prepare orders for next day
                    self.prepare_next_day_orders(signals)

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
            try:
                if hasattr(self, "pre_market_open"):
                    self.pre_market_open()

                # Execute prepared orders
                self.execute_pending_orders()

                # Manage existing positions
                self.manage_existing_positions()

                # Handle failed orders
                self.retry_failed_orders()

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
            self.update_all_historical_data()

            # Re-optimise strategies
            self.reoptimize_all_sectors()

            # Remove high IV candidates
            self.remove_high_iv_candidates()

            # Rebalance sector allocations
            self.rebalance_sector_exposure()

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
        """Forces DailyCycleManagement to always run regardless of trading day or time."""
        def _can_run_daily_analysis(self) -> bool:  # always allow
            return True
        def _is_trading_day(self, when=None) -> bool:  # always treat as trading day
            return True

    LOG.info("DailyCycleManagement runner starting (forced execution mode)...")
    try:
        r = _Runner()
        r.daily_trading_cycle()
        LOG.info("DailyCycleManagement runner completed successfully.")
        sys.exit(0)
    except Exception:
        LOG.exception("DailyCycleManagement runner failed.")
        sys.exit(1)