#!/usr/bin/env python3
"""
switch_trading_mode.py — Switch between paper and live IB trading.

Usage:
    python switch_trading_mode.py paper          # Switch to paper trading (port 7497)
    python switch_trading_mode.py live           # Switch to live trading   (port 7496)
    python switch_trading_mode.py status         # Show current mode
    python switch_trading_mode.py live --dry-run # Preview changes, write nothing

Updates 6 files atomically:
    1. InteractiveBrokersTrader/ib_config.py         -- IB_PORT
    2. C:\\OptionsHistory\\bin\\IB_Watchdog.ps1      -- $IB_GW_PORT
    3. C:\\OptionsHistory\\bin\\Health.ps1           -- $IB_PORT
    4. C:\\IBC\\config.ini                           -- TradingMode, ApiPort, OverrideTwsApiPort
    5. C:\\IBC\\run_gateway_service.cmd              -- /Mode:paper|live
    6. Health.ps1 (repo)                             -- $IB_PORT (used by PushButtonMenu + IB_Health_0715)

Then restarts IBGateway via NSSM so IBC auto-logs in to the new mode.
Then restarts OptionsListener so it reloads ib_config.py with the new IB_PORT.
IBC handles all login dialogs automatically using saved credentials in config.ini.
"""

import re
import subprocess
import sys
from pathlib import Path

# Set to True when --dry-run is passed; suppresses all writes and restarts.
DRY_RUN: bool = False

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------
SCRIPT_DIR   = Path(__file__).resolve().parent
IB_CONFIG_PY = SCRIPT_DIR / "InteractiveBrokersTrader" / "ib_config.py"
WATCHDOG_PS1      = Path(r"C:\OptionsHistory\bin\IB_Watchdog.ps1")
HEALTH_PS1        = Path(r"C:\OptionsHistory\bin\Health.ps1")
HEALTH_PS1_REPO   = SCRIPT_DIR / "Health.ps1"   # Fix AW: also updated by PushButtonMenu + IB_Health_0715
DAILY_HEALTH_PS1  = Path(r"C:\OptionsHistory\bin\DailyHealthCheck.ps1")  # Fix DI: IB_DailyHealth_0830 task
IBC_CONFIG        = Path(r"C:\IBC\config.ini")
RUN_GATEWAY_CMD   = Path(r"C:\IBC\run_gateway_service.cmd")

# ---------------------------------------------------------------------------
# Mode definitions
# ---------------------------------------------------------------------------
MODES = {
    "paper": {
        "port":        7497,
        "trading_mode": "paper",
        "label":       "PAPER",
    },
    "live": {
        "port":        7496,
        "trading_mode": "live",
        "label":       "LIVE",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, original: str, updated: str) -> None:
    """Write updated content; in dry-run mode print a diff instead."""
    if DRY_RUN:
        # Show only changed lines as before/after pairs
        orig_lines = original.splitlines()
        new_lines  = updated.splitlines()
        changes = [
            (i + 1, old, new)
            for i, (old, new) in enumerate(zip(orig_lines, new_lines))
            if old != new
        ]
        if changes:
            print(f"  [DRY RUN] Would update {path}:")
            for lineno, old, new in changes:
                print(f"    line {lineno}:  {old.strip()}")
                print(f"            -> {new.strip()}")
        else:
            print(f"  [DRY RUN] No changes needed in {path}")
    else:
        path.write_text(updated, encoding="utf-8")
        print(f"  [OK] {path}")


def _sub(pattern: str, replacement: str, text: str, flags=0) -> str:
    new, n = re.subn(pattern, replacement, text, flags=flags)
    if n == 0:
        raise ValueError(f"Pattern not found: {pattern!r}")
    return new


def _current_port_from_ib_config() -> int | None:
    """Read IB_PORT from ib_config.py; return None if unreadable."""
    try:
        text = _read(IB_CONFIG_PY)
        m = re.search(r"^IB_PORT\s*[:=]\s*int\s*=\s*(\d+)", text, re.MULTILINE)
        if m:
            return int(m.group(1))
        # fallback: plain assignment
        m = re.search(r"^IB_PORT\s*=\s*(\d+)", text, re.MULTILINE)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Per-file update functions
# ---------------------------------------------------------------------------

def update_ib_config_py(port: int) -> None:
    original = _read(IB_CONFIG_PY)
    updated = _sub(
        r"(^IB_PORT\s*:\s*int\s*=\s*)\d+",
        rf"\g<1>{port}",
        original,
        flags=re.MULTILINE,
    )
    _write(IB_CONFIG_PY, original, updated)


def update_watchdog_ps1(port: int) -> None:
    original = _read(WATCHDOG_PS1)
    # Anchor to start-of-line (MULTILINE) so comment lines are not matched
    updated = _sub(
        r"^(\$IB_GW_PORT\s*=\s*)\d+",
        rf"\g<1>{port}",
        original,
        flags=re.MULTILINE,
    )
    _write(WATCHDOG_PS1, original, updated)


def update_daily_health(port: int) -> None:
    """Fix DI: Update $IB_PORT in DailyHealthCheck.ps1 (used by IB_DailyHealth_0830 task)."""
    original = _read(DAILY_HEALTH_PS1)
    updated = _sub(
        r"^(\$IB_PORT\s*=\s*)\d+",
        rf"\g<1>{port}",
        original,
        flags=re.MULTILINE,
    )
    _write(DAILY_HEALTH_PS1, original, updated)


def update_health_ps1(port: int, path: Path = HEALTH_PS1) -> None:
    original = _read(path)
    # Anchor to start-of-line (MULTILINE) so comment lines are not matched
    updated = _sub(
        r"^(\$IB_PORT\s*=\s*)\d+",
        rf"\g<1>{port}",
        original,
        flags=re.MULTILINE,
    )
    _write(path, original, updated)


def update_run_gateway_cmd(trading_mode: str) -> None:
    """Update /Mode:paper|live in C:\\IBC\\run_gateway_service.cmd."""
    original = _read(RUN_GATEWAY_CMD)
    updated = _sub(
        r"(?i)(/Mode:)(paper|live)",
        rf"\g<1>{trading_mode}",
        original,
    )
    _write(RUN_GATEWAY_CMD, original, updated)


def update_ibc_config(port: int, trading_mode: str) -> None:
    """Update TradingMode, ApiPort, and OverrideTwsApiPort in C:\\IBC\\config.ini."""
    original = _read(IBC_CONFIG)
    updated = original

    updated = _sub(
        r"(?i)(^TradingMode\s*=\s*)\S+",
        rf"\g<1>{trading_mode}",
        updated,
        flags=re.MULTILINE,
    )
    updated = _sub(
        r"(?i)(^ApiPort\s*=\s*)\d+",
        rf"\g<1>{port}",
        updated,
        flags=re.MULTILINE,
    )
    updated = _sub(
        r"(?i)(^OverrideTwsApiPort\s*=\s*)\d+",
        rf"\g<1>{port}",
        updated,
        flags=re.MULTILINE,
    )
    _write(IBC_CONFIG, original, updated)


# ---------------------------------------------------------------------------
# IBGateway restart
# ---------------------------------------------------------------------------

def _nssm_status() -> str:
    """Return the current NSSM service status string, or '' on error."""
    try:
        r = subprocess.run(["nssm", "status", "IBGateway"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip().replace(" ", "").upper()
    except Exception:
        return ""


def restart_ibgateway() -> None:
    """Restart the IBGateway Windows service via NSSM.

    nssm restart fails with SERVICE_STOP_PENDING if the service is mid-stop.
    Fall back to stop → wait → start when that happens.
    """
    if DRY_RUN:
        print("\n  [DRY RUN] Would run: nssm restart IBGateway")
        print("  [DRY RUN] IBGateway NOT restarted.")
        return

    import time

    print("\nRestarting IBGateway service (IBC will auto-login)...")
    try:
        result = subprocess.run(
            ["nssm", "restart", "IBGateway"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            print("  [OK] IBGateway service restarted")
            return

        # nssm restart fails when service is SERVICE_STOP_PENDING.
        # Fall back to: wait for it to stop, then start.
        stderr = result.stderr.replace("\x00", "").strip()
        if "STOP_PENDING" in stderr.upper() or result.returncode != 0:
            print("  [INFO] Service is stopping; waiting up to 30s then starting...")
            for _ in range(15):
                time.sleep(2)
                st = _nssm_status()
                if st in ("SERVICE_STOPPED", "STOPPED"):
                    break
            start = subprocess.run(["nssm", "start", "IBGateway"],
                                   capture_output=True, text=True, timeout=30)
            if start.returncode == 0:
                print("  [OK] IBGateway service started")
            else:
                print(f"  [WARN] nssm start returned code {start.returncode}")
                print("  Please restart the IBGateway service manually in Windows Services.")

    except FileNotFoundError:
        print("  [WARN] nssm not found in PATH.")
        print("  Please restart the IBGateway service manually in Windows Services.")
    except subprocess.TimeoutExpired:
        print("  [WARN] nssm restart timed out. Restart IBGateway manually.")


def restart_options_listener() -> None:
    """Restart OptionsListener so it reloads ib_config.py with the new IB_PORT."""
    if DRY_RUN:
        print("  [DRY RUN] Would run: nssm restart OptionsListener")
        return

    print("\nRestarting OptionsListener service (to pick up new IB_PORT)...")
    try:
        result = subprocess.run(
            ["nssm", "restart", "OptionsListener"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            print("  [OK] OptionsListener restarted")
        else:
            stderr = result.stderr.replace("\x00", "").strip()
            print(f"  [WARN] OptionsListener restart returned rc={result.returncode}: {stderr}")
            print("  (The listener may still restart correctly — check health after)")
    except FileNotFoundError:
        print("  [WARN] nssm not found in PATH. Restart OptionsListener manually.")
    except subprocess.TimeoutExpired:
        print("  [WARN] nssm restart timed out. Restart OptionsListener manually.")


# ---------------------------------------------------------------------------
# Status display
# ---------------------------------------------------------------------------

def show_status() -> None:
    port = _current_port_from_ib_config()
    if port is None:
        print("Could not determine current mode from ib_config.py")
        return
    if port == 7497:
        print(f"Current mode: PAPER (port {port})")
    elif port == 7496:
        print(f"Current mode: LIVE  (port {port})")
    else:
        print(f"Current mode: UNKNOWN (port {port})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global DRY_RUN

    args = sys.argv[1:]
    DRY_RUN = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    if not args or args[0].lower() not in ("paper", "live", "status"):
        print(__doc__)
        sys.exit(1)

    action = args[0].lower()

    if action == "status":
        show_status()
        return

    mode = MODES[action]
    port = mode["port"]
    trading_mode = mode["trading_mode"]
    label = mode["label"]

    # Check if already in the requested mode
    current = _current_port_from_ib_config()
    if current == port:
        if DRY_RUN:
            print(f"[DRY RUN] Already in {label} mode (port {port}). Nothing would change.")
        else:
            print(f"Already in {label} mode (port {port}). No changes made.")
        return

    if DRY_RUN:
        print(f"\n[DRY RUN] Preview: switching from port {current} -> {port} ({label})\n")
    else:
        print(f"\nSwitching to {label} trading (port {port})...\n")

    errors = []

    for name, fn, fargs in [
        ("ib_config.py",                           update_ib_config_py,    (port,)),
        ("IB_Watchdog.ps1",                        update_watchdog_ps1,    (port,)),
        ("C:\\OptionsHistory\\bin\\Health.ps1",    update_health_ps1,      (port,)),
        ("C:\\IBC\\config.ini",                    update_ibc_config,      (port, trading_mode)),
        ("C:\\IBC\\run_gateway_service.cmd",       update_run_gateway_cmd, (trading_mode,)),
        ("Health.ps1 (repo)",                      update_health_ps1,      (port, HEALTH_PS1_REPO)),
        ("C:\\OptionsHistory\\bin\\DailyHealthCheck.ps1", update_daily_health, (port,)),  # Fix DI
    ]:
        try:
            fn(*fargs)
        except Exception as e:
            errors.append(f"  [FAIL] {name}: {e}")

    if errors:
        print("\nErrors encountered:")
        for e in errors:
            print(e)
        if not DRY_RUN:
            print("\nPlease fix the above before restarting IBGateway.")
            sys.exit(1)
        return

    restart_ibgateway()
    restart_options_listener()

    if DRY_RUN:
        print(f"\n[DRY RUN] Complete. No files were written, IBGateway was NOT restarted.")
        print(f"          Run without --dry-run to apply these changes.")
    else:
        print(f"\nDone. System is now configured for {label} trading.")
        if action == "live":
            print("""
LIVE TRADING CHECKLIST (verify before placing orders):
  1. IB Gateway shows 'Live Trading' in title bar (not 'Paper Trading')
  2. 'Read-Only API' is OFF in IB Gateway API settings
  3. Market data subscriptions are active (Error 354 = subscription missing)
  4. Run Health.ps1 to confirm all services healthy on port 7496
""")
        else:
            print("Paper trading active. Run Health.ps1 to confirm port 7497 healthy.")


if __name__ == "__main__":
    main()
